# data-manifest

Mint an identity record — a `sha256` + `size` (+ opaque `built_by`) per file —
for the experiment's declared input data into `.hpc/data_manifest.json`, journal
the mint, and refresh the `(size, mtime)` fast-path cache. The manifest converts
data changes from **invisible to attributed**: it does not prevent them, it makes
the quiet-corruption class visible — same filename, silently rebuilt bytes, every
downstream result subtly wrong and nothing ever throwing. No robustness layer
catches that (nothing fails); only an identity record sees it.

Core hashes opaque bytes and never parses a format — there is no data-format
knowledge in this verb, no `data/` convention, and `built_by` is caller free text
carried opaquely (preserved across a re-mint, never validated).

## Inputs

- `roots` (list of strings, optional) — opaque relpath roots (files or
  directories) to hash. When absent, defaults to the experiment's existing input
  declaration (`interview.json`'s `audited_source.input_roots`) — the **one**
  "what are my inputs" declaration. With neither `roots` nor a declaration the
  verb refuses (`spec_invalid`): core never guesses a `data/` directory.
- `output_path` (string, optional) — manifest destination (relpath or absolute).
  Defaults to `.hpc/data_manifest.json`.

## Outputs

`{manifest_path, roots, manifest_doc_sha, file_count, files}` where `files` is the
`{relpath: {sha256, size, built_by?}}` record map. `manifest_doc_sha` is the
canonical-JSON hash of `files` (the manifest's identity **as a document** — the
journaled mint's "new known-good" fingerprint), distinct from the raw-byte
file-content shas inside it.

## Errors

- `spec_invalid` — no `roots` supplied and no input declaration found (the refused
  no-declaration case), naming the declaration path to fix.

## Idempotency

Not idempotent: a re-mint after a legitimate data change is a **new** journaled
act — the mint history is the tier-0 "who changed the data, when" timeline the
repo otherwise lacks. Re-minting is the acknowledgment that clears a drift alarm
(silence-by-record). Unchanged files are never re-hashed (the `(size, mtime)`
cache), so a re-mint over untouched data is cheap and produces a byte-identical
manifest.

## Notes

Drift is disclosed, never enforced (accept-with-disclosure). The greenlight brief
carries a verdict-free, code-rendered `data_manifest` disclosure — counts and
identities only ("N match, M drifted, K new, J missing", or the standing "no
manifest" line) — and drift items route into the attention queue under the tier
map: a changed/vanished tracked file is needs-attention (verdict), new untracked
files are one low-tier line, and no-manifest is one standing disclosure. Core
never says "updated / corrupted"; the human concludes.
