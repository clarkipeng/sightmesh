# Search and usage from native evidence

cdesktop owns original execution bytes and artifact occurrences. SightMesh's
separate disposable SQLite index stores contentless FTS5 postings, byte locators,
hashes, provenance, and confirmed ingestion positions. It never stores another
transcript or report body. Do not point it at the task database.

```python
from pathlib import Path
from sightmesh.evidence import EvidenceClient
from sightmesh.evidence_index import EvidenceIndex
from sightmesh.usage import derive_native

native = EvidenceClient("http://127.0.0.1:3210")
index = EvidenceIndex(Path("workspace-evidence-index.sqlite"))
index.sync(native, execution_id, task_id=task_id, repo=repo)
index.sync_artifact(native, execution_id, occurrence_id, task_id=task_id, repo=repo)
result = index.search(native, "checkpoint.operation_id", task_id=task_id)
usage = derive_native(native, execution_id)
```

These consumers require native `execution_evidence` capability version 1 and
confirmed provenance/receipts. An advertised API alone is not a durability proof.
Indexing is opt-in source selection, not an automatic scan of every workspace.
This library example does not authorize installing, migrating, or reclaiming live
state. The SDK checkpoint retention contract and activation gate remain separate.

## Query and coverage contract

Queries are literal, case-sensitive substrings of **3 to 256 Unicode characters**,
including punctuation and identifiers. They are not FTS expressions. NUL is not
supported. This deliberately bounded contract allows a 1,024-byte overlap to find
any supported query spanning a 64 KiB frame, including a split UTF-8 character,
without persisting transcript tails. SQLite must support FTS5's
[trigram tokenizer](https://www.sqlite.org/fts5.html#the_trigram_tokenizer).

Log search matches the original serialized `LogMsg` JSONL, including JSON escapes;
artifact search matches original UTF-8 text. Binary artifacts remain retained by
the native owner but return explicit unsupported-text coverage in this index.
Task, repository, execution, and capture-time filters use ordinary columns/indexes.

Each hit is a bounded original-backed window, with a byte range, first qualifying
match, and short snippet. A match wholly in a window's overlap belongs to its
earlier window. Results are not a list of every individual matching occurrence.
`next_after` pages candidate windows, so a page can have no hits while still having
a next cursor. Restart that disposable cursor after a rebuild.

Read `sources`, `unchecked_sources`, and `complete`, not only `hits`. Coverage
describes the **selected indexed sources**, not undiscovered tasks or sources.
Logs stay in `unchecked_sources` during search: a candidate read only verifies
that window, and search deliberately does not re-read every retained log. An
artifact candidate may be checked because its required full stream verifies the
entire immutable original; an artifact without a candidate stays unchecked.
Historical capture completion and current query verification are separate facts.
Live, legacy-unknown, unavailable, binary, corrupt, or unchecked evidence is not
an empty complete search. Frame cursors use compressed frame starts, including
empty legacy rows and terminal seals; an available physical end is not itself
terminal capture completeness.

Log frames commit cursor and postings in one transaction. Artifact bytes have no
native range endpoint, so each occurrence streams under one disposable-index
transaction and commits only after its full digest matches the confirmed receipt.
An interrupted artifact retries as a whole. Artifact snippets likewise stream and
verify the full original, costing O(artifact size) per candidate but bounded memory.
Every artifact occurrence keeps its producer, original name/path, capture time,
publication key, and native ID even when two occurrences share one blob.

`rebuild(native)` re-reads selected originals, including previously indexed log
prefixes; a final fetched page can observe new tail frames. `compact()` optimizes
only the index. Neither operation deletes, migrates, or changes originals.

## Usage observations, not guessed bills

`derive_native` streams confirmed original JSONL with a bounded 1 MiB outer record
buffer and explicit page budget. It separately buffers the provider's JSONL across
arbitrary native `Stdout` chunks, with the same 1 MiB bound. Native capture
`complete` and `derivation_complete` are distinct: unavailable bytes,
malformed/oversized provider records, partial provider records, outer partial
records, budget exhaustion, and unknown capture outcomes stay visible. It has no
telemetry database or copied source body.

Codex `thread/tokenUsage/updated` has distinct `last` request and `total` thread
scopes. Updates replace the prior observation by thread/turn identity. Thread
lifetime includes any earlier/resumed history, so it does not establish this
execution's consumption. Input already includes cached input, and output already
includes reasoning. Those categories are never added twice. The contract matches
the native app-server normalization fixture in cdesktop PR37.

Claude assistant messages are deduplicated by message ID and expose input/cache
counts; their output count is a placeholder. Streaming output snapshots replace
each other by message identity. A successful execution result remains a separate
main-loop aggregate, never added to per-message snapshots. Missing counters are
unknown, not zero; error results can underreport. `execution_tokens` is therefore
available only from one successful result with known categories and complete
source coverage, and is not a billed or all-subagent total. See Anthropic's
[usage contract](https://code.claude.com/docs/en/agent-sdk/cost-tracking).

No subscription billing, token prices, or savings are inferred. Unknown identities
and unsupported usage shapes remain visible warnings instead of disappearing from
an apparently complete total.
