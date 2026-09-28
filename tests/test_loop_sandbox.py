import json
import subprocess
from datetime import date
from pathlib import Path

from runner import loop


def test_sandbox_fences_the_checkouts_and_every_env_file():
    box = loop.sandbox_settings(Path("/Users/admin/Development/app"))["sandbox"]
    assert box["enabled"] and box["failIfUnavailable"]
    assert box["allowUnsandboxedCommands"] is False
    assert "/Users/admin/Development" in box["filesystem"]["denyRead"]
    assert "~/**/.env" in box["filesystem"]["denyRead"]
    assert box["filesystem"]["allowRead"] == ["/Users/admin/Development/app/.git", "/Users/admin/Development/app/.venv"]
    assert "pypi.org" not in box["network"]["allowedDomains"]
    assert box["network"]["strictAllowlist"] is True


def test_session_never_gets_an_api_key(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, cwd, env, **kw):
        seen["cmd"], seen["env"] = cmd, env
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"is_error": False}), stderr="")

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-exported")
    wt = tmp_path / "7"
    wt.mkdir()
    loop.claude_session("p", "m", wt, 1, "claude", ["Read"], tmp_path / "log", tmp_path / "repo",
                        {"ANTHROPIC_API_KEY": "from-config", "anthropic_api_key": "test-placeholder"})
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert seen["env"]["anthropic_api_key"] == "test-placeholder"
    assert seen["env"]["UV_CACHE_DIR"] == str(tmp_path / "7.cache" / "uv")
    assert "--settings" in seen["cmd"]


def test_shared_venv_is_never_installed_into_or_edited():
    for rule in ("Bash(pip:*)", "Bash(.venv/bin/python -m pip:*)", "Bash(uv:*)", "Edit(.venv/**)", "Write(.venv/**)"):
        assert rule in loop.DENIED_TOOLS


def test_git_answers_github_with_the_runner_token_only(tmp_path, monkeypatch):
    real_run = subprocess.run

    def fake_security(cmd, **kw):
        if cmd[0] == "security":
            return subprocess.CompletedProcess(cmd, 0, stdout="runner-token\n", stderr="")
        return real_run(cmd, **kw)

    monkeypatch.setattr(loop.subprocess, "run", fake_security)
    env = loop.github_env()
    assert env["GH_TOKEN"] == "runner-token"
    # An admin helper that would answer first: the runner's env must drop it.
    admin = tmp_path / "gitconfig"
    admin.write_text("[credential]\n\thelper = \"!f() { echo password=admin-login; }; f\"\n")
    out = real_run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                   capture_output=True, text=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": str(admin), **env}).stdout
    assert "password=runner-token" in out and "admin-login" not in out


def test_missing_keychain_token_fails_loudly(monkeypatch):
    monkeypatch.setattr(loop.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 44, stdout="", stderr="not found"))
    try:
        loop.github_env()
    except RuntimeError as e:
        assert loop.KEYCHAIN_SERVICE in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def _gh_says(monkeypatch, rc=0, stdout="", stderr=""):
    monkeypatch.setattr(loop.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr))


def test_token_expiring_soon_gives_a_notice(monkeypatch):
    headers = "HTTP/2.0 200 OK\nGithub-Authentication-Token-Expiration: 2026-12-27 10:44:12 +0200\n\n{}"
    _gh_says(monkeypatch, stdout=headers)
    assert loop.token_notice(today=date(2026, 12, 1)) is None
    notice = loop.token_notice(today=date(2026, 12, 20))
    assert "2026-12-27 (7 days)" in notice and "security add-generic-password" in notice
    _gh_says(monkeypatch, stdout="HTTP/2.0 200 OK\n\n{}")  # a token without expiry
    assert loop.token_notice(today=date(2026, 12, 20)) is None


def test_rejected_token_raises_but_offline_does_not(monkeypatch):
    _gh_says(monkeypatch, rc=1, stderr="gh: Bad credentials (HTTP 401)")
    try:
        loop.token_notice()
    except RuntimeError as e:
        assert "rejected" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
    _gh_says(monkeypatch, rc=1, stderr="dial tcp: lookup api.github.com: no such host")
    assert loop.token_notice() is None


def test_notify_at_most_once_a_day(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(loop, "TOKEN_ALERTED", tmp_path / "alerted")
    monkeypatch.setattr(loop.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    loop.notify("token expires")
    loop.notify("token expires")
    assert len(calls) == 1 and calls[0][0] == "osascript"


def test_report_shows_the_notice():
    from runner.cli import report_body
    plan = {"config": {"stages": [], "quota": 0, "per_issue_minutes": 30, "models": {}},
            "labels": {"skip": "agent:skip", "hold": "agent:hold"}, "run_id": "r", "repo": "o/r",
            "started_at": "2026-12-20 06:00", "credential": "subscription", "picked": [], "skipped": [],
            "notice": "The runner's GitHub token expires on 2026-12-27 (7 days)."}
    assert "> **Action needed:** The runner's GitHub token expires" in report_body(plan)
