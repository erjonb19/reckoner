# ADR 0002 — The storage seam, and why there is no backend class

- **Status:** Accepted
- **Date:** 2026-09-09

## Context

The architecture rules say ADLS Gen2 is the authoritative store and Fabric reads it through
OneLake shortcuts — Fabric is compute and presentation, never the only copy. Today
everything runs against local Parquet, because Fabric trial activation depends on an
unresolved tenant question.

The Phase 3 brief asks for "a `FabricBackend` alongside the existing local DuckDB path".
There is no DuckDB path. There is no DuckDB dependency anywhere in the project, and never
has been. Storage is PyArrow datasets over Parquet files, and every read funnels through
two functions:

- `open_curated(root)` — `src/reconcile/curated.py:89`
- `open_payer_dataset(files)` — `src/payer/curated.py:331`

Both take paths and return a `pyarrow.dataset.Dataset`. Everything downstream — the
comparability layer, the variance mart, the range comparison, the CLI — consumes that
`Dataset` and knows nothing about where the bytes came from.

## Decision

**Move the two entry points to accept an optional `pyarrow.fs.FileSystem`, and add a small
resolver that decides which filesystem and root a run uses. Do not introduce a backend
class hierarchy.**

PyArrow already abstracts local versus cloud storage. `ds.dataset(path, filesystem=fs)`
reads local files with `LocalFileSystem` and ADLS Gen2 with `AzureFileSystem`, returning the
same `Dataset` either way. The abstraction the brief asks for exists one layer down and is
already a dependency.

So the cloud path needs a *filesystem and root resolver*, not a parallel implementation:

```
src/storage/
    __init__.py     resolve(name) -> (root: str, filesystem: fs.FileSystem)
                    reads from config/env; defaults to local
```

## Alternatives considered

**A `Backend` protocol with `LocalBackend` and `FabricBackend`.** This is what the brief
describes. Rejected because it would define an interface whose two implementations differ
only in the filesystem object they pass to the same PyArrow call — the classes would be
empty ceremony around a one-line difference. It also doubles the surface under test:
`FabricBackend` cannot be exercised without cloud credentials, so in practice one
implementation would be tested and the other would be hoped for.

**Wait for Fabric before designing anything.** Rejected because the seam costs almost
nothing now and the tenant question may not clear inside the trial window. Threading an
optional `filesystem` parameter through two functions is cheap; retrofitting it through the
mart, the CLI and the tests later is not.

**Copy to ADLS and read with a separate code path.** Rejected: two code paths over the same
logical dataset is how the numbers in two places stop agreeing.

## Consequences

- One code path stays under test. The local suite exercises the same `ds.dataset` call the
  cloud will use; only the filesystem differs.
- The Fabric decision stays reversible. If the trial never activates, nothing was built
  that has to be torn out — the resolver defaults to local and the parameter is unused.
- Credentials are a config concern, not a code one: resolved from environment per CLAUDE.md
  ("secrets via environment, never in code"), never passed as literals.
- `open_payer_dataset` takes a list of file paths rather than a root, so it needs the
  filesystem threaded alongside the `PayerFile` list rather than derived from a single root.
  That asymmetry is inherent to the payer side being a set of files rather than a
  partitioned tree.
- This ADR does not commit to Fabric. It commits to not making Fabric expensive to adopt.
