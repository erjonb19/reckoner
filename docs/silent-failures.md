# Silent failures

Every entry here is something that **reported success and was wrong**. That is the
only kind of bug this file collects. A crash announces itself and gets fixed in an
hour; a green run with bad output gets believed, built on, and found weeks later by
someone reconciling a number that will not reconcile.

The pattern is consistent enough to be worth naming: a tool accepts input it cannot
honour, does something adjacent to what was asked, and returns zero. Nothing in the
exit code, the logs, or the output distinguishes it from the correct outcome.

Each entry ends with **the check that now catches it**, because a postmortem without
one is just a story.

---

## 1. `--args` collapses a command line into a single argument

**Symptom.** `az containerapp job create --args="--stage manifest --dry-run"` was
accepted, the job was created, and `az containerapp job show` reported it healthy.
The first execution failed inside the container on an argparse error about an
unrecognised argument — one long string where four were meant.

**Cause.** The CLI stores the value as one element. `job update --args` is worse: it
refuses multiple values outright. Neither failure appears until a replica runs.

**Caught by.** `deploy/job.scheduled.yaml` declares `args` as a YAML list, and the
job is created from the file rather than from flags. The file is committed, so the
form is reviewable instead of living in one person's shell history.

---

## 2. A managed identity cannot be assigned from the job YAML

**Symptom.** A job created from YAML with an `identity:` block, or with
`--mi-user-assigned` passed alongside `--yaml`, reports `Succeeded`. Its
`identity.type` is `None`.

**Cause.** The flag is ignored with a warning when `--yaml` is present; the YAML
block is rejected by `job update` with "Request requires identities to be assigned".
Both leave a job that looks complete. The failure would first surface as an auth
error against ADLS on the 1st of the month, with nothing before it to notice.

**Caught by.** Identity assignment is a separate, verified step, documented in the
header of `deploy/job.scheduled.yaml`:
`az containerapp job show ... --query identity.type` must read `UserAssigned`.

---

## 3. pyarrow's Azure credential chain shells out to a CLI the container lacks

**Symptom.** The first execution that actually read ADLS failed with
`Failed to get token from DefaultAzureCredential`, preceded by
`/bin/sh: 1: az: not found`. Everything was configured correctly: identity attached,
`AZURE_CLIENT_ID` matching, **Storage Blob Data Contributor** granted.

**The silent half.** The execution before it reported `Succeeded` — because the stage
was still a `stage_not_implemented` placeholder. Nothing had ever authenticated, so a
green run proved only that the container started.

**Cause.** `AzureFileSystem` built with an account name alone uses the Azure **C++**
SDK's `DefaultAzureCredential` — bundled inside pyarrow, not the Python
`azure-identity` in the image. Its chain tries the environment, then the Azure CLI,
then gives up.

**Caught by.** `storage.resolve()` passes `client_id`, selecting
`ManagedIdentityCredential` explicitly.
`test_a_named_identity_is_passed_through_to_the_filesystem` captures the constructor
kwargs — the failure mode is an argument that silently is not passed, so asserting on
a live account would not have caught it — and its pair asserts `client_id` is
**absent**, not empty, when unset, so the laptop path is not narrowed.

---

## 4. `write_dataset` drops a partition field the schema does not have

**Symptom.** Publishing with `partitioning=[hospital_slug, code_type, vintage]`
against data lacking `code_type` produced a two-level tree and returned normally.
Row counts verified. The layout was simply not the one requested.

**Cause.** pyarrow does not object to a partition field absent from the write schema.
It writes what it can.

**Why it matters.** A silver copy keyed on two of three columns is not a smaller
mistake than a failed write. It is the same wrong layout with nothing to say so, and
every query written against it would return correct-looking answers over the wrong
partitions.

**Caught by.** `publish_hospital` checks `PARTITION_KEYS` against the write schema
and raises before writing.
`test_a_missing_partition_column_raises_rather_than_flattening`.

---

## 5. `str.replace` returns the string unchanged when nothing matches

**Symptom.** A `--all` flag was added to `storage.publish`. Running it called
`publish_hospital(None)` and died with "no curated rows for None". The `--all` branch
had never been inserted — the edit that was supposed to add it did nothing.

**Cause.** Python's `str.replace` is not an assertion. When the pattern is absent it
returns the original, the script exits 0, and the linter and type checker both pass
because the file is still valid code. Every gate was green over an edit that had not
happened.

**Why it is in this file.** This one is about the tooling used to write the other
fixes, which makes it the most expensive kind: it can silently undo any of them.

**Caught by.** Scripted edits now `assert` the anchor is present before replacing, so
a stale pattern fails loudly. And the behaviour itself has tests: `TestTheCommandLine`
covers `--all`, `--hospital`, and neither, including that the free-tier check prints
*before* the first write.

---

## 6. `write_dataset` defaults to snappy against a zstd source

**Symptom.** Hospital silver published, verified, every row count matching — at
**6.2 GB from a 3.5 GB source**. Nothing was wrong with the data.

**Cause.** The curated lake is zstd. `write_dataset`'s default is snappy. Same rows,
same layout, weaker codec.

**Why it counts as silent.** A verified write with correct row counts is exactly what
a correct run looks like. The only signal was a number in a manifest that nobody had
a reason to compare against the source's size — and the pre-write projection, which
was computed from local file sizes and so described the source rather than the
destination.

**Caught by.** `COMPRESSION = "zstd"` in `storage/publish.py`, pinned by
`test_it_is_written_with_the_codec_the_source_uses`. Re-publishing recovered 2.45 GB.

---

## 7. A bare `except` made every diagnostic failure look identical

**Symptom.** The Log Analytics cap probe reported `"log_ingestion_status": "unknown"`
on a run that was otherwise correct, with no indication why.

**Cause.** `except Exception: return None` collapsed a missing library, a refused
token, a network error, and a workspace with no cap configured into one word. In this
case it was `azure-identity` missing from the venv — which was itself a second silent
failure, since the package was pinned only in the Dockerfile, so the image and the
package could drift and every test covering a cloud path would skip in CI while
passing on a laptop that happened to have it.

**Why it matters.** `log_cap_hit` would have read `false` forever. The signal that
exists to stop a capped day looking like a quiet day would itself have failed
quietly.

**Caught by.** `CapProbe` carries a `detail` alongside the status, surfaced as
`log_probe_detail` on every summary record. `azure-identity` is a declared
dependency. `tests/pipeline/test_cap.py` covers each failure path by name, and the
probe still never raises — a diagnostic about the telemetry channel must not be able
to break the thing it monitors.

---

## 8. A job ran a stale image and reported success having checked less

**Symptom.** A manual execution finished `Succeeded`, exit code 0, every record
`matches=True`. It had checked **two of three layers**. The third — payer silver,
merged minutes earlier — was simply absent from the telemetry, and nothing in the
run said so.

**Cause.** Two failures stacked.

The process one: `gh run list --workflow=image.yml --limit 1` immediately after a
merge returns the *previous* build, because the new workflow run does not exist yet.
Watching it succeed and starting the job produced an execution at 12:24:20Z against
an image that finished pushing at 12:25:03Z — the job started 43 seconds before its
own code existed, and faithfully ran what `:latest` pointed at.

The design one, which is the real defect: **the job could not say what code it was
running.** `job_start` logged the Python and pyarrow versions but nothing about the
image, so a stale build was indistinguishable from a current one. The discrepancy was
only found by noticing a missing record twenty minutes later.

**Why it is the worst shape in this file.** Every other entry is caught by a check
that now exists. This one could silently defeat those checks: the guards are in the
image, so an execution running an older image runs an older set of guards while
reporting the same green.

**Caught by.** The commit SHA is baked into the image at build time
(`ARG BUILD_SHA` / `RECKONER_BUILD_SHA`, passed from `github.sha`) and logged on
`job_start`, so every execution names the code it ran. The stage also logs
`manifest_layers` with the layer names and count before checking any of them, so
coverage is stated rather than inferred from which records turned up
(`test_the_run_states_which_layers_it_covered`). And an image build is now waited on
by matching `headSha` to the merge commit, not by taking the newest run.

---

## 9. A tested feature the caller never switched on

**Symptom.** The first full run of stage 2 reported success. Four systems
reconciled, gold was written, the manifest verified. **Two of the four had
produced nothing at all** — NYU Langone and NewYork-Presbyterian, zero pairs
each, and the run was green.

**Cause.** Neither publishes a billing class. The comparability layer refuses an
unstated class rather than treating it as compatible with anything, so every
candidate was excluded: **136,259,228** of them for NYU Langone on that one
reason, 2,289,594 for NYP.

The relaxation for exactly this case already existed. It had been designed,
scoped mechanically from the data, given a note on every pair it touches, and
covered by tests including one holding that Maimonides must still be refused
because it publishes 121,119 professional rows. All of that was correct. It is
opt-in, and the new stage simply never opted in.

**Why it is the hardest kind to see.** Every other entry here is something
behaving differently from how it reads. This one behaved exactly as written. No
exception, no warning, no degraded exit code — two systems reported zero, which
is a number a reconciliation can legitimately produce, and nothing distinguished
"nothing to compare" from "never asked". It was found only by reading the
per-system figures and noticing that two of them were implausible.

A feature that is tested but unwired is worth nothing, and it looks from the
inside exactly like a feature that is wired and finding nothing.

**Caught by.** The stage computes eligibility from the lake once per run and
applies the assumption where the data says it may. `coverage` carries
`assumed_facility_when_unstated` per system, so a reader can see which numbers
depend on it rather than inferring it from their size, and
`test_coverage_records_whether_the_facility_assumption_applied` pins that the
flag travels with the figures.

Mount Sinai is the control that makes the flag trustworthy: eligible, assumption
applied, numbers **unchanged** at 3,370,446 pairs — because it had no
`billing_class_unstated` refusals to relax. A flag that changed every system's
numbers would not have told us whether it was doing the right thing.

---

## 10. A gauge that read zero, correctly, about the wrong thing

**Symptom.** Every slice of the mart stage logged `arrow_pool_mib: 0` while the
container's resident memory climbed to 7.1 GB and was killed. The obvious reading
— Arrow is not involved, the memory must be Python's — was wrong, and it steered
**four** structural changes that each moved the number without fixing anything.

**Cause.** The gauge called `pyarrow.total_allocated_bytes()`, which returns what
Arrow holds **at that instant**. It was sampled at the end of each slice, after
every table had been released. It was never going to return anything but zero.

The number was correct. It answered a question nobody had asked. Arrow's peak
during the slice was over a gigabyte, and the call that reports it is
`pool.max_memory()` — a high-water mark, which is what "did this use a lot of
memory" means when the thing being measured is transient.

**Why it is the most expensive entry here.** The other nine describe something
behaving differently from how it reads. This one describes an instrument working
exactly as designed and being believed about a claim it was never making. It is
worse than having no instrument, because no instrument would have prompted a
profile on day one. A wrong reading from a working gauge does not look like
missing information; it looks like an answer.

Three of the four changes it prompted were kept, because they turned out to be
correct for other reasons — the carrier and facility splits are exact and were
verified row-for-row. That is luck, not vindication. They were made to fix
something they had nothing to do with.

**Caught by.** `arrow_memory()` returns live bytes, the high-water mark, **and**
the allocator's backend name, and the slice record logs all of them. The backend
name is there for the same class of reason: `ARROW_DEFAULT_MEMORY_POOL` is read at
import and ignored in silence when the backend is not compiled in, so a
configuration change that did nothing would otherwise be indistinguishable from
one that worked.

The profile that settled it is kept as `scripts/profile_mart_slice.py` and
`scripts/profile_mart_load.py`, so the next memory question starts with a
measurement rather than four guesses.

**The rule.** A gauge reading zero is a claim about the instrument as much as
about the system. Before believing it, ask what it measures and when it was
sampled — and prefer the high-water mark for anything transient.

---

## Earlier, same family

Two from before this file existed, kept because they are the same shape:

- **The wheel omitted the job entrypoint.** `reckoner_job.py` is a top-level module,
  not a package, so `packages.find` did not see it. The image built cleanly and would
  have died at run time with `ModuleNotFoundError`. Found by building the wheel and
  inspecting its contents rather than trusting the build. Fixed with
  `py-modules = ["reckoner_job"]` in `pyproject.toml`, which carries the reason.

- **`--mode pairs` produced zero pairs for four merged PRs.** A refactor left
  `facilities=None`, removing the system→facility bridge. The command ran, exited 0,
  and printed an empty mart — which is also what a legitimately empty result looks
  like, so nothing distinguished them.

---

## The rule this file argues for

When a tool can accept input it cannot honour, assume it will, and check the outcome
rather than the return code. Concretely, in this repo:

- Verify **what landed**, not what was sent — row counts from Parquet footers, tree
  shape from the paths, identity from `az ... --query identity.type`.
- A diagnostic that cannot answer must say **why**, never just "unknown".
- A check that could not run has **not** found the data clean; it fails.
- An exit code is the signal people notice first, so put the verdict in it.
