"""Local orchestration: one Run for every repo in repos.yml, driven by launchd.

  python -m runner.loop --config repos.yml [--only <path substring>] [--quota N]

Each Claude session runs `claude -p` inside a throwaway git worktree, so the
admin's own checkout and uncommitted work are never touched. Tool access is the
allow-list in the repo config (`allowed_tools`); anything outside it is denied,
not prompted, because there is nobody at the keyboard.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from runner import cli

HERE = Path(__file__).resolve().parent.parent
WORKTREES = Path.home() / ".issue-runner" / "worktrees"
LOGS = Path.home() / "Library" / "Logs" / "issue-runner"

DEFAULT_ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep", "Agent",
    "Bash(git:*)", "Bash(gh:*)", "Bash(pytest:*)", "Bash(.venv/bin/*)",
    "Bash(ruff:*)", "Bash(npm:*)", "Bash(python:*)", "Bash(python3:*)",
]


def sh(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{proc.stderr.strip()}")
    return proc


def repo_slug(path: Path) -> str:
    return sh(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], cwd=path).stdout.strip()


def render_prompt(name: str, **vars: str) -> str:
    text = (HERE / "prompts" / name).read_text()
    for k, v in vars.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def claude_session(prompt: str, model: str, cwd: Path, minutes: int, claude_bin: str,
                   allowed_tools: list[str], log: Path) -> dict:
    """One non-interactive Claude Code session. Returns the parsed JSON result
    (total_cost_usd, usage, duration_ms, is_error) or a synthetic error dict."""
    cmd = [claude_bin, "-p", prompt, "--model", model, "--output-format", "json",
           "--allowedTools", ",".join(allowed_tools)]
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=minutes * 60)
        log.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {"is_error": True, "result": proc.stdout[-2000:]}
        data["exit_code"] = proc.returncode
    except subprocess.TimeoutExpired as e:
        log.write_text(e.stdout if isinstance(e.stdout, str) else "")
        data = {"is_error": True, "timed_out": True, "result": f"killed after {minutes} min"}
    data["minutes"] = round((time.time() - started) / 60, 1)
    return data


def worktree(repo_path: Path, number: int, base_branch: str) -> Path:
    wt = WORKTREES / repo_path.name / str(number)
    if wt.exists():
        sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo_path, check=False)
        shutil.rmtree(wt, ignore_errors=True)
    wt.parent.mkdir(parents=True, exist_ok=True)
    sh(["git", "fetch", "origin", base_branch], cwd=repo_path)
    sh(["git", "worktree", "add", "--detach", str(wt), f"origin/{base_branch}"], cwd=repo_path)
    return wt


def remove_worktree(repo_path: Path, wt: Path) -> None:
    sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo_path, check=False)


def run_repo(entry: dict, global_cfg: dict, quota_override: str | None, run_dir: Path) -> None:
    repo_path = Path(entry["path"]).expanduser()
    slug = repo_slug(repo_path)
    cfg = cli.apply_defaults(entry.get("config") or {}, quota_override)
    claude_bin = global_cfg.get("claude_bin", "claude")
    allowed = cfg.get("allowed_tools") or DEFAULT_ALLOWED_TOOLS
    name = repo_path.name
    print(f"== {slug} ({repo_path})")

    plan_file = run_dir / f"{name}-plan.json"
    results_dir = run_dir / f"{name}-results"
    results_dir.mkdir(parents=True, exist_ok=True)
    cli.main(["plan", "--repo", slug, "--config-json", json.dumps(cfg), "--quota", quota_override or "",
              "--out", str(plan_file)])
    plan = json.loads(plan_file.read_text())
    labels = plan["labels"]

    if "triage" in cfg["stages"]:
        wt = worktree(repo_path, 0, cfg["base_branch"])
        try:
            data = claude_session(render_prompt("triage.md", repo=slug), cfg["models"]["triage"], wt,
                                  cfg.get("triage_minutes", 20), claude_bin, allowed, run_dir / f"{name}-triage.log")
            (results_dir / "triage").mkdir(exist_ok=True)
            moves = wt / "triage-moves.json"
            if moves.exists():
                shutil.copy(moves, results_dir / "triage" / "triage-moves.json")
            (results_dir / "triage" / "session.json").write_text(json.dumps(
                {k: data.get(k) for k in ("total_cost_usd", "usage", "minutes", "is_error")}))
        finally:
            remove_worktree(repo_path, wt)

    for pick in plan["picked"]:
        n = pick["number"]
        wt = worktree(repo_path, n, cfg["base_branch"])
        try:
            data = claude_session(
                render_prompt("implement.md", repo=slug, number=str(n), base_branch=cfg["base_branch"]),
                cfg["models"]["implement"], wt, cfg["per_issue_minutes"], claude_bin, allowed,
                run_dir / f"{name}-issue-{n}.log")
            session_file = run_dir / f"{name}-issue-{n}.json"
            session_file.write_text(json.dumps(data))
            cli.main(["result", "--repo", slug, "--number", str(n), "--execution-file", str(session_file),
                      "--minutes", str(data["minutes"]), "--exit-code", "1" if data.get("is_error") else "0",
                      "--in-flight-label", labels["in_flight"], "--out", str(results_dir / f"result-{n}.json")])
        finally:
            remove_worktree(repo_path, wt)

    cli.main(["finalize", "--plan", str(plan_file), "--results-dir", str(results_dir)])


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="issue-runner loop")
    p.add_argument("--config", default=str(HERE / "repos.yml"))
    p.add_argument("--only", default=None, help="path substring; run that repo only")
    p.add_argument("--quota", default=None)
    args = p.parse_args(argv)

    os.environ["ISSUE_RUNNER_LOCAL"] = "1"
    global_cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    run_dir = LOGS / time.strftime("%Y%m%d-%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for entry in global_cfg.get("repos", []):
        if args.only and args.only not in entry["path"]:
            continue
        try:
            run_repo(entry, global_cfg, args.quota, run_dir)
        except Exception as e:  # one repo failing must not stop the others
            failures += 1
            print(f"!! {entry['path']}: {e}", file=sys.stderr)
    print(f"logs: {run_dir}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
