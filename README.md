# issue-runner

Daily GitHub issue triage and implementation with Claude Code, human in the loop. Runs locally on the
admin's Mac under their own Claude Code login (launchd, one control file `repos.yml`). GitHub is the
only state store (see `docs/adr/0001`). Vocabulary: `CONTEXT.md`. A GitHub Actions variant exists
(`.github/workflows/run.yml`, `examples/`) for repos whose tests can run on a hosted runner; it is
secondary and untested past quota 0.

## What a Run does

1. **Plan**: sync labels from `labels.yml`, list open issues, build the pick set, check the budget
   gate, open a Run Report issue, mark picks `agent:in-flight`.
2. **Triage** (Sonnet 5 by default): label unlabeled and `needs-triage` issues, write briefs, split
   `size:L` into sub-issues.
3. **Implement** (Opus 5 by default): one fresh session per pick, capped by `per_issue_minutes`,
   ends in a draft PR or a written blocker.
4. **Report**: fill the Run Report with results, PRs, minutes and cost; close it.

## Local setup (primary)

```
python3 -m venv .venv && .venv/bin/pip install pytest pyyaml
cp repos.example.yml repos.yml          # add repos, set schedule, stages, quota, budget
PYTHONPATH=. .venv/bin/python -m runner.loop --quota 0     # smoke: labels + empty Run Report
scripts/install-launchd.sh              # daily job at repos.yml schedule; prints run-now/remove
```

Each Claude session runs `claude -p` in a throwaway git worktree under `~/.issue-runner/worktrees`,
never in the admin's checkout, with the tool allow-list from the config. Logs per Run in
`~/Library/Logs/issue-runner/<stamp>/`, one JSON per session with `total_cost_usd` and usage.

Credential is the Claude Code login (Pro/Max), and only that: the loop strips `ANTHROPIC_API_KEY` and
`ANTHROPIC_AUTH_TOKEN` from every session's environment and refuses to start if a key is exported.
Reports show tokens and minutes; the hours budget is the gate. `show_api_equivalent: true` adds an
informational list-price figure computed from the transcript, never a charge.

## Onboard a repo on GitHub Actions (secondary)

1. Copy `examples/caller.yml` to `.github/workflows/issue-runner.yml` and set the cron.
2. Copy `examples/issue-runner.yml` to `.github/issue-runner.yml` and set quota, stages, budget.
3. Add one credential secret: `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, Pro/Max plan)
   or `ANTHROPIC_API_KEY`. Both set is an error. Swap by changing the secret and the matching
   `budget:` line.
4. Optional but recommended: a GitHub App installed on the repo, secrets `APP_ID` and
   `APP_PRIVATE_KEY`. Without it, agent PRs get no CI (PRs opened by `github-actions[bot]` do not
   trigger workflows).
5. `gh workflow run issue-runner -f quota=0` opens a Run Report with zero picks. That is the smoke test.

## Time caps and continuation

One session is capped by size label (`minutes_by_size`, default 45). The prompt states the cap and asks
for a draft PR before it. A session killed at the cap with a PR open is a **continuation**: the issue
gets `agent:continue`, and the next Run resumes that branch first in the pick order, in a fresh session
reading the PR's "what is left". After `max_continuations` the issue goes `ready-for-human` with the WIP
PR attached. Tokens for a killed session are summed from Claude Code's transcript.

## Admin verbs

Merge is always yours. On an issue: `agent:skip` (ignore), `agent:retry` (redo from scratch),
`agent:hold` (keep the PR, stay quiet). On a PR: mention `@claude` with what to change.

## Tests

`.venv/bin/pytest`

## Not built yet

- Flip draft PR to ready when CI is green.
- Free-text follow-ups on a PR: locally this needs a verb (`agent:revise`) the next Run acts on, or
  the `@claude` mention workflow on Actions.
- Dashboard (static SPA on GitHub Pages).
- Verify `claude-code-action@v1` input names and the execution-file schema on first real run.
