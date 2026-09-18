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
from datetime import date, timedelta
from pathlib import Path

import yaml

from runner import cli
from runner.usage import usage_from_transcripts

HERE = Path(__file__).resolve().parent.parent
WORKTREES = Path.home() / ".issue-runner" / "worktrees"
LOGS = Path.home() / "Library" / "Logs" / "issue-runner"

DEFAULT_ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep", "Agent",
    "Bash(git:*)", "Bash(gh:*)", "Bash(pytest:*)", "Bash(.venv/bin/*)",
    "Bash(ruff:*)", "Bash(npm:*)", "Bash(python:*)", "Bash(python3:*)",
]


MARKER = Path.home() / ".issue-runner" / "last-run-date"


def slot_date(schedule: dict, now: time.struct_time | None = None) -> str:
    """Date of the latest scheduled time at or before now: today's if the clock has passed
    schedule.hour:minute, else yesterday's."""
    now = now or time.localtime()
    hour, minute = int(schedule.get("hour", 6)), int(schedule.get("minute", 0))
    day = date(now.tm_year, now.tm_mon, now.tm_mday)
    if (now.tm_hour, now.tm_min) < (hour, minute):
        day -= timedelta(days=1)
    return day.isoformat()


def due(schedule: dict, marker: Path, now: time.struct_time | None = None) -> bool:
    """True once per scheduled slot, at the first tick at or after schedule.hour:minute local time.
    launchd wakes us every few minutes (StartInterval); a closed laptop just runs at the first
    tick after wake, even when that is past midnight. The marker file holds the last slot served."""
    return not (marker.exists() and marker.read_text().strip() == slot_date(schedule, now))


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
    # Subscription only: an exported API key must never reach the session.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=minutes * 60)
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


def record_usage(data: dict, wt: Path, started: float, cfg: dict) -> None:
    """Tokens always; a dollar figure only when the admin asks for the API equivalent."""
    show = bool(cfg.get("show_api_equivalent", False))
    if not data.get("usage"):
        data.update(usage_from_transcripts(str(wt), since=started, price=show))
    if not show:
        data["total_cost_usd"] = None
        data["cost_source"] = "subscription"


def worktree(repo_path: Path, number: int, base_branch: str, branch: str | None = None) -> Path:
    """Fresh worktree at origin/<base_branch>, or on an existing agent branch when continuing."""
    wt = WORKTREES / repo_path.name / str(number)
    if wt.exists():
        sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo_path, check=False)
        shutil.rmtree(wt, ignore_errors=True)
    wt.parent.mkdir(parents=True, exist_ok=True)
    if branch:
        sh(["git", "fetch", "origin", branch], cwd=repo_path)
        sh(["git", "branch", "-f", branch, f"origin/{branch}"], cwd=repo_path)
        sh(["git", "worktree", "add", str(wt), branch], cwd=repo_path)
    else:
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
            started = time.time()
            data = claude_session(render_prompt("triage.md", repo=slug), cfg["models"]["triage"], wt,
                                  cfg.get("triage_minutes", 20), claude_bin, allowed, run_dir / f"{name}-triage.log")
            record_usage(data, wt, started, cfg)
            (results_dir / "triage").mkdir(exist_ok=True)
            moves = wt / "triage-moves.json"
            if moves.exists():
                shutil.copy(moves, results_dir / "triage" / "triage-moves.json")
            (results_dir / "triage" / "session.json").write_text(json.dumps(
                {k: data.get(k) for k in ("total_cost_usd", "usage", "minutes", "is_error")}))
        finally:
            remove_worktree(repo_path, wt)

    try:
        for pick in plan["picked"]:
            n = pick["number"]
            try:
                implement_one(pick, cfg, slug, repo_path, run_dir, results_dir, labels, claude_bin, allowed)
            except (Exception, SystemExit) as e:  # one pick failing must not strand the others
                print(f"!! {slug}#{n}: {e}", file=sys.stderr)
                (results_dir / f"result-{n}.json").write_text(json.dumps(
                    {"number": n, "status": "failed", "error": str(e)[:500]}))
    finally:
        cli.main(["finalize", "--plan", str(plan_file), "--results-dir", str(results_dir)])


def implement_one(pick: dict, cfg: dict, slug: str, repo_path: Path, run_dir: Path, results_dir: Path,
                  labels: dict, claude_bin: str, allowed: list[str]) -> None:
    """One Implement session for one pick: worktree, claude -p, result labels. Always removes the worktree."""
    n = pick["number"]
    name = repo_path.name
    minutes = int(pick.get("minutes") or cfg["per_issue_minutes"])
    pr = pick.get("pr") or {}
    wt = worktree(repo_path, n, cfg["base_branch"], branch=pr.get("headRefName") if pick.get("continue") else None)
    try:
        if pick.get("continue"):
            prompt = render_prompt("continue.md", repo=slug, number=str(n), base_branch=cfg["base_branch"],
                                   minutes=str(minutes), pr_number=str(pr.get("number", "")), branch=pr.get("headRefName", ""))
        else:
            prompt = render_prompt("implement.md", repo=slug, number=str(n), base_branch=cfg["base_branch"],
                                   minutes=str(minutes))
        started = time.time()
        data = claude_session(prompt, cfg["models"]["implement"], wt, minutes, claude_bin, allowed,
                              run_dir / f"{name}-issue-{n}.log")
        record_usage(data, wt, started, cfg)
        session_file = run_dir / f"{name}-issue-{n}.json"
        session_file.write_text(json.dumps(data))
        args = ["result", "--repo", slug, "--number", str(n), "--execution-file", str(session_file),
                "--minutes", str(data["minutes"]), "--exit-code", "1" if data.get("is_error") else "0",
                "--in-flight-label", labels["in_flight"], "--continue-label", labels["cont"],
                "--out", str(results_dir / f"result-{n}.json")]
        if data.get("timed_out"):
            args.append("--timed-out")
        cli.main(args)
    finally:
        remove_worktree(repo_path, wt)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="issue-runner loop")
    p.add_argument("--config", default=str(HERE / "repos.yml"))
    p.add_argument("--only", default=None, help="path substring; run that repo only")
    p.add_argument("--quota", default=None)
    p.add_argument("--tick", action="store_true",
                   help="scheduler mode: run only if past today's schedule and not yet run today")
    args = p.parse_args(argv)

    os.environ["ISSUE_RUNNER_LOCAL"] = "1"
    global_cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    schedule = global_cfg.get("schedule") or {}
    if args.tick and not due(schedule, MARKER):
        return
    if args.tick:  # claim the slot first so a tick landing mid-Run does not start a second one
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(slot_date(schedule))
    run_dir = LOGS / time.strftime("%Y%m%d-%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)
    attempted = failures = 0
    for entry in global_cfg.get("repos", []):
        if args.only and args.only not in entry["path"]:
            continue
        attempted += 1
        try:
            run_repo(entry, global_cfg, args.quota, run_dir)
        except Exception as e:  # one repo failing must not stop the others
            failures += 1
            print(f"!! {entry['path']}: {e}", file=sys.stderr)
    # Every Claude session leaves a .log in run_dir, so none there means every repo failed
    # before its first session (gh auth, missing claude, network): cheap to retry next tick.
    if args.tick and attempted and failures == attempted and not any(run_dir.glob("*.log")):
        MARKER.unlink(missing_ok=True)
    print(f"logs: {run_dir}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
