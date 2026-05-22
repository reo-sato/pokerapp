<!--
Issue / Mismatch Log Template

Usage:
- Use this template whenever testing (automated or manual) reveals a
  mismatch between expected and actual behavior, or when a defect is found
  in shipped code.
- Copy this file to `docs/issues/NNNN-<short-slug>.md` (zero-padded
  sequential ID).
- One issue per file. Keep the file even after the issue is fixed — it
  becomes a permanent reference asset for "why this regression test
  exists" and "what was the root cause".
- Cross-link the worklog where the mismatch was observed, the ADR (if any)
  that the issue led to, the fix commit, and the regression test.
- Register significant issues in `docs/decision-log.md`.
-->

# Issue NNNN: <Title>

## Date

YYYY-MM-DD

## Status

<!-- One of: Open / Investigating / Fixed / WontFix / Duplicate -->
Open

## Severity / Priority

<!--
Severity: Blocker / High / Medium / Low
Priority: P0 / P1 / P2 / P3
-->

- Severity: …
- Priority: …

## Area

<!-- e.g. audio / rfid / integration / gui / settlement / reconstruct / docs -->

## Expected Behavior

<!-- What should have happened, with a pointer to the spec / ADR / CLAUDE.md section. -->

## Actual Behavior

<!-- What actually happened. Include exact error messages or output if relevant. -->

## Reproduction

<!--
Minimum steps to reproduce. Include commands, inputs, config, environment.
A failing test snippet is ideal.
-->

1. …
2. …
3. …

## Root Cause

<!-- Once known, the underlying reason. Be precise — link to file:line if possible. -->

## Fix

<!--
Describe the fix applied (or proposed). Reference the commit and the files
changed.
-->

## Regression Test

<!--
The test that now pins this behavior so the bug cannot return silently.
Reference test file and test name.
-->

- `tests/test_xxx.py::TestYyy::test_zzz`

## Affected Files

- `path/to/file.py`

## Related Worklog

- `docs/worklog/YYYY-MM-DD-<slug>.md`

## Related ADRs

- `docs/adr/NNNN-...md`

## Related Commits

- `<commit-sha>` — …

## Notes

<!-- Any additional context: similar prior issues, follow-up risks, etc. -->
