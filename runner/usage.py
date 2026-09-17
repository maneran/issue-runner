"""Token usage and cost from Claude Code's own transcripts.

`claude -p --output-format json` prints total_cost_usd only when the session
ends on its own. A session killed at the cap prints nothing, so we sum the
per-message `usage` blocks Claude Code writes to ~/.claude/projects/<cwd>/*.jsonl
and price them. That is an estimate at API list prices, marked as such.
"""

from __future__ import annotations

import json
from pathlib import Path

# USD per 1M tokens: input, output, cache read, cache write.
PRICES = {
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
}
KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def project_dir_name(cwd: str) -> str:
    """Claude Code encodes the working directory by replacing every '/' and '.' with '-'."""
    return cwd.replace("/", "-").replace(".", "-")


def usage_from_transcripts(cwd: str, since: float, projects_dir: Path | None = None,
                           price: bool = True) -> dict:
    """Token totals for the session in `cwd` since `since`. With `price=False`
    (subscription runs) no dollar figure is produced: tokens and minutes are the cost."""
    projects_dir = projects_dir or (Path.home() / ".claude" / "projects")
    root = projects_dir / project_dir_name(cwd)
    usage = {k: 0 for k in KEYS}
    cost = 0.0
    if not root.exists():
        return {"usage": usage, "total_cost_usd": None, "cost_source": "no_transcript"}
    for path in root.rglob("*.jsonl"):
        if path.stat().st_mtime < since:
            continue
        with path.open() as f:
            for line in f:
                try:
                    msg = (json.loads(line).get("message") or {})
                except json.JSONDecodeError:
                    continue
                u = msg.get("usage")
                if not u:
                    continue
                prices = PRICES.get(msg.get("model", ""), PRICES["claude-opus-5"])
                for k, p in zip(KEYS, prices):
                    n = int(u.get(k, 0) or 0)
                    usage[k] += n
                    cost += n * p / 1e6
    if not price:
        return {"usage": usage, "total_cost_usd": None, "cost_source": "subscription"}
    return {"usage": usage, "total_cost_usd": round(cost, 4), "cost_source": "transcript_estimate"}
