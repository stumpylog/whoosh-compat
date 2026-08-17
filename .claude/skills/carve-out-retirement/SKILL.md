---
name: carve-out-retirement
description: Use when a new tantivy-py or tantivy release ships, when bumping the tantivy dependency floor or pin, or when asked to remove version-specific workarounds or compatibility fallbacks
---

# Carve-out retirement

## Overview

whoosh-compat carries version-specific workarounds ("carve-outs") for upstream limitations. Each has a distinct retirement condition. **Never conflate them**: a release that satisfies one condition says nothing about the others. Confirm the fix is in the actual release changelog or diff, not merely merged upstream.

## Tracked carve-outs

| Workaround | Where | Retires when |
|---|---|---|
| JSON subpath `parse_query` fallback + `_json_paths_supported` probe | `src/whoosh_compat/emitters/tantivy_.py` | a tantivy-py release actually exposes programmatic JSON subpath terms AND the dependency floor is raised past it. See the caveats below: do NOT treat "#716 merged" as the trigger |
| `_to_naive_utc` tz-aware to naive-UTC conversion | `src/whoosh_compat/emitters/tantivy_.py` | tantivy-py release containing PR #666 (merged after 0.26.0 was tagged). Conversion is harmless post-fix; retiring it is optional, docs wording is what changes |
| All-MustNot boolean padding | `_boolean_query` in the emitter | tantivy core issue quickwit-oss/tantivy#3025 fixed in the Rust core tantivy-py vendors. Probably never; verify before touching |
| Date-range window clamp (`_TANTIVY_DATE_MIN`/`_TANTIVY_DATE_MAX`) | `visit_daterange` in the emitter | tantivy's `DateTime` stops being an i64 nanosecond count (bounds outside roughly 1677..2262 wrap modulo 2**64 ns, silently matching wrong documents; measured on 0.26.0). Probably never. Separately RE-VERIFY on every tantivy-py bump: the whole-second inward rounding of the MIN constant is only safe because tantivy-py 0.26 truncates datetimes to whole seconds on both the index and query sides; a release that keeps sub-second precision would make the rounded MIN (and symmetrically the rounded MAX) wrongly exclude a stored sub-second instant just inside the true edge, and the constants must then carry the exact ns-precision values instead |

## JSON carve-out: what the upstream situation actually is

The open PR (quickwit-oss/tantivy-py#716) is under a design discussion, not just review, so its shape is not settled:

- The tantivy-py maintainer has questioned whether the value-typing logic belongs in the Python bindings at all rather than upstream in the tantivy crate, and separately whether it should extend `Query.term_query` or arrive as a new method. Either answer changes the API this library would call.
- Reviewers proposed splitting it: binding ergonomics (accepting a dotted `field.subpath` name, exposing `expand_dots_enabled`) versus the value-typing logic, which hand-copies tantivy's private `generate_literals_for_json_object`. The halves may land separately or on different timelines.
- **A structural limitation survives no matter what lands.** Tantivy's query parser emits an OR of a fast-value term and a tokenized text term for a JSON leaf. A single `Term` cannot express that union, so a JSON string leaf whose text looks numeric, boolean, or date-like (for example the string `"5"`) stays unreachable programmatically. If hosts have such values, the `parse_query` path must survive for that case even after the rest retires.
- The programmatic path appends the raw string where the parser tokenizes. That is consistent with `term_query`'s documented contract, but it means "parity with the parser" is narrower than it sounds, and it interacts with this library's analyzer seam.

So: verify what a release actually exposes and under what name, and re-check whether the numeric-looking-string case still needs the fallback, before deleting anything.

## Retirement checklist (JSON carve-out)

0. **Before celebrating the probe flipping to `True`**: re-verify that non-string (numeric/boolean) JSON subpath values still match correctly under the programmatic branch. The `parse_query` fallback currently gives a JSON subpath term free, tantivy-native type inference (`attrs.value:100` matches both a numeric-100 document and a string-"100" document; `attrs.flag:true` matches a JSON boolean) because it hands the text to tantivy's own query-string grammar, which tries a fast-value interpretation and a tokenized-text interpretation and ORs them. The programmatic `term_query` path builds a single, explicitly `Str`-typed term with no equivalent union, so this inference does not carry over automatically; see DIVERGENCES.md entry 22's extension for the verified mechanism and the paperless-ngx custom-fields motivating case. Do not treat "the probe returns `True`" alone as sufficient to retire step 4 below without this check.
1. Confirm the feature, and the exact API it ships under, in the release notes and diff of the specific version. Do not assume it matches the open PR.
2. **Coordinate downstream first**: raising the floor breaks any consumer still on the old pin (paperless-ngx's pin is the reason the CI `tantivy-pin` job exists). Do not land a silent break; confirm the downstream pin is moving.
3. `pyproject.toml`: raise the `[tantivy]` extra floor; `uv lock --upgrade-package tantivy`.
4. Emitter: delete the probe and fallback branch, simplify `_emit_json_term` to the pure `term_query` path. Keep the `index` constructor parameter (public API; do not remove it as a drive-by).
5. Tests: delete probe-specific tests; reword tests/comments that describe the fallback (including the conftest fixture doc added for escaping round-trips; the doc stays, its comment changes).
6. Docs: README carve-out section, ARCHITECTURE extension-points bullet, emitter module docstring. `DIVERGENCES.md`'s JSON entry STAYS (design divergence, permanent, version-independent).
7. CI: delete or repurpose the `tantivy-pin` job consistent with step 2's decision.
8. Grep for stragglers: `716`, `_json_paths`, `parse_query` across src, tests, docs, CI.
9. Verify: `uv run ruff check .`, `uv run mypy src`, `uv run pytest tests --cov --cov-branch`.

## Common mistakes

- Treating a #716 release as also covering #666 or #3025. Separate conditions, verify each.
- Raising the dependency floor without the downstream coordination in step 2.
- Deleting the DIVERGENCES JSON entry. It documents a permanent design difference from whoosh, not the workaround.
