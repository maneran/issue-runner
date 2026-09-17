# 0001. GitHub is the only state store

Date: 2026-09-17
Status: accepted

## Context

The runner needs history: what each Run picked, skipped, opened, and spent, plus the current state
of every Issue it touches. It serves many repos owned by one admin, and a dashboard reads that history.

Alternatives considered:

1. A Postgres ledger on Railway with its own API and auth.
2. A JSON-lines ledger committed to a branch in each repo.
3. GitHub itself: labels for state, comments for the per-issue trail, one Run Report issue per Run
   holding a machine-readable JSON block.

## Decision

Option 3. Labels are the state machine, the Run Report issue is the durable record, the dashboard is
a static page that reads the GitHub API in the browser. No database, no service, no second truth.

## Consequences

- One truth. A human editing a label on GitHub is the same operation the dashboard performs; nothing
  can drift.
- Actions logs expire, so anything worth keeping must land in the Run Report JSON block at finalize.
- Querying history is `gh issue list --label agent-run` and parsing the JSON block. Aggregations
  (cost per month) are computed on read; fine at tens of Runs a month per repo.
- Private repos stay private: the dashboard never copies data to a host.
- Reversal cost: moving to a database later means backfilling from Run Report issues, which is
  possible because the JSON block is complete, but every consumer of labels-as-state must change.
