<!--
ADR Template — Architecture Decision Record

Usage:
- Use this template when capturing an architecturally meaningful decision
  (design policy, ownership boundaries, replay strategy, persistence strategy,
  state-management strategy, GUI interaction model, etc.).
- Copy this file to `docs/adr/<NNNN>-<short-title>.md` (zero-padded sequential
  ID, e.g. `0007-blind-canonical-state.md`).
- One ADR per decision. Do NOT bundle multiple decisions in one file.
- Do NOT rewrite old ADRs to erase history. If the direction changes, create
  a new ADR and mark the old one `Superseded` here and in `Supersedes /
  Superseded by`.
- Cross-link related commits, tests, worklogs, and issues so future readers
  can trace the decision back to working code.
- After adding, register this ADR in `docs/decision-log.md`.
-->

# ADR-NNNN: <Title>

## Status

<!-- One of: Proposed / Accepted / Superseded / Rejected / Deprecated -->
Proposed

## Date

YYYY-MM-DD

## Context

<!--
What is the problem? What forces are at play (technical, operational,
organisational)? What constraints make this decision non-trivial?
Link to relevant `CLAUDE.md` sections, prior ADRs, or issues.
-->

## Decision

<!--
The decision itself, stated as an active sentence ("We will ...").
Be specific enough that a future contributor can implement / verify it.
-->

## Alternatives Considered

<!--
List the realistic alternatives and why they were not chosen.
This is the most important section for future readers — it preserves the
"why not" reasoning that the codebase alone cannot show.
-->

- **Alternative A** — …
  - Pros: …
  - Cons: …
  - Why rejected: …
- **Alternative B** — …

## Consequences

<!--
Positive, negative, and neutral consequences. Include operational impact,
required follow-up work, and any new constraints introduced.
-->

- Positive: …
- Negative / trade-offs: …
- Neutral / new constraints: …

## Validation / Follow-up

<!--
How will we know this decision is working? What tests, metrics, or
follow-up tasks confirm it? List any deferred work explicitly.
-->

- [ ] …
- [ ] …

## Related Files

<!-- Paths in the repo most affected by this decision. -->

- `path/to/file.py`

## Related Tests

<!-- Tests that pin this decision. -->

- `tests/test_xxx.py::TestYyy`

## Related Commits

<!-- SHA or PR links. -->

- `<commit-sha>` — short description

## Supersedes / Superseded by

<!--
If this ADR replaces an earlier one, link it here AND update the older ADR's
`Status` to `Superseded` with a back-link.
-->

- Supersedes: —
- Superseded by: —
