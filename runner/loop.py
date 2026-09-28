"""Local orchestration: one Run for every repo in repos.yml, driven by launchd.

  python -m runner.loop --config repos.yml [--only <path substring>] [--quota N]

Each Claude session runs `claude -p` inside a throwaway git worktree, so the
admin's own checkout and uncommitted work are never touched. Bash commands run in
Claude Code's OS-level sandbox (see `sandbox_settings`) and are auto-allowed there;
every other tool call needs the allow-list (`allowed_tools`). Anything outside both
is denied, not prompted, because there is nobody at the keyboard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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

# Bare "Bash" is safe only because of the sandbox: with allowUnsandboxedCommands off, every
# command runs inside it except one that is wholly a SANDBOX_EXCLUDED command (a compound such
# as `gh ... && cat x` stays sandboxed), and DENIED_TOOLS refuses the risky forms of those.
# Without it, loops, pipes and redirects are refused even inside the sandbox.
DEFAULT_ALLOWED_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Agent", "Bash"]

# Always denied; deny wins over any allow. gh runs unsandboxed with the runner's token, so
# the subcommands that can send arbitrary data or change the account are refused outright.
# The .venv is the admin's, shared by symlink: no installs into it, no edits under it.
DENIED_TOOLS = [
    "Bash(docker:*)", "Read(**/.env)",
    "Bash(pip:*)", "Bash(pip3:*)", "Bash(.venv/bin/pip:*)", "Bash(.venv/bin/pip3:*)", "Bash(uv:*)",
    "Bash(python -m pip:*)", "Bash(python3 -m pip:*)", "Bash(.venv/bin/python -m pip:*)",
    "Edit(.venv/**)", "Write(.venv/**)",
    "Bash(gh api:*)", "Bash(gh gist:*)", "Bash(gh secret:*)", "Bash(gh variable:*)",
    "Bash(gh auth:*)", "Bash(gh ssh-key:*)", "Bash(gh gpg-key:*)", "Bash(gh extension:*)",
    "Bash(gh alias:*)", "Bash(gh repo create:*)", "Bash(gh repo delete:*)", "Bash(gh repo edit:*)",
    "Bash(gh release:*)", "Bash(gh workflow:*)", "Bash(gh pr merge:*)",
]

# Go's TLS check fails under the macOS sandbox (gh), and git needs github.com, which the
# sandbox's network allow-list leaves out. These run outside it, gated by the rules above.
SANDBOX_EXCLUDED = ["gh *", "git push *", "git fetch *", "git pull *", "git ls-remote *"]
# npm only; no PyPI, so nothing pip-installs. GitHub traffic is gh and git, which run outside
# the sandbox, so sandboxed code has no route to GitHub.
SANDBOX_DOMAINS = ["registry.npmjs.org"]

# The runner's own fine-grained token, scoped to its repos. Read at launch; never the admin's gh login.
KEYCHAIN_SERVICE = "issue-runner-github"
TOKEN_WARN_DAYS = 14
TOKEN_ALERTED = Path.home() / ".issue-runner" / "token-alert-date"
REPLACE_TOKEN = (f"Make a new token (README, Local setup), then run `security delete-generic-password -s {KEYCHAIN_SERVICE}` "
                 f"and `security add-generic-password -a \"$USER\" -s {KEYCHAIN_SERVICE} -w`.")


def sandbox_settings(repo_path: Path) -> dict:
    """OS-level limits on every sandboxed Bash command and its children. Writes: the worktree,
    its cache dir and a temp dir only. Reads: not the admin's checkouts (their parent dir; the
    worktree needs only the shared .git and the shared .venv), not credentials, not any .env.
    Network: the npm registry only. No unsandboxed retry, and no session at all if the sandbox is down."""
    return {"sandbox": {
        "enabled": True, "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
        "excludedCommands": SANDBOX_EXCLUDED,
        "filesystem": {
            "denyRead": [str(repo_path.parent), "~/.ssh", "~/.aws", "~/.gnupg", "~/.netrc", "~/.docker",
                         "~/.config/gh", "~/Library/Keychains", "~/**/.env"],
            "allowRead": [str(repo_path / ".git"), str(repo_path / ".venv")],
        },
        "network": {"strictAllowlist": True, "allowedDomains": SANDBOX_DOMAINS},
    }}


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
                   allowed_tools: list[str], log: Path, repo_path: Path, test_env: dict) -> dict:
    """One non-interactive Claude Code session. Returns the parsed JSON result
    (total_cost_usd, usage, duration_ms, is_error) or a synthetic error dict."""
    cache = cache_dir(cwd)
    cache.mkdir(exist_ok=True)
    # The built-in file tools are not sandboxed, so they get the same read and write fence.
    denied = DENIED_TOOLS + [f"Read(/{repo_path.parent}/**)", f"Edit(/{repo_path.parent}/**)"]
    cmd = [claude_bin, "-p", prompt, "--model", model, "--output-format", "json",
           "--settings", json.dumps(sandbox_settings(repo_path)), "--add-dir", str(cache),
           "--allowedTools", ",".join(allowed_tools), "--disallowedTools", ",".join(denied)]
    # The session's own package caches: whatever it installs never reaches the admin's shared ones.
    env = {**os.environ, **test_env,
           "UV_CACHE_DIR": str(cache / "uv"), "npm_config_cache": str(cache / "npm"), "PIP_CACHE_DIR": str(cache / "pip")}
    # Subscription only: an exported API key must never reach the session.
    env = {k: v for k, v in env.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
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


def github_env() -> dict:
    """GH_TOKEN for gh, and a git credential helper for HTTPS that answers with it. Env only:
    the admin's gh login and git config stay untouched. The empty helper first drops the
    admin's helpers (osxkeychain), so git neither uses their login nor stores this token."""
    proc = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                          capture_output=True, text=True)
    token = proc.stdout.strip()
    if proc.returncode != 0 or not token:
        raise RuntimeError(f"no GitHub token in the Keychain under service {KEYCHAIN_SERVICE!r}")
    helper = '!f() { test "$1" = get && echo username=x-access-token && echo "password=$GH_TOKEN"; }; f'
    return {"GH_TOKEN": token, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.https://github.com.helper", "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.https://github.com.helper", "GIT_CONFIG_VALUE_1": helper}


def token_notice(today: date | None = None) -> str | None:
    """A warning when the token expires within TOKEN_WARN_DAYS; RuntimeError when GitHub rejects it.
    GitHub states the expiry in a response header. Any other gh failure (offline) is left to the Run."""
    proc = subprocess.run(["gh", "api", "--include", "rate_limit"], capture_output=True, text=True)
    if proc.returncode != 0:
        if "401" in proc.stderr or "Bad credentials" in proc.stderr:
            raise RuntimeError(f"GitHub rejected the runner's token (expired or revoked). {REPLACE_TOKEN}")
        return None
    m = re.search(r"(?im)^github-authentication-token-expiration:\s*(\d{4}-\d{2}-\d{2})", proc.stdout)
    if not m:
        return None
    left = (date.fromisoformat(m.group(1)) - (today or date.today())).days
    if left > TOKEN_WARN_DAYS:
        return None
    return f"The runner's GitHub token expires on {m.group(1)} ({left} days). {REPLACE_TOKEN}"


def notify(message: str) -> None:
    """macOS notification, at most once a day: a failing tick retries every 15 minutes."""
    today = date.today().isoformat()
    if TOKEN_ALERTED.exists() and TOKEN_ALERTED.read_text().strip() == today:
        return
    TOKEN_ALERTED.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_ALERTED.write_text(today)
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(message)} with title \"issue-runner\""],
                   capture_output=True)


def cache_dir(wt: Path) -> Path:
    return wt.with_name(wt.name + ".cache")


def worktree(repo_path: Path, number: int, base_branch: str, setup: list[str], branch: str | None = None) -> Path:
    """Fresh worktree at origin/<base_branch>, or on an existing agent branch when continuing."""
    wt = WORKTREES / repo_path.name / str(number)
    if wt.exists():
        sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo_path, check=False)
        shutil.rmtree(wt, ignore_errors=True)
    shutil.rmtree(cache_dir(wt), ignore_errors=True)
    wt.parent.mkdir(parents=True, exist_ok=True)
    if branch:
        sh(["git", "fetch", "origin", branch], cwd=repo_path)
        sh(["git", "branch", "-f", branch, f"origin/{branch}"], cwd=repo_path)
        sh(["git", "worktree", "add", str(wt), branch], cwd=repo_path)
    else:
        sh(["git", "fetch", "origin", base_branch], cwd=repo_path)
        sh(["git", "worktree", "add", "--detach", str(wt), f"origin/{base_branch}"], cwd=repo_path)
    # The admin's .venv, so `.venv/bin/pytest` works unchanged. The repo must ignore the bare name
    # (.git/info/exclude): a `.venv/` pattern does not match a symlink.
    if (repo_path / ".venv").is_dir():
        (wt / ".venv").symlink_to(repo_path / ".venv")
    # The repo's `setup` from repos.yml (node_modules and the like). Admin config, so it runs
    # outside the sandbox and uses the shared download caches.
    for step in setup:
        proc = subprocess.run(["bash", "-c", step], cwd=wt, capture_output=True, text=True)
        if proc.returncode != 0:
            remove_worktree(repo_path, wt)
            raise RuntimeError(f"setup `{step}` failed:\n{proc.stderr.strip()[-2000:]}")
    return wt


def remove_worktree(repo_path: Path, wt: Path) -> None:
    sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo_path, check=False)
    shutil.rmtree(cache_dir(wt), ignore_errors=True)


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
        wt = worktree(repo_path, 0, cfg["base_branch"], cfg.get("setup", []))
        try:
            started = time.time()
            data = claude_session(render_prompt("triage.md", repo=slug), cfg["models"]["triage"], wt,
                                  cfg.get("triage_minutes", 20), claude_bin, allowed, run_dir / f"{name}-triage.log",
                                  repo_path, cfg.get("env", {}))
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
    wt = worktree(repo_path, n, cfg["base_branch"], cfg.get("setup", []),
                  branch=pr.get("headRefName") if pick.get("continue") else None)
    try:
        if pick.get("continue"):
            prompt = render_prompt("continue.md", repo=slug, number=str(n), base_branch=cfg["base_branch"],
                                   minutes=str(minutes), pr_number=str(pr.get("number", "")), branch=pr.get("headRefName", ""))
        else:
            prompt = render_prompt("implement.md", repo=slug, number=str(n), base_branch=cfg["base_branch"],
                                   minutes=str(minutes))
        started = time.time()
        data = claude_session(prompt, cfg["models"]["implement"], wt, minutes, claude_bin, allowed,
                              run_dir / f"{name}-issue-{n}.log", repo_path, cfg.get("env", {}))
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
    try:  # before claiming the slot: a missing or rejected token is retried next tick
        os.environ.update(github_env())
        notice = token_notice()
    except RuntimeError as e:
        notify(str(e))
        sys.exit(f"!! {e}")
    if notice:  # the Run Report shows it; cli reads the env like ISSUE_RUNNER_LOCAL
        notify(notice)
        os.environ["ISSUE_RUNNER_NOTICE"] = notice
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
