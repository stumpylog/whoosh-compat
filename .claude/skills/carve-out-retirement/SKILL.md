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
| JSON subpath `parse_query` fallback + `_json_paths_supported` probe | `src/whoosh_compat/emitters/tantivy_.py` | tantivy-py ships quickwit-oss/tantivy-py#716 AND the dependency floor is raised past that release |
| `_to_naive_utc` tz-aware to naive-UTC conversion | `src/whoosh_compat/emitters/tantivy_.py` | tantivy-py release containing PR #666 (merged after 0.26.0 was tagged). Conversion is harmless post-fix; retiring it is optional, docs wording is what changes |
| All-MustNot boolean padding | `_boolean_query` in the emitter | tantivy core issue quickwit-oss/tantivy#3025 fixed in the Rust core tantivy-py vendors. Probably never; verify before touching |

## Retirement checklist (JSON carve-out)

1. Confirm #716's feature in the release notes/diff of the specific version.
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
