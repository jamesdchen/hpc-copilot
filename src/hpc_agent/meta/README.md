# meta

The `meta/` role holds **operations about operations** — subjects whose
job is to coordinate, drive, replay, or reason about other operations
rather than perform cluster work themselves. They sit *above* the
`ops/` subjects (which act on the cluster) and compose them into
higher-level lifecycles. A campaign, for example, is a tagged loop of
submits — it never does I/O on the cluster, it just decides what
sequence of `ops/submit/` invocations to make next. That makes it
meta-level, not an `ops/` subject.
