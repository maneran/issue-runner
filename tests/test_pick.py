import json
from pathlib import Path
from datetime import datetime, timezone

from runner.pick import select_picks, budget_used, ReportTotals


def issue(n, labels=(), assignees=(), created="2026-01-01T00:00:00Z", blocked=0):
    return {
        "number": n,
        "title": f"issue {n}",
        "url": f"https://github.com/o/r/issues/{n}",
        "labels": [{"name": l} for l in labels],
        "assignees": [{"login": a} for a in assignees],
        "createdAt": created,
        "blocked_by": blocked,
    }


LABELS = {
    "ready": "ready-for-agent", "skip": "agent:skip", "hold": "agent:hold",
    "in_flight": "agent:in-flight", "bug": "bug", "priority": ["P0", "P1", "P2"],
}


def test_quota_zero_picks_nothing_but_lists_candidates():
    picked, skipped = select_picks([issue(1, ["ready-for-agent"])], quota=0, labels=LABELS)
    assert picked == []
    assert skipped == [(1, "quota reached")]


def test_sort_priority_then_bug_then_oldest():
    issues = [
        issue(1, ["ready-for-agent", "enhancement"], created="2026-01-01T00:00:00Z"),
        issue(2, ["ready-for-agent", "bug"], created="2026-01-05T00:00:00Z"),
        issue(3, ["ready-for-agent", "P0"], created="2026-01-09T00:00:00Z"),
        issue(4, ["ready-for-agent", "bug"], created="2026-01-02T00:00:00Z"),
    ]
    picked, _ = select_picks(issues, quota=10, labels=LABELS)
    assert [i["number"] for i in picked] == [3, 4, 2, 1]


def test_skip_reasons():
    issues = [
        issue(1, ["ready-for-agent", "agent:skip"]),
        issue(2, ["ready-for-agent", "agent:hold"]),
        issue(3, ["ready-for-agent", "agent:in-flight"]),
        issue(4, ["ready-for-agent"], assignees=["bob"]),
        issue(5, ["ready-for-agent"], blocked=1),
        issue(6, ["needs-triage"]),
        issue(7, ["ready-for-agent"]),
    ]
    picked, skipped = select_picks(issues, quota=10, labels=LABELS)
    assert [i["number"] for i in picked] == [7]
    assert dict(skipped) == {
        1: "agent:skip", 2: "agent:hold", 3: "agent:in-flight",
        4: "assigned", 5: "blocked", 6: "not ready-for-agent",
    }


def test_budget_used_sums_only_this_month():
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    reports = [
        {"createdAt": "2026-09-01T06:00:00Z", "totals": {"usd": 4.5, "minutes": 30}},
        {"createdAt": "2026-09-10T06:00:00Z", "totals": {"usd": 2.0, "minutes": 12}},
        {"createdAt": "2026-08-30T06:00:00Z", "totals": {"usd": 99.0, "minutes": 999}},
    ]
    assert budget_used(reports, now) == ReportTotals(usd=6.5, minutes=42)


def test_report_json_roundtrip():
    from runner.cli import parse_report_json, report_body

    plan = {
        "run_id": "1", "repo": "o/r", "started_at": "2026-09-17 06:00", "credential": "oauth",
        "config": {"stages": [], "quota": 0, "per_issue_minutes": 30, "models": {}},
        "labels": {"skip": "agent:skip", "hold": "agent:hold"},
        "picked": [{"number": 7, "title": "t"}], "skipped": [(1, "quota reached")],
    }
    body = report_body(plan, [{"number": 7, "status": "pr_opened", "usd": 1.5, "minutes": 20}])
    data = parse_report_json(body)
    assert data["totals"]["usd"] == 1.5 and data["totals"]["minutes"] == 20
    assert data["picked"] == [7]


def test_cost_from_claude_p_json(tmp_path):
    from runner.cli import cost_from_execution_file

    f = tmp_path / "s.json"
    f.write_text('{"total_cost_usd": 1.25, "usage": {"input_tokens": 10, "output_tokens": 5, '
                 '"cache_read_input_tokens": 100}, "result": "done"}')
    cost = cost_from_execution_file(str(f))
    assert cost == {"total_cost_usd": 1.25, "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}


def test_continue_first_and_in_flight_without_continue_is_skipped():
    labels = dict(LABELS, cont="agent:continue")
    issues = [
        issue(1, ["ready-for-agent", "P0"]),
        issue(2, ["ready-for-agent", "agent:in-flight", "agent:continue"], created="2026-02-01T00:00:00Z"),
        issue(3, ["ready-for-agent", "agent:in-flight"]),
    ]
    picked, skipped = select_picks(issues, quota=5, labels=labels)
    assert [i["number"] for i in picked] == [2, 1]
    assert dict(skipped) == {3: "agent:in-flight"}


def test_continuation_count_from_previous_reports():
    from runner.pick import continuation_count

    reports = [
        {"createdAt": "2026-09-01T06:00:00Z", "results": [{"number": 97, "status": "continued"}]},
        {"createdAt": "2026-09-02T06:00:00Z", "results": [{"number": 97, "status": "continued"}, {"number": 5, "status": "continued"}]},
        {"createdAt": "2026-09-03T06:00:00Z", "results": [{"number": 97, "status": "pr_opened"}]},
    ]
    assert continuation_count(reports, 97) == 2
    assert continuation_count(reports, 5) == 1
    assert continuation_count(reports, 6) == 0


def test_minutes_for_issue_by_size():
    from runner.pick import minutes_for

    caps = {"S": 20, "M": 45, "default": 45}
    assert minutes_for(issue(1, ["size:S"]), caps) == 20
    assert minutes_for(issue(1, ["size:M"]), caps) == 45
    assert minutes_for(issue(1, []), caps) == 45


def test_usage_from_transcripts_prices_and_sums(tmp_path):
    from runner.usage import usage_from_transcripts

    proj = tmp_path / "-Users-me-wt-97"
    proj.mkdir()
    lines = [
        {"message": {"model": "claude-opus-5", "usage": {"input_tokens": 1000000, "output_tokens": 0}}},
        {"message": {"model": "claude-opus-5", "usage": {"cache_read_input_tokens": 1000000, "output_tokens": 1000000}}},
        {"type": "user", "message": {"role": "user", "content": "no usage here"}},
    ]
    (proj / "s.jsonl").write_text("\n".join(json.dumps(l) for l in lines))
    out = usage_from_transcripts("/Users/me/wt/97", since=0, projects_dir=tmp_path)
    assert out["usage"] == {"input_tokens": 1000000, "output_tokens": 1000000, "cache_read_input_tokens": 1000000, "cache_creation_input_tokens": 0}
    assert out["total_cost_usd"] == 5 + 25 + 0.5
    assert out["cost_source"] == "transcript_estimate"


def test_usage_unpriced_on_subscription(tmp_path):
    from runner.usage import usage_from_transcripts

    proj = tmp_path / "-Users-me-wt-5"
    proj.mkdir()
    (proj / "s.jsonl").write_text(json.dumps({"message": {"model": "claude-opus-5", "usage": {"output_tokens": 10}}}))
    out = usage_from_transcripts("/Users/me/wt/5", since=0, projects_dir=tmp_path, price=False)
    assert out["total_cost_usd"] is None and out["cost_source"] == "subscription"
    assert out["usage"]["output_tokens"] == 10


def test_local_mode_refuses_api_key(monkeypatch):
    from runner.cli import credential_kind

    monkeypatch.setenv("ISSUE_RUNNER_LOCAL", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    import pytest
    with pytest.raises(SystemExit):
        credential_kind()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert credential_kind() == "oauth"


def test_due_once_per_day_after_schedule(tmp_path):
    import time
    from runner.loop import due, slot_date

    marker = tmp_path / "last"
    sched = {"hour": 6, "minute": 0}
    early = time.strptime("2026-09-18 05:59", "%Y-%m-%d %H:%M")
    late = time.strptime("2026-09-18 06:00", "%Y-%m-%d %H:%M")
    marker.write_text("2026-09-17")
    assert due(sched, marker, early) is False
    assert due(sched, marker, late) is True
    marker.write_text(slot_date(sched, late))
    assert marker.read_text() == "2026-09-18"
    assert due(sched, marker, late) is False
    tomorrow = time.strptime("2026-09-19 09:30", "%Y-%m-%d %H:%M")
    assert due(sched, marker, tomorrow) is True


def test_due_serves_missed_slot_after_midnight(tmp_path):
    import time
    from runner.loop import due, slot_date

    marker = tmp_path / "last"
    # Laptop closed at 05:00, opened after midnight: yesterday's 06:00 slot is still owed.
    marker.write_text("2026-09-17")
    after_midnight = time.strptime("2026-09-19 00:10", "%Y-%m-%d %H:%M")
    sched = {"hour": 6, "minute": 0}
    assert slot_date(sched, after_midnight) == "2026-09-18"
    assert due(sched, marker, after_midnight) is True
    marker.write_text(slot_date(sched, after_midnight))
    assert due(sched, marker, after_midnight) is False
    # A late schedule with 15-minute ticks: the 00:05 tick still serves the 23:55 slot.
    late = {"hour": 23, "minute": 55}
    marker.write_text("2026-09-17")
    tick = time.strptime("2026-09-19 00:05", "%Y-%m-%d %H:%M")
    assert slot_date(late, tick) == "2026-09-18"
    assert due(late, marker, tick) is True


def test_tick_clears_marker_only_when_every_repo_fails_before_a_session(tmp_path, monkeypatch):
    import pytest
    from runner import loop

    monkeypatch.setattr(loop, "MARKER", tmp_path / "last")
    monkeypatch.setattr(loop, "LOGS", tmp_path / "logs")
    cfg = tmp_path / "repos.yml"
    cfg.write_text("schedule: {hour: 0, minute: 0}\nrepos:\n  - path: /a\n  - path: /b\n")

    def boom(entry, *_):
        raise RuntimeError("gh: not logged in")
    monkeypatch.setattr(loop, "run_repo", boom)
    with pytest.raises(SystemExit):
        loop.main(["--tick", "--config", str(cfg)])
    assert not loop.MARKER.exists()  # next tick retries

    def boom_after_session(entry, global_cfg, quota, run_dir):
        (run_dir / "a-issue-1.log").write_text("")
        raise RuntimeError("finalize failed")
    monkeypatch.setattr(loop, "run_repo", boom_after_session)
    with pytest.raises(SystemExit):
        loop.main(["--tick", "--config", str(cfg)])
    assert loop.MARKER.exists()  # a session ran: do not spend more hours on this slot

    loop.MARKER.unlink()
    ran = []
    monkeypatch.setattr(loop, "run_repo", lambda entry, *_: ran.append(entry["path"]) if entry["path"] == "/a" else boom(entry))
    with pytest.raises(SystemExit):
        loop.main(["--tick", "--config", str(cfg)])
    assert ran == ["/a"]
    assert loop.MARKER.read_text() == loop.slot_date({"hour": 0, "minute": 0})  # one repo ran: slot served


def test_reconcile_closes_issue_when_agent_pr_merged(monkeypatch):
    from runner import cli

    calls = []

    def fake_gh(*args, input=None):
        calls.append(args)
        return ""

    def fake_agent_pr(repo, n, state="open"):
        return {"url": "https://x/pr/9"} if state == "merged" else None

    monkeypatch.setattr(cli, "gh", fake_gh)
    monkeypatch.setattr(cli, "agent_pr", fake_agent_pr)
    labels = {"ready": "ready-for-agent", "in_flight": "agent:in-flight", "cont": "agent:continue",
              "retry": "agent:retry", "human": "ready-for-human"}
    issues = [issue(5, ["ready-for-agent", "agent:in-flight"])]
    handoffs = cli.reconcile_verbs("o/r", issues, labels, reports=[], max_continuations=3)
    assert any(c[:2] == ("issue", "close") for c in calls)
    assert handoffs == [(5, "closed: agent PR merged (https://x/pr/9)")]
    assert {l["name"] for l in issues[0]["labels"]} == set()


def test_quota_three_one_pick_fails_others_still_run_and_finalize(tmp_path, monkeypatch):
    from runner import loop

    picks = [{"number": 1, "title": "a", "url": "u"}, {"number": 2, "title": "b", "url": "u"}, {"number": 3, "title": "c", "url": "u"}]
    calls = []

    def fake_cli_main(argv):
        calls.append(argv[0])
        if argv[0] == "plan":
            out = argv[argv.index("--out") + 1]
            Path(out).write_text(json.dumps({"picked": picks, "labels": {"in_flight": "agent:in-flight", "cont": "agent:continue"}}))
        elif argv[0] == "result":
            n = int(argv[argv.index("--number") + 1])
            if n == 2:
                raise SystemExit("gh pr list failed")  # what cli.gh raises
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps({"number": n, "status": "pr_opened"}))

    sessions = []
    monkeypatch.setattr(loop.cli, "main", fake_cli_main)
    monkeypatch.setattr(loop, "repo_slug", lambda p: "o/r")
    monkeypatch.setattr(loop, "worktree", lambda *a, **k: tmp_path / "wt")
    monkeypatch.setattr(loop, "remove_worktree", lambda *a: None)
    monkeypatch.setattr(loop, "record_usage", lambda *a: None)
    monkeypatch.setattr(loop, "render_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(loop, "claude_session", lambda *a, **k: sessions.append(1) or {"minutes": 1.0})

    run_dir = tmp_path / "run"
    loop.run_repo({"path": str(tmp_path), "config": {"stages": ["implement"], "quota": 3}}, {}, None, run_dir)

    assert len(sessions) == 3, "every pick got a session"
    assert calls[-1] == "finalize"
    results = sorted(p.name for p in (run_dir / f"{tmp_path.name}-results").glob("result-*.json"))
    assert results == ["result-1.json", "result-2.json", "result-3.json"]
    assert json.loads((run_dir / f"{tmp_path.name}-results" / "result-2.json").read_text())["status"] == "failed"
