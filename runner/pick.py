"""Pure pick logic. No I/O here so it stays testable."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ReportTotals:
    usd: float = 0.0
    minutes: float = 0.0


def _names(issue: dict) -> set[str]:
    return {l["name"] for l in issue.get("labels", [])}


def _skip_reason(issue: dict, labels: dict) -> str | None:
    names = _names(issue)
    if labels["ready"] not in names:
        return f"not {labels['ready']}"
    for key in ("skip", "hold", "in_flight"):
        if labels[key] in names:
            return labels[key]
    if issue.get("assignees"):
        return "assigned"
    if issue.get("blocked_by", 0) > 0:
        return "blocked"
    return None


def _sort_key(issue: dict, labels: dict):
    names = _names(issue)
    priorities = labels["priority"]
    rank = next((i for i, p in enumerate(priorities) if p in names), len(priorities) - 1)
    is_bug = 0 if labels["bug"] in names else 1
    return (rank, is_bug, issue["createdAt"])


def select_picks(issues: list[dict], quota: int, labels: dict) -> tuple[list[dict], list[tuple[int, str]]]:
    """Return (picked, skipped). skipped is [(number, reason)] for every issue not picked."""
    skipped: list[tuple[int, str]] = []
    eligible: list[dict] = []
    for issue in issues:
        reason = _skip_reason(issue, labels)
        if reason:
            skipped.append((issue["number"], reason))
        else:
            eligible.append(issue)
    eligible.sort(key=lambda i: _sort_key(i, labels))
    picked = eligible[:quota]
    skipped.extend((i["number"], "quota reached") for i in eligible[quota:])
    return picked, skipped


def budget_used(reports: list[dict], now: datetime) -> ReportTotals:
    """Sum totals of Run Reports created in the same calendar month as `now`."""
    usd = minutes = 0.0
    for r in reports:
        created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        if (created.year, created.month) != (now.year, now.month):
            continue
        totals = r.get("totals", {})
        usd += float(totals.get("usd", 0))
        minutes += float(totals.get("minutes", 0))
    return ReportTotals(usd=usd, minutes=minutes)
