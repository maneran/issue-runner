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
    assert data["totals"] == {"usd": 1.5, "minutes": 20}
    assert data["picked"] == [7]
