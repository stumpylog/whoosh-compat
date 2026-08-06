---
name: differential-triage
description: Use when a test under tests/differential/ fails, a corpus line mismatches the whoosh oracle, a query parses differently in whoosh-compat than in real whoosh, or you must decide whether whoosh-compat should match a whoosh behavior
---

# Differential mismatch triage

## Overview

The differential harness (`tests/differential/`) parses a corpus of query strings through both whoosh-compat and real whoosh (the pinned oracle) and compares ASTs. A mismatch is never silently fixed or skipped: classify it first, then act.

**Parity bar: whoosh's intended semantics, not its defects.** Never change whoosh-compat to reproduce a confirmed whoosh bug (policy stated in `DIVERGENCES.md`'s header and `tests/differential/allowlist.py`'s docstring).

## Triage flow

1. **Reproduce outside pytest.** Parse the query on both sides directly (oracle helpers are in `tests/differential/oracle.py`; note the oracle's `DateParserPlugin` setup differs from a default whoosh parser, so reproduce through `oracle_parse`, not a hand-built parser).
2. **Classify** the mismatch:
   - **whoosh-compat defect**: fix the code, add a regression test. No allowlist entry.
   - **whoosh bug** (oracle is wrong; verify against whoosh source and cite the exact function): keep whoosh-compat's behavior, document per step 3.
   - **design divergence or out-of-scope**: document per step 3.
3. **Document every non-fix classification.** All three pieces, not just one:
   - Allowlist entry in `tests/differential/allowlist.py`: anchored regex scoped as narrowly as possible, reason string citing `DIVERGENCES.md entry N` with the right prefix (`whoosh-bug` / `design` / `out-of-scope`). Match the format of existing entries.
   - `DIVERGENCES.md` entry: next number, bold one-line summary with the classification tag, mechanism prose citing exact source locations, closing "Test references:" paragraph.
   - Keep the corpus line. The harness must keep exercising the pattern and report it as an explained skip. Deleting or watering down the line to make tests pass defeats the harness.
4. **Cover it outside the harness too**: a direct parametrized unit test (`pytest.param(..., id="...")`) where feasible, and check whether the divergence is AST-level only or also result-level (acceptance suite `tests/emitter/test_acceptance_e2e.py`; see DIVERGENCES.md's entry on AST-level vs result-level divergence and extend that entry's list either way).
5. **Verify**: `uv run pytest tests/differential -rs` (the new skip reason must be visible and attributed), then the full suite.

## Common mistakes

- "Fixing" whoosh-compat to match a whoosh defect. Check the parity bar first.
- Allowlist regex broader than the pattern it excuses (silently swallows unrelated corpus lines). Anchor and scope it.
- Adding the allowlist entry but no DIVERGENCES entry, or vice versa. Both, plus the corpus line, always.
