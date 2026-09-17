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
    continuing = labels.get("cont") in names
    for key in ("skip", "hold", "in_flight"):
        if labels[key] in names and not (key == "in_flight" and continuing):
            return labels[key]
    if issue.get("assignees"):
        return "assigned"
    if issue.get("blocked_by", 0) > 0:
        return "blocked"
    return None


def _sort_key(issue: dict, labels: dict):
    names = _names(issue)
    priorities = labels["priority"]
    cont = 0 if labels.get("cont") in names else 1  # unfinished work first
    rank = next((i for i, p in enumerate(priorities) if p in names), len(priorities) - 1)
    is_bug = 0 if labels["bug"] in names else 1
    return (cont, rank, is_bug, issue["createdAt"])


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


def continuation_count(reports: list[dict], number: int) -> int:
    """How many past Runs ended this issue's session at the cap with work committed."""
    return sum(1 for r in reports for res in r.get("results", [])
               if res.get("number") == number and res.get("status") == "continued")


def minutes_for(issue: dict, caps: dict) -> int:
    names = _names(issue)
    for size, minutes in caps.items():
        if size != "default" and f"size:{size}" in names:
            return int(minutes)
    return int(caps.get("default", 45))


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
