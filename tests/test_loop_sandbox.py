import json
import subprocess
from pathlib import Path

from runner import loop


def test_sandbox_fences_the_checkouts_and_every_env_file():
    box = loop.sandbox_settings(Path("/Users/admin/Development/app"))["sandbox"]
    assert box["enabled"] and box["failIfUnavailable"]
    assert box["allowUnsandboxedCommands"] is False
    assert "/Users/admin/Development" in box["filesystem"]["denyRead"]
    assert "~/**/.env" in box["filesystem"]["denyRead"]
    assert box["filesystem"]["allowRead"] == ["/Users/admin/Development/app/.git"]
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
