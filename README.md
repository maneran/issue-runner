# issue-runner

Daily GitHub issue triage and implementation with Claude Code, human in the loop. One public repo of
tooling; each target repo adds a 20-line caller workflow and a config file. GitHub is the only state
store (see `docs/adr/0001`). Vocabulary: `CONTEXT.md`.

## What a Run does

1. **Plan**: sync labels from `labels.yml`, list open issues, build the pick set, check the budget
   gate, open a Run Report issue, mark picks `agent:in-flight`.
2. **Triage** (Sonnet 5 by default): label unlabeled and `needs-triage` issues, write briefs, split
   `size:L` into sub-issues.
3. **Implement** (Opus 5 by default): one fresh session per pick, capped by `per_issue_minutes`,
   ends in a draft PR or a written blocker.
4. **Report**: fill the Run Report with results, PRs, minutes and cost; close it.

## Onboard a repo

1. Copy `examples/caller.yml` to `.github/workflows/issue-runner.yml` and set the cron.
2. Copy `examples/issue-runner.yml` to `.github/issue-runner.yml` and set quota, stages, budget.
3. Add one credential secret: `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, Pro/Max plan)
   or `ANTHROPIC_API_KEY`. Both set is an error. Swap by changing the secret and the matching
   `budget:` line.
4. Optional but recommended: a GitHub App installed on the repo, secrets `APP_ID` and
   `APP_PRIVATE_KEY`. Without it, agent PRs get no CI (PRs opened by `github-actions[bot]` do not
   trigger workflows).
5. `gh workflow run issue-runner -f quota=0` opens a Run Report with zero picks. That is the smoke test.

## Admin verbs

Merge is always yours. On an issue: `agent:skip` (ignore), `agent:retry` (redo from scratch),
`agent:hold` (keep the PR, stay quiet). On a PR: mention `@claude` with what to change.

## Local

```
python3 -m venv .venv && .venv/bin/pip install pytest pyyaml
.venv/bin/pytest
GH_TOKEN=... PYTHONPATH=. .venv/bin/python -m runner.cli plan --repo owner/name --config path/to/issue-runner.yml --quota 0
```

## Not built yet

- `agent:retry` handler (close PR, delete branch, clear in-flight) and the `pull_request: closed`
  hook that clears `agent:in-flight`.
- Flip draft PR to ready when CI is green.
- `@claude` mention workflow (claude-code-action's default `issue_comment` trigger; add as a second
  caller workflow).
- Dashboard (static SPA on GitHub Pages).
- Verify `claude-code-action@v1` input names and the execution-file schema on first real run.
