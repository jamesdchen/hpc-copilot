# CLAUDE.md

Judgment rules that no lint can enforce — "verify a guard can actually fire"
and the library-knowledge boundary test — live in full in the index page
[`docs/internals/engineering-principles.md`](docs/internals/engineering-principles.md),
which also indexes the per-section enforcement maps split under
[`docs/internals/principles/`](docs/internals/principles/) — each a
self-contained prose + enforcement-map + drift-log unit whose lint/test names
the source of truth for every mechanized line. The index's section listing is
GENERATED from the section files (regen via `python scripts/regen_all.py
--write`). Read the index before classifying anything as intentional, removing
an apparent duplication, or adding third-party-library knowledge to core; then
read the specific section you touch before changing an enforcement-mapped line.
Keep this file a pointer: facts restated here rot (that page's drift log records
how).
