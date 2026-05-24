# meta

The `meta/` role holds **operations about operations** — subjects whose
job is to coordinate, drive, replay, or reason about other operations
rather than perform cluster work themselves. They sit *above* the
`ops/` subjects (which act on the cluster) and compose them into
higher-level lifecycles. A campaign, for example, is a tagged loop of
submits — it never does I/O on the cluster, it just decides what
sequence of `ops/submit/` invocations to make next. That makes it
meta-level, not an `ops/` subject.

## Subjects

Each immediate subdirectory of `meta/` is a self-contained subject
following the same rules as `ops/` subjects (no sideways imports;
cross-subject helpers go through `infra/`, cross-subject primitive
calls go through `hpc_agent.runner`). Current inventory:

- **[`meta/campaign/`](campaign/README.md)** — the campaign lifecycle
  subject. Eight per-step primitives (`campaign-init`, `-list`,
  `-status`, `-advance`, `-budget`, `-converged`, `-health`,
  `-replay`), the `load-context` fresh-context bootstrap, the
  `validate-campaign` workflow that composes the four `ops/validate/*`
  atoms, and the headless `hpc-campaign-driver` console script that
  walks a campaign one step per invocation.

## Adding a new meta-subject

A new top-level operation belongs in `meta/` when it operates on
*records of operations* (e.g. a journal sweep, a campaign sibling) and
doesn't itself touch the cluster. Drop it into a new subdirectory
alongside `campaign/` with the same scaffold:

- `__init__.py` — empty (no eager re-exports; per the cross-subject
  discipline, importers reach the leaf module directly).
- `README.md` — *What + why* / *Invariant* / *Public vs internal*
  following the pattern in `campaign/README.md` and the
  `ops/<subject>/README.md` files.
- One module per `@primitive`.
- Tests mirror at `tests/meta/<subject>/`.

See `docs/architecture.md` for the broader layering rules and the
subject-imports lint that enforces them.
