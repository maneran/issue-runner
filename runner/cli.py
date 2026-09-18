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

from runner.pick import ReportTotals, budget_used, continuation_count, minutes_for, select_picks

HERE = Path(__file__).resolve().parent.parent
JSON_MARK = "<!-- issue-runner:json -->"

CANONICAL_KEYS = {
    "ready": "ready-for-agent", "skip": "agent:skip", "hold": "agent:hold",
    "in_flight": "agent:in-flight", "bug": "bug", "report": "agent-run",
    "needs_triage": "needs-triage", "cont": "agent:continue", "retry": "agent:retry",
    "human": "ready-for-human",
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
    cfg.setdefault("minutes_by_size", {"S": 20, "M": 45, "default": cfg["per_issue_minutes"]})
    cfg.setdefault("max_continuations", 3)
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
    if os.environ.get("ISSUE_RUNNER_LOCAL"):
        # Local loop: Claude Code's own login (Pro/Max), never the metered API.
        if key:
            raise SystemExit("ANTHROPIC_API_KEY is set; the local runner uses the Claude login only. Unset it.")
        return "oauth"
    if oauth and key:
        raise SystemExit("both CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY are set; pick one")
    if key:
        return "api_key"
    if oauth:
        return "oauth"
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
            reports.append({"createdAt": i["createdAt"], "totals": data.get("totals", {}),
                            "results": data.get("results", [])})
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
            toks = r.get("tokens") or {}
            if r.get("usd") is not None:
                cost = f"${r['usd']:.2f}"
            elif toks:
                cost = f"{toks.get('output_tokens', 0) // 1000}k out / {(toks.get('cache_read_input_tokens', 0) + toks.get('input_tokens', 0)) // 1_000_000}M in"
            else:
                cost = "-"
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
        "output_tokens": sum((r.get("tokens") or {}).get("output_tokens", 0) for r in results),
        "input_tokens": sum((r.get("tokens") or {}).get(k, 0) for r in results
                            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
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
    reports = previous_reports(repo, labels)
    handoffs = reconcile_verbs(repo, issues, labels, reports, cfg["max_continuations"])
    for i in issues:
        names = {l["name"] for l in i["labels"]}
        i["blocked_by"] = blocked_count(repo, i["number"]) if labels["ready"] in names else 0
    picked, skipped = select_picks(issues, cfg["quota"], labels)
    skipped.extend(handoffs)

    gate = None
    if "implement" not in cfg["stages"]:
        gate = "implement stage disabled in config"
    elif kind == "none" and cfg["quota"] > 0:
        gate = "no credential secret set"
    else:
        gate = budget_gate(cfg, kind, budget_used(reports, now))
    if gate:
        skipped.extend((i["number"], gate) for i in picked)
        picked = []
    for i in picked:
        names = {l["name"] for l in i["labels"]}
        i["minutes"] = minutes_for(i, cfg["minutes_by_size"])
        i["continue"] = labels["cont"] in names
        i["pr"] = agent_pr(repo, i["number"]) if i["continue"] else None

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
    matrix = [{"number": i["number"], "title": i["title"], "url": i["url"], "minutes": i["minutes"],
               "continue": i["continue"], "pr": i["pr"]} for i in picked]
    set_output("picks", json.dumps(matrix))
    set_output("count", str(len(matrix)))
    set_output("report_number", str(plan["report_number"]))
    set_output("run_triage", "true" if "triage" in cfg["stages"] and kind != "none" else "false")
    set_output("per_issue_minutes", str(cfg["per_issue_minutes"]))
    set_output("model_triage", cfg["models"]["triage"])
    set_output("model_implement", cfg["models"]["implement"])
    set_output("base_branch", cfg["base_branch"])


def agent_pr(repo: str, number: int, state: str = "open") -> dict | None:
    prs = gh_json("pr", "list", "--repo", repo, "--state", state, "--limit", "5",
                  "--search", f"head:agent/{number}-", "--json", "number,url,isDraft,headRefName")
    return prs[0] if prs else None


def reconcile_verbs(repo: str, issues: list[dict], labels: dict, reports: list[dict],
                    max_continuations: int) -> list[tuple[int, str]]:
    """Act on human verbs and stale runner labels before picking. Mutates `issues`
    labels in place so the pick logic sees the new state. Returns hand-offs to
    list under Skipped."""
    handoffs: list[tuple[int, str]] = []
    for i in issues:
        names = {l["name"] for l in i["labels"]}
        n = i["number"]

        if labels["retry"] in names:
            pr = agent_pr(repo, n)
            if pr:
                gh("pr", "close", "--repo", repo, str(pr["number"]), "--delete-branch",
                   "--comment", "> *issue-runner:* closed on `agent:retry`; the next Run starts over.")
            for lab in (labels["retry"], labels["in_flight"], labels["cont"]):
                if lab in names:
                    gh("issue", "edit", "--repo", repo, str(n), "--remove-label", lab)
                    names.discard(lab)

        elif labels["in_flight"] in names and not agent_pr(repo, n):
            # The PR was merged or closed by a human; the runner label is stale.
            for lab in (labels["in_flight"], labels["cont"]):
                if lab in names:
                    gh("issue", "edit", "--repo", repo, str(n), "--remove-label", lab)
                    names.discard(lab)
            merged = agent_pr(repo, n, state="merged")
            if merged:
                # GitHub closes on "Closes #n" only when the PR lands on the default branch.
                # A merge anywhere else would leave the issue ready-for-agent and picked again.
                gh("issue", "close", "--repo", repo, str(n), "--comment",
                   f"> *issue-runner:* closed because {merged['url']} was merged.")
                handoffs.append((n, f"closed: agent PR merged ({merged['url']})"))
                names.discard(labels["ready"])

        if labels["cont"] in names and continuation_count(reports, n) >= max_continuations:
            gh("issue", "comment", "--repo", repo, str(n), "--body",
               f"> *This was generated by AI during implementation.*\n\nThe agent hit its time cap "
               f"{max_continuations} times on this issue. Handing it to a human; the WIP draft PR stays open.")
            gh("issue", "edit", "--repo", repo, str(n), "--remove-label", labels["cont"],
               "--remove-label", labels["ready"], "--add-label", labels["human"])
            names -= {labels["cont"], labels["ready"]}
            names.add(labels["human"])
            handoffs.append((n, f"handed to human after {max_continuations} continuations"))

        i["labels"] = [{"name": x} for x in names]
    return handoffs


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
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError:
        return found
    walk(data)
    if isinstance(data, dict) and data.get("cost_source"):
        found["cost_source"] = data["cost_source"]
    return found


def cmd_result(args: argparse.Namespace) -> None:
    """Run after one Implement session: record PR, cost and outcome for one issue."""
    number = int(args.number)
    prs = gh_json("pr", "list", "--repo", args.repo, "--state", "open", "--limit", "20",
                  "--search", f"head:agent/{number}-", "--json", "number,url,isDraft")
    cost = cost_from_execution_file(args.execution_file)
    minutes = float(args.minutes) if args.minutes else None
    if prs and args.timed_out:
        status = "continued"
    elif prs:
        status = "pr_opened"
    else:
        status = "gave_up" if args.exit_code == "0" else "failed"
    result = {
        "number": number, "status": status,
        "pr": prs[0]["url"] if prs else None, "minutes": minutes,
        "usd": cost.get("total_cost_usd"), "cost_source": cost.get("cost_source", "session"),
        "tokens": {k: v for k, v in cost.items() if k not in ("total_cost_usd", "cost_source")},
    }
    if status == "continued":
        gh("issue", "edit", "--repo", args.repo, str(number), "--add-label", args.continue_label)
    elif prs:
        gh("issue", "edit", "--repo", args.repo, str(number), "--remove-label", args.continue_label)
    else:
        # Nothing in flight: let the next Run pick it again unless a human labels otherwise.
        gh("issue", "edit", "--repo", args.repo, str(number), "--remove-label", args.in_flight_label,
           "--remove-label", args.continue_label)
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
    r.add_argument("--continue-label", default="agent:continue")
    r.add_argument("--timed-out", action="store_true")
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
