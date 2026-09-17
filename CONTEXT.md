# issue-runner: domain glossary

Vocabulary only. Implementation lives in the code and the ADRs.

| Term | Meaning |
|---|---|
| **Issue** | A GitHub issue. "Ticket" is not used. |
| **Run** | One scheduled execution of the runner against one repo. |
| **Stage** | One of the two phases inside a Run: **Triage** (classify, label, brief, split) and **Implement** (fix an Issue and open a PR). A Run may enable either, both, or neither. |
| **Pick set** | The Issues eligible for the Implement stage in a Run: `ready-for-agent`, unassigned, not blocked, no verb or runner label. |
| **Pick** | An Issue the Implement stage took this Run. |
| **Quota** | Maximum number of Picks per Run. |
| **Brief** | The triage comment that is the Implement stage's contract for an Issue. |
| **Verb label** | A label only a human sets to steer the runner: `agent:skip`, `agent:retry`, `agent:hold`. |
| **Runner label** | A label only the runner sets: `agent:in-flight`, `agent-run`. |
| **In-flight** | An Issue with a live agent PR. Not picked again until the PR closes, unless it is a Continuation. |
| **Continuation** | An Issue whose session hit the time cap with work committed. The next Run resumes its PR branch in a fresh session, first in the pick order. Capped by `max_continuations`, then handed to a human. |
| **Cap** | The wall-clock limit of one session, by size label (`size:S`, `size:M`) or the default. The agent is told the cap and asked to open the PR before it. |
| **Run Report** | The GitHub issue, labelled `agent-run`, that records one Run. The only durable record of a Run. |
| **Budget gate** | The monthly cap (hours on a subscription token, dollars on an API key) past which the Implement stage does not start. |
| **Stale PR** | An agent PR idle longer than `stale_days`. Listed under "Waiting on you", never auto-closed. |
| **Credential** | What the agent authenticates to Claude with: a subscription OAuth token or an API key. One per repo, human-swappable. |
