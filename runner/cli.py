"""issue-runner CLI. All GitHub I/O goes through the `gh` CLI so the same code
runs locally and inside Actions.

  python -m runner.cli plan      -> sync labels, pick issues, open the Run Report
  python -m runner.cli finalize  -> fill the Run Report from stage results
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from runner.pick import ReportTotals, budget_used, select_picks

HERE = Path(__file__).resolve().parent.parent
JSON_MARK = "<!-- issue-runner:json -->"

CANONICAL_KEYS = {
    "ready": "ready-for-agent", "skip": "agent:skip", "hold": "agent:hold",
    "in_flight": "agent:in-flight", "bug": "bug", "report": "agent-run",
    "needs_triage": "needs-triage",
}


def gh(*args: str, input: str | None = None) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, input=input)
    if proc.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout


def gh_json(*args: str):
    out = gh(*args)
    return json.loads(out) if out.strip() else []


# --- config -----------------------------------------------------------------

def load_config(path: str, quota_override: str | None) -> dict:
    return apply_defaults(yaml.safe_load(Path(path).read_text()) or {}, quota_override)


def apply_defaults(cfg: dict, quota_override: str | None = None) -> dict:
    cfg = dict(cfg)
    cfg.setdefault("stages", [])
    cfg.setdefault("quota", 0)
    cfg.setdefault("per_issue_minutes", 30)
    cfg.setdefault("stale_days", 7)
    cfg.setdefault("models", {"triage": "claude-sonnet-5", "implement": "claude-opus-5"})
    cfg.setdefault("budget", {})
    cfg.setdefault("base_branch", "main")
    cfg.setdefault("labels", {})
    if quota_override not in (None, ""):
        cfg["quota"] = int(quota_override)
    return cfg


def label_map(cfg: dict) -> dict:
    """Canonical key -> label string actually used in this repo."""
    rename = cfg["labels"]
    m = {k: rename.get(v, v) for k, v in CANONICAL_KEYS.items()}
    m["priority"] = [rename.get(p, p) for p in ("P0", "P1", "P2")]
    return m


def credential_kind() -> str:
    oauth, key = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), os.environ.get("ANTHROPIC_API_KEY")
    if oauth and key:
        raise SystemExit("both CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY are set; pick one")
    if key:
        return "api_key"
    if oauth or os.environ.get("ISSUE_RUNNER_LOCAL"):
        return "oauth"  # local loop: Claude Code's own login (Pro/Max)
    return "none"


# --- step 0: labels -----------------------------------------------------------

def sync_labels(repo: str, cfg: dict) -> None:
    canon = yaml.safe_load((HERE / "labels.yml").read_text())
    rename = cfg["labels"]
    for group in canon.values():
        for name, spec in group.items():
            gh("label", "create", rename.get(name, name), "--repo", repo, "--force",
               "--color", spec["color"], "--description", spec["description"])


# --- pick set -----------------------------------------------------------------

def list_open_issues(repo: str) -> list[dict]:
    issues = gh_json("issue", "list", "--repo", repo, "--state", "open", "--limit", "200",
                     "--json", "number,title,url,labels,assignees,createdAt")
    return issues


def blocked_count(repo: str, number: int) -> int:
    out = gh("api", f"repos/{repo}/issues/{number}",
             "--jq", ".issue_dependencies_summary.blocked_by // 0").strip()
    return int(out or 0)


def previous_reports(repo: str, labels: dict) -> list[dict]:
    issues = gh_json("issue", "list", "--repo", repo, "--label", labels["report"], "--state", "all",
                     "--limit", "100", "--json", "number,createdAt,body")
    reports = []
    for i in issues:
        data = parse_report_json(i.get("body") or "")
        if data:
            reports.append({"createdAt": i["createdAt"], "totals": data.get("totals", {})})
    return reports


def parse_report_json(body: str) -> dict | None:
    m = re.search(re.escape(JSON_MARK) + r"\s*```json\s*(\{.*?\})\s*```", body, re.S)
    return json.loads(m.group(1)) if m else None


def budget_gate(cfg: dict, kind: str, used: ReportTotals) -> str | None:
    b = cfg["budget"]
    if kind == "api_key" and "usd_month" in b and used.usd >= float(b["usd_month"]):
        return f"budget reached: ${used.usd:.2f} of ${b['usd_month']} this month"
    if kind == "oauth" and "hours_month" in b and used.minutes / 60 >= float(b["hours_month"]):
        return f"budget reached: {used.minutes / 60:.1f}h of {b['hours_month']}h this month"
    return None


# --- report -------------------------------------------------------------------

def report_body(plan: dict, results: list[dict] | None = None) -> str:
    cfg, labels = plan["config"], plan["labels"]
    results = results if results is not None else []
    by_number = {r["number"]: r for r in results}
    lines = [
        f"Run `{plan['run_id']}` on `{plan['repo']}` at {plan['started_at']} UTC.",
        f"Credential: `{plan['credential']}` · stages: `{', '.join(cfg['stages']) or 'none'}` · "
        f"quota {cfg['quota']} · cap {cfg['per_issue_minutes']} min/issue · "
        f"status: **{plan.get('status', 'running')}**",
    ]
    if plan.get("gate"):
        lines.append(f"\n> Implement stage skipped: {plan['gate']}")
    lines.append(f"\n## Picked ({len(plan['picked'])})\n")
    if plan["picked"]:
        lines.append("| # | Title | Status | PR | Minutes | Cost |")
        lines.append("|---|---|---|---|---|---|")
        for i in plan["picked"]:
            r = by_number.get(i["number"], {})
            cost = f"${r['usd']:.2f}" if r.get("usd") is not None else "-"
            lines.append(f"| #{i['number']} | {i['title']} | {r.get('status', 'pending')} | "
                         f"{r.get('pr') or '-'} | {r.get('minutes', '-')} | {cost} |")
    else:
        lines.append("_none_")
    lines.append(f"\n## Skipped ({len(plan['skipped'])})\n")
    if plan["skipped"]:
        lines.append("| # | Reason |\n|---|---|")
        lines.extend(f"| #{n} | {reason} |" for n, reason in plan["skipped"])
    else:
        lines.append("_none_")
    lines.append("\n## Triage moves\n")
    moves = plan.get("triage_moves") or []
    lines.append("\n".join(f"- #{m['number']}: {m['move']}" for m in moves) if moves else "_none_")
    lines.append("\n## Waiting on you\n")
    stale = plan.get("stale_prs") or []
    lines.append("\n".join(f"- {p['url']} idle {p['days']}d" for p in stale) if stale else "_none_")
    lines.append(
        "\n## Verbs\n"
        f"`{labels['skip']}` ignore the issue · `agent:retry` redo from scratch · "
        f"`{labels['hold']}` keep the PR, stay quiet. Free text: mention `@claude` on the PR. "
        "Merging is always yours."
    )
    totals = {
        "usd": round(sum(r.get("usd") or 0 for r in results), 4),
        "minutes": round(sum(r.get("minutes") or 0 for r in results), 1),
    }
    data = {
        "run_id": plan["run_id"], "repo": plan["repo"], "started_at": plan["started_at"],
        "credential": plan["credential"], "status": plan.get("status", "running"),
        "config": {k: cfg[k] for k in ("stages", "quota", "per_issue_minutes", "models")},
        "picked": [i["number"] for i in plan["picked"]], "skipped": plan["skipped"],
        "results": results, "triage_moves": moves, "stale_prs": stale, "totals": totals,
    }
    lines.append(f"\n{JSON_MARK}\n```json\n{json.dumps(data, indent=2)}\n```")
    return "\n".join(lines)


def report_title(plan: dict) -> str:
    return f"Run Report {plan['started_at'][:16]} · {plan['repo'].split('/')[-1]}"


# --- stale PRs ----------------------------------------------------------------

def stale_prs(repo: str, days: int, now: datetime) -> list[dict]:
    prs = gh_json("pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
                  "--json", "number,url,headRefName,updatedAt,isDraft")
    out = []
    for p in prs:
        if not p["headRefName"].startswith("agent/"):
            continue
        updated = datetime.fromisoformat(p["updatedAt"].replace("Z", "+00:00"))
        idle = (now - updated).days
        if idle >= days:
            out.append({"url": p["url"], "days": idle})
    return out


# --- commands -----------------------------------------------------------------

def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}<<EOF\n{value}\nEOF\n")
    print(f"{name}={value}")


def cmd_plan(args: argparse.Namespace) -> None:
    now = datetime.now(timezone.utc)
    repo = args.repo
    if args.config_json:
        cfg = apply_defaults(json.loads(args.config_json), args.quota)
    else:
        cfg = load_config(args.config, args.quota)
    labels = label_map(cfg)
    kind = credential_kind()
    sync_labels(repo, cfg)

    issues = list_open_issues(repo)
    for i in issues:
        names = {l["name"] for l in i["labels"]}
        i["blocked_by"] = blocked_count(repo, i["number"]) if labels["ready"] in names else 0
    picked, skipped = select_picks(issues, cfg["quota"], labels)

    gate = None
    if "implement" not in cfg["stages"]:
        gate = "implement stage disabled in config"
    elif kind == "none" and cfg["quota"] > 0:
        gate = "no credential secret set"
    else:
        gate = budget_gate(cfg, kind, budget_used(previous_reports(repo, labels), now))
    if gate:
        skipped.extend((i["number"], gate) for i in picked)
        picked = []

    plan = {
        "run_id": os.environ.get("GITHUB_RUN_ID", now.strftime("local-%Y%m%d%H%M%S")),
        "repo": repo, "started_at": now.strftime("%Y-%m-%d %H:%M"), "credential": kind,
        "config": cfg, "labels": labels, "picked": picked, "skipped": skipped, "gate": gate,
        "stale_prs": stale_prs(repo, cfg["stale_days"], now), "status": "running",
    }
    url = gh("issue", "create", "--repo", repo, "--label", labels["report"],
             "--title", report_title(plan), "--body", report_body(plan)).strip()
    plan["report_number"] = int(url.rstrip("/").split("/")[-1])
    for i in picked:
        gh("issue", "edit", "--repo", repo, str(i["number"]), "--add-label", labels["in_flight"])

    Path(args.out).write_text(json.dumps(plan, indent=2))
    matrix = [{"number": i["number"], "title": i["title"], "url": i["url"]} for i in picked]
    set_output("picks", json.dumps(matrix))
    set_output("count", str(len(matrix)))
    set_output("report_number", str(plan["report_number"]))
    set_output("run_triage", "true" if "triage" in cfg["stages"] and kind != "none" else "false")
    set_output("per_issue_minutes", str(cfg["per_issue_minutes"]))
    set_output("model_triage", cfg["models"]["triage"])
    set_output("model_implement", cfg["models"]["implement"])
    set_output("base_branch", cfg["base_branch"])


def cost_from_execution_file(path: str | None) -> dict:
    """claude-code-action writes an execution log; find total_cost_usd wherever it sits."""
    found: dict = {}
    if not path or not Path(path).exists():
        return found

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("total_cost_usd", "input_tokens", "output_tokens",
                         "cache_read_input_tokens", "cache_creation_input_tokens") and isinstance(v, (int, float)):
                    found[k] = found.get(k, 0) + v
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    try:
        walk(json.loads(Path(path).read_text()))
    except json.JSONDecodeError:
        pass
    return found


def cmd_result(args: argparse.Namespace) -> None:
    """Run after one Implement session: record PR, cost and outcome for one issue."""
    number = int(args.number)
    prs = gh_json("pr", "list", "--repo", args.repo, "--state", "open", "--limit", "20",
                  "--search", f"head:agent/{number}-", "--json", "number,url,isDraft")
    cost = cost_from_execution_file(args.execution_file)
    minutes = float(args.minutes) if args.minutes else None
    result = {
        "number": number, "status": "pr_opened" if prs else ("gave_up" if args.exit_code == "0" else "failed"),
        "pr": prs[0]["url"] if prs else None, "minutes": minutes,
        "usd": cost.get("total_cost_usd"), "tokens": {k: v for k, v in cost.items() if k != "total_cost_usd"},
    }
    if not prs:
        # Nothing in flight: let the next Run pick it again unless a human labels otherwise.
        gh("issue", "edit", "--repo", args.repo, str(number), "--remove-label", args.in_flight_label)
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


def cmd_finalize(args: argparse.Namespace) -> None:
    plan = json.loads(Path(args.plan).read_text())
    results = []
    for p in sorted(Path(args.results_dir).glob("**/result-*.json")):
        results.append(json.loads(p.read_text()))
    moves_file = Path(args.results_dir) / "triage" / "triage-moves.json"
    if moves_file.exists():
        plan["triage_moves"] = json.loads(moves_file.read_text())
    done = {r["number"] for r in results}
    for i in plan["picked"]:
        if i["number"] not in done:
            results.append({"number": i["number"], "status": "no result (job cancelled?)"})
    plan["status"] = "done"
    gh("issue", "edit", "--repo", plan["repo"], str(plan["report_number"]), "--body", report_body(plan, results))
    gh("issue", "close", "--repo", plan["repo"], str(plan["report_number"]))
    print(f"Run Report #{plan['report_number']} finalised with {len(results)} results")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="issue-runner")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("plan")
    a.add_argument("--repo", required=True)
    a.add_argument("--config", default=".github/issue-runner.yml")
    a.add_argument("--config-json", default=None, help="inline config (used by the local loop)")
    a.add_argument("--quota", default=None, help="override config quota")
    a.add_argument("--out", default="plan.json")
    a.set_defaults(fn=cmd_plan)

    r = sub.add_parser("result")
    r.add_argument("--repo", required=True)
    r.add_argument("--number", required=True)
    r.add_argument("--execution-file", default=None)
    r.add_argument("--minutes", default=None)
    r.add_argument("--exit-code", default="0")
    r.add_argument("--in-flight-label", default="agent:in-flight")
    r.add_argument("--out", required=True)
    r.set_defaults(fn=cmd_result)

    f = sub.add_parser("finalize")
    f.add_argument("--plan", default="plan.json")
    f.add_argument("--results-dir", default="results")
    f.set_defaults(fn=cmd_finalize)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
