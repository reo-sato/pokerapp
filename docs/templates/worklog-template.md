<!--
Worklog Template

Usage:
- Use this template for any non-trivial change (one file per task / phase /
  PR-sized change). Do NOT append unrelated work into a single worklog.
- Copy this file to `docs/worklog/YYYY-MM-DD-<short-slug>.md`.
- Record expected vs. implemented behavior separately so post-hoc readers
  can see where reality diverged from intent.
- Cross-link related ADRs, issue logs, commits, and tests.
- A worklog is part of the deliverable: the task is not complete until this
  entry is filled in.
-->

# Worklog: <Title>

## Date

YYYY-MM-DD

## Scope / Task

<!-- One sentence: what this work covers. Reference the phase / PR / issue. -->

## Goal

<!-- What outcome should this work achieve? What does "done" look like? -->

## Changed Files

<!-- Paths and a one-line description of the change per file. -->

- `path/to/file.py` — …

## Expected Behavior

<!--
What the change was supposed to do, written BEFORE / independent of what
was actually implemented. Bullet the user-visible and developer-visible
behaviors separately if useful.
-->

## Implemented Behavior

<!--
What the change actually does. If this differs from "Expected Behavior",
make the divergence explicit and explain it in "Mismatches Found During
Testing" below.
-->

## Test Results

<!--
Commands run and their outcomes. Paste the relevant summary line
(e.g. `pytest ... → 654 passed`). Include manual / GUI smoke tests too.
-->

- `pytest tests/ -v --ignore=tests/test_vision.py` — …
- Manual: …

## Mismatches Found During Testing

<!--
List any divergence between expected and implemented behavior surfaced by
testing. If none, write "None observed.". If something was found, link to
or create an entry under `docs/issues/`.
-->

## Fixes Applied

<!--
Concrete fixes made in response to the mismatches above. One bullet per
fix with a short rationale.
-->

## Remaining Gaps / Out-of-Scope

<!--
Known shortcomings, deferred work, follow-up tasks. Be explicit so they
don't get lost.
-->

- [ ] …

## Related ADRs

- `docs/adr/NNNN-...md` — …

## Related Issues

- `docs/issues/NNNN-...md` — …

## Related Commits

- `<commit-sha>` — …
