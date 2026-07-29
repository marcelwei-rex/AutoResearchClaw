# Structured Scientific Claim Authority Design

Status: `B5-D1E / STAGE 23 PROVIDER-RELEVANCE WIRE FROZEN / TRUSTED-LOCAL FILESYSTEM THREAT MODEL NORMALIZED / B5-A2 PAUSED / NOT ACTIVATED`

Scope: Batch B4-D0/D1/D2/D3/D4 authority for structured Stage 19 revision and
structured Stage 20 replay, B5-D1A docs-only authority for deterministic
structured Stage 21 archival, B5-D1B-R1 docs-only authority for structured
Stage 22 export and Stage 23 citation verification, B5-D1D project-wide
release reconstruction, and B5-D1E exact Stage 23 provider/relevance wire
contracts. B5-D1E changes only the Stage 23 report's nested relevance object;
it does not change a report or manifest root schema.
This document also records trusted-local filesystem normalization under the
`structured-scientific-claim-v1` capability.

B3 and B4 implementation and their separately reviewed declarations are
complete. The code-owned structured capability is now exactly `1110`: Stage 17
publication, Stage 19 revision, and Stage 20 replay are declared, while
Stage 24/release integration remains undeclared. B5-D1A freezes the structured
Stage 21 deterministic archive; B5-D1B-R1 freezes the structured Stage 22-23
schemas, transport, outcomes, lifecycles, postconditions, and generic
boundary. B5-D1D changes only the project-wide filesystem threat boundary and
restores the frozen Stage 22 manifest v2 nested-code schema unchanged. It does
not implement or resume Stage 21-23, change the capability map, activate the
structured production path, define Stage 24-25 structured schemas,
reconstruct a release, or change any release gate.

## 1. Normative boundary

The words MUST, MUST NOT, SHOULD, and MAY are normative.

1. `EvidenceFact` and `ScientificClaimRecord` are built by deterministic code
   from one replayed canonical evidence generation.
2. The claim registry is closed before any LLM selection call.
3. An LLM may select, delete, and reorder existing claim IDs. It may not edit a
   rendered sentence, mint an ID, supply prose, or add renderer parameters.
4. Abstract, Results, Discussion, Limitations, and Conclusion use only
   deterministic rendered claims when the capability is active.
5. Connector policy v1 contains exactly one connector template: `NONE`.
6. Stage 19 may keep, delete, and reorder claim IDs only.
7. Stage 20 independently rebuilds and replays the authority whether or not an
   LLM client exists.
8. Stage 24 may reuse byte offsets, IDs, canonical serialization, hashes, and
   replay discipline. Comparative lexical grammar is not a scientific semantic
   oracle for this capability.
9. `generic-v1` behavior, schemas, check lists, bytes, and hashes remain
   unchanged.
10. A fresh F0 remains forbidden until separately authorized.

The current non-sectional and sectional Stage 19 prose paths remain
`generic-v1`. The current conditional Stage 20 replay is also not structured
authority. This design does not describe either blocker as already closed.

### 1.1 Project-wide trusted-local filesystem threat model

This boundary applies uniformly to structured Stages 17-23. It separates
untrusted authority data from the trusted local execution environment; it
does not infer trust from a manifest, hash, path, caller, provider, or LLM.

The trusted execution boundary contains:

- the ResearchClaw main process and code-owned helpers executing in one active
  writer epoch;
- code-owned in-process state, registry tokens, held file descriptors, and the
  operating-system kernel semantics required by those descriptors;
- cooperating ResearchClaw writers, all of which obey the
  `ReleaseGraphLock` and writer-epoch lifecycle; and
- one supported local filesystem with stable mount topology, descriptor-
  relative operations, `O_DIRECTORY`, `O_NOFOLLOW`, `O_EXCL`, stable live
  device/inode identity, and the documented sync/stat/list/unlink semantics.

Provider and LLM responses, caller arguments, configuration and environment
content, persisted capability snapshots, stored artifacts, manifests, hashes,
paths, and artifact presence are untrusted inputs. They never select a route
or become authority without code-owned reconstruction and complete replay.

The application-level threat and failure model includes:

- malformed, duplicate, missing, extra, or wrong-typed fields;
- stale, mixed-generation, wrong-source, partially published, or internally
  hash-consistent stored artifact forgery;
- preexisting symlink, hardlink, FIFO, socket, device, directory, wrong-
  filesystem, case/NFC/prefix alias, or other reserved-name collision;
- cooperating-writer conflicts and deterministic fault injection at the
  documented capture, phase, fixpoint, and postcondition boundaries;
- parent or canonical-name drift detected after an fd has been captured;
- late mutation, ordinary I/O failure, cleanup failure, crash/restart
  residue, and partial publication;
- caller/config/environment/artifact-presence route injection; and
- provider or LLM fallback, retry, cache, transport, or authority injection.

The following are explicitly outside the ResearchClaw process-internal
authority model:

- an arbitrary malicious, noncooperating same-UID process racing namespace or
  content syscalls between any two application operations;
- debugger, `ptrace`, process-memory, inherited-fd, or in-process compromise;
- privileged mount, bind-mount, namespace, or mount-topology manipulation
  during an attempt;
- a compromised kernel, filesystem implementation, storage stack, or OS
  account; and
- cryptographic or historical proof of which process originally created an
  inode, plus crash-atomic multi-file directory transactions.

If untrusted same-UID code must be resisted, deployment MUST use a separate OS
user, container, sandbox, VM, or equivalent kernel-enforced isolation.
ResearchClaw manifests, hashes, writer locks, identity checks, and semantic
replay do not replace that isolation.

This scope does not relax untrusted-data authority. Every consumer still
captures current bytes through held descriptors, independently rebuilds the
expected schema/content/source/generation closure, and performs complete
semantic replay. Altered, unknown, incomplete, stale, mixed-generation, or
merely self-consistent stored bytes reject. An exact byte-for-byte copy of
independently expected content is semantically equivalent content; replay
does not and need not attribute it to a historical producer.

Held-fd and identity rules are current-epoch integrity and fault-detection
mechanisms inside this boundary. They MUST NOT be described as proof against
the excluded syscall adversary. Device/inode values are live-epoch identity,
not persistent creator provenance. An unknown or unsupported filesystem,
including a network, FUSE, or cloud filesystem without the required
semantics, fails closed unless a separate support contract admits it.

## 2. Structured capability admission and mixed generations

The existing `CANONICAL_EVIDENCE_CAPABILITIES` map is frozen independently.
B4 MUST NOT add, remove, or rename a key in that map and MUST NOT route a
partial structured migration through its guard.

Structured admission has code-owned schema version `1` and this separate exact
four-key map. At B4-D0 and throughout B4 implementation it is exactly:

```json
{
  "stage17_publication": 1,
  "stage19_revision": 0,
  "stage20_replay": 0,
  "stage24_and_release_integration": 0
}
```

The schema version MUST satisfy
`type(structured_capability_schema_version) is int` and
`structured_capability_schema_version == 1`. Each component MUST satisfy
`type(component_value) is int and component_value in {0, 1}`; a JSON
boolean is not an integer for this contract. The exact key set is mandatory.
Missing or unknown keys and boolean, float, string, null, or other integer
component values are invalid. The map and its schema version are runtime
authority owned by code. They MUST NOT be accepted from config, an LLM, a
caller, an environment variable, or a persisted snapshot.

The structured capability is active only when:

- the exact code-owned map has all four components at `1`;
- strict canonical input replay confirms an exact domain-evaluator-v2
  generation;
- the independently rebuilt CFS has true integer schema version `1`;
- every structured input belongs to one generation binding.

Implementation commits MUST NOT change the map. After a milestone is complete,
independently reviewed, and explicitly approved, a separate narrow declaration
commit MAY change only the component or components owned by that milestone.
A partial map records reviewed migration progress but does not activate any
structured production path.

### 2.1 Admission matrix

| Case | Strict canonical input result | Code-owned structured map | Ordinary dispatch | Public/direct structured entry |
|---|---|---|---|---|
| 1 | recognized legal non-domain-v2 generation | any valid, partial, or malformed map | unchanged `generic-v1`; no structured probe or cleanup | reject before structured output I/O |
| 2 | exact domain-v2, CFS-v1, one generation | exact `1000` | unchanged `generic-v1` | incomplete capability; reject before lock, provider, or output I/O |
| 3 | domain-v2 discriminator present but Stage 17 authority manifest, CFS, generation, or replay is invalid | any map | `FAILED`; never generic fallback | `FAILED`; never generic fallback |
| 4 | strict canonical replay succeeds | malformed map: missing/unknown key, wrong root type, or wrong-typed value | unchanged `generic-v1`; malformed map is ineligible | capability-invalid before direct-entry I/O |
| 5 | exact domain-v2, CFS-v1, one generation | any other partial state, including exact `1000` | unchanged `generic-v1` | incomplete capability before direct-entry I/O |
| 6 | exact domain-v2, CFS-v1, one generation | internal B4 pre-activation test with repository still `1000` | ordinary dispatcher remains `generic-v1` | public/direct entry remains blocked; only the private verified-context helper in this section is eligible |
| 7 | exact domain-v2, CFS-v1, one generation | current declared `1110` | unchanged `generic-v1` | incomplete capability before direct-entry I/O |
| 8 | exact domain-v2, CFS-v1, one generation | future complete `1111` | structured path | continue under held-fd structured admission |

`Legal non-domain-v2` means strict replay succeeded and the code-owned
discriminator is either absent where the canonical schema permits absence or
names a recognized non-domain-v2 generation. An unknown, malformed, or
inconsistent discriminator is invalid; it is not evidence for generic
fallback.

The ordinary Stage 17/19/20 dispatcher first inspects only the code-owned map,
then reuses the canonical input capture and strict replay already required by
that stage. The captured result is passed to the selected implementation; it
is not reopened through a new live path. This common capture is the sole
discriminator authority:

1. replay failure returns `FAILED`;
2. a valid legal non-domain-v2 generation selects the existing `generic-v1`;
3. a domain-v2 discriminator with invalid evidence, CFS schema, generation
   binding, or replay returns `FAILED`;
4. a valid domain-v2 generation selects structured only when the code-owned
   map is exact and all `1`; otherwise it selects unchanged `generic-v1`.

If the initial code-owned map inspection is exact and all `1`, the dispatcher
MUST run the same complete-map guard before this common capture performs its
first I/O. A partial map does not call that raising structured guard; the
mandatory common capture still runs and can return `FAILED`.

The presence of a domain-v2 discriminator is established only by strict
parsing of the captured canonical envelope. Once that discriminator is
present, a later CFS, schema, generation, or replay error is corruption, not a
legal non-domain-v2 case.

The dispatcher MUST NOT use structured artifact presence, caller/config
declarations, a persisted capability snapshot, or an additional live-path
probe as an oracle. Selecting generic adds no structured file probe, cleanup,
provider call, schema change, or failure rule beyond failures already produced
by the mandatory canonical capture/replay.

A public or direct structured entry MUST call a no-caller-map
complete-capability guard before acquiring a lock or performing any
filesystem, provider, or output I/O. Evidence domain, CFS schema, and
generation prerequisites are then derived through held-fd source capture and
replay; they MUST fail before any provider call or creation or publication of
a new output. Once structured entry begins, any error returns `FAILED`; it
MUST NOT fall back to `generic-v1`.

B4 implementation is tested while the repository map remains exact `1000`.
There is no caller, config, environment, persisted-snapshot, or test-flag
bypass. The only end-to-end pre-activation seam is one private
under-lock helper accepting a `StructuredStage19VerifiedContext`. Its
constructor is private. Code may issue it only after validating a live writer
lease and completing Snapshot A plus full Stage 17 and Stage 18 replay. An
issuance registry binds the context to the same writer-owner object, run and
stage device/inode identities, canonical run path, generation binding,
Stage 17 manifest digest, and complete Snapshot A identity tuple. The helper
rechecks the registry and every binding. A copied, caller-constructed, expired,
cross-run, cross-epoch, or cross-generation context is rejected before
provider or publication I/O. This seam bypasses only the complete-map
admission check for tests; it bypasses no source, schema, lifecycle, or
postcondition check and creates no production activation authority.

A structured registry, selection, paper, closure report, or manifest from a
different evidence/CFS generation MUST fail closed. There is no conversion,
fallback, best-effort merge, or artifact-presence activation between
generations.

### 2.2 Exact milestone states and activation commits

The presentation order below is consistent with the four-key declaration.
Each inline object is the complete exact key set and value map, not a delta;
canonical serialization still sorts object keys under Section 3:

| Repository state | Exact code-owned map | Allowed change |
|---|---|---|
| Completed B3 implementation and declaration; B4-D0 | `{"stage17_publication":1,"stage19_revision":0,"stage20_replay":0,"stage24_and_release_integration":0}` | current `1000`; B4-D0 docs only |
| Every B4 implementation commit | `{"stage17_publication":1,"stage19_revision":0,"stage20_replay":0,"stage24_and_release_integration":0}` | implementation only; map change forbidden |
| Separately approved B4 declaration commit | `{"stage17_publication":1,"stage19_revision":1,"stage20_replay":1,"stage24_and_release_integration":0}` | only B4-owned `stage19_revision` and `stage20_replay: 0 -> 1` |
| Every B5 implementation and pre-activation approval commit | `{"stage17_publication":1,"stage19_revision":1,"stage20_replay":1,"stage24_and_release_integration":0}` | implementation/evidence only; final component remains `0` |
| Separately named and reviewed global activation commit | `{"stage17_publication":1,"stage19_revision":1,"stage20_replay":1,"stage24_and_release_integration":1}` | only `stage24_and_release_integration: 0 -> 1`; activates the full structured path |

Neither `1000` nor `1110` activates a public or direct structured production
entry. The last transition MUST be a commit explicitly named as the global activation
of `structured-scientific-claim-v1`. It MUST contain no implementation change.
It requires separate review of the completed B3, B4, B5 evidence and generic-v1
byte regressions. No implementation, refactor, test, or schema commit may
silently include a capability transition.

## 3. Canonical JSON and Decimal rules

Authority JSON uses the repository `canonical_authority_json_text` rules:

- UTF-8;
- object keys sorted lexicographically;
- no insignificant whitespace;
- exactly one trailing LF;
- arrays retain order;
- strings use JSON escaping with `ensure_ascii=False`;
- booleans and null use JSON literals;
- integers use base-10 JSON integer tokens;
- binary floats are forbidden;
- `Decimal` values are finite and serialized with `canonical_decimal`;
- negative zero canonicalizes to `0`;
- exponent spelling and redundant trailing fractional zeros are removed.

Parsers MUST reject duplicate keys, non-finite values, unknown fields, missing
fields, booleans in numeric positions, unsafe paths, and noncanonical
round-trips.

The code-owned builder MAY canonicalize an already typed, finite `Decimal`
before publication. An authority parser MUST reject noncanonical external
numeric spellings such as negative zero, exponent aliases, or redundant
trailing fractional zeros; it must not silently normalize them into authority.

All SHA-256 values are lowercase 64-character hexadecimal digests over exact
bytes. No Unicode normalization, newline conversion, trimming, or re-encoding
is allowed between hashing and replay.

## 4. Evidence generation binding

`generation_binding_sha256` is:

```text
SHA256(canonical_json({
  "binding_policy_version": 1,
  "canonical_experiment_evidence_path": "canonical_experiment_evidence.json",
  "canonical_experiment_evidence_sha256": "<sha256>",
  "experiment_contract_path": "stage-09/experiment_contract.yaml",
  "experiment_contract_sha256": "<sha256>",
  "run_config_path": "<canonical evidence run_config_path>",
  "run_config_sha256": "<sha256>",
  "cfs_schema_version": 1,
  "cfs_sha256": "<sha256>",
  "claim_policy_id": "structured-scientific-claim-v1"
}))
```

The paths and hashes are obtained from the replayed
`CanonicalExperimentEvidence`, not from a caller-supplied object. The CFS is
rebuilt from that evidence and hashed with `canonical_fact_sheet_sha256`.

## 5. `EvidenceFact` exact schema

Every record has exactly these fields:

```json
{
  "schema_version": 1,
  "fact_id": "<sha256>",
  "fact_kind": "<closed code registry ID>",
  "subject_id": "<closed code registry ID>",
  "predicate_id": "<closed code registry ID>",
  "object_kind": "decimal|string|identifier|boolean",
  "object_value": "<canonical string>",
  "unit_id": "<closed code registry ID or NONE>",
  "source_path": "<safe run-relative path>",
  "source_sha256": "<sha256>",
  "source_json_pointer": "<RFC 6901 pointer>",
  "cfs_schema_version": 1,
  "cfs_sha256": "<sha256>",
  "generation_binding_sha256": "<sha256>"
}
```

`fact_kind`, `subject_id`, `predicate_id`, and `unit_id` come from code-owned
registries. They are never accepted from an LLM.

For `object_kind=decimal`, `object_value` is the exact
`canonical_decimal` string. For `boolean`, it is exactly `true` or `false`.
For `identifier`, it is a code-built registry ID. For `string`, it is an exact
source value admitted by the fact builder; it is data, not a renderer template.

`source_path` names the canonical artifact containing the source value.
`source_sha256` binds its exact bytes. `source_json_pointer` must resolve to the
value during independent replay.

### 5.1 Fact identity and self-hash avoidance

`fact_id` MUST NOT be included in its own identity payload:

```text
fact_id = SHA256(canonical_json({
  all EvidenceFact fields except "fact_id"
}))
```

Validation deletes only the `fact_id` key, recomputes the payload, and requires
byte-exact equality. No placeholder ID, two-pass fixed point, or caller-supplied
identity is permitted.

## 6. `ScientificClaimRecord` exact schema

Every record has exactly these fields:

```json
{
  "schema_version": 1,
  "claim_id": "<sha256>",
  "claim_kind": "<closed code registry ID>",
  "section_id": "abstract|results|discussion|limitations|conclusion",
  "evidence_fact_ids": ["<fact_id>"],
  "renderer_template_id": "<closed code registry ID>",
  "renderer_slot_fact_ids": ["<fact_id>"],
  "rendered_sentence": "<exact sentence>",
  "rendered_sentence_sha256": "<sha256>",
  "mandatory": true,
  "source_path": "<safe run-relative path>",
  "source_sha256": "<sha256>",
  "cfs_schema_version": 1,
  "cfs_sha256": "<sha256>",
  "generation_binding_sha256": "<sha256>"
}
```

All fields are present. `evidence_fact_ids` is nonempty, sorted, and duplicate
free. `renderer_slot_fact_ids` is ordered by the code-owned template and each
ID must occur in `evidence_fact_ids`. `mandatory` is a JSON boolean.

`source_path` and `source_sha256` bind the canonical source artifact from which
the claim registry is built. They do not point to LLM output.

### 6.1 Claim identity and self-hash avoidance

`claim_id` MUST NOT be included in its own identity payload:

```text
claim_id = SHA256(canonical_json({
  all ScientificClaimRecord fields except "claim_id"
}))
```

`rendered_sentence_sha256` is the SHA-256 of the exact UTF-8 bytes of
`rendered_sentence`, with no trailing LF. The claim payload includes both the
sentence and its hash. The validator independently rerenders first, verifies
the sentence bytes and sentence hash, then recomputes `claim_id`.

The registry manifest hash is not stored inside an individual claim, avoiding
a registry/claim self-hash cycle.

## 7. Deterministic renderer

Renderer templates are code constants keyed by `renderer_template_id`.
Templates do not contain user, provider, or LLM strings. A template consumes
only the exact `EvidenceFact` records named in `renderer_slot_fact_ids`.

The renderer MUST:

1. validate the generation, CFS, fact IDs, template ID, and slot arity;
2. render from canonical fact values without float conversion;
3. emit one sentence with terminal punctuation;
4. reject CR, LF, Markdown headings, citation syntax not supplied by the
   template, and control characters;
5. return exact UTF-8 bytes and their SHA-256.

The persisted `rendered_sentence` is evidence, not an editable cache. Replay
rerenders it and requires byte equality.

For connector `NONE`, adjacent rendered sentences are joined by one ASCII
space. `NONE` emits zero bytes itself. Section headings and blank lines are
emitted by deterministic manuscript assembly, not by the LLM.

## 8. Strict JSON claim selection

The LLM response has exactly three keys and no wrapper or prose:

```json
{
  "selected_claim_ids": ["<claim_id>"],
  "ordered_claim_ids": ["<claim_id>"],
  "connector_template_ids": []
}
```

No `schema_version`, comments, Markdown fences, explanations, renderer
arguments, connector arguments, or additional fields are accepted.

Validation order is deterministic:

1. parse strict JSON with duplicate-key rejection;
2. require the exact three-key set;
3. require arrays of strings;
4. require every selected ID to exist in the code-built registry;
5. require every selected claim's `section_id` to equal the target section;
6. reject duplicate selected IDs;
7. require every `mandatory=true` registry claim for the target section;
8. require `ordered_claim_ids` to have the same length as selected IDs;
9. reject duplicate ordered IDs;
10. require `ordered_claim_ids` to be an exact permutation of selected IDs:
   no missing, duplicate, or extra IDs;
11. require connector count to equal
    `max(len(ordered_claim_ids) - 1, 0)`;
12. require every connector ID to equal `NONE`.

There is no connector parameter object or free-string channel in v1. An empty
or one-claim selection therefore has an empty connector array.

## 9. Publication artifacts

B3 artifacts use exact canonical bytes and manifest binding rather than
implicit in-memory state:

- `scientific_evidence_facts.json`
- `scientific_claim_registry.json`
- `scientific_claim_selection.json`
- `scientific_claim_authority_manifest.json`

### 9.1 Facts artifact

`scientific_evidence_facts.json` has a direct array root. An object wrapper is
invalid. Its records have the exact `EvidenceFact` shape in Section 5.

```json
[
  {
    "schema_version": 1,
    "fact_id": "<sha256>",
    "fact_kind": "<code-owned-id>",
    "subject_id": "<code-owned-id>",
    "predicate_id": "<code-owned-id>",
    "object_kind": "decimal|string|identifier|boolean",
    "object_value": "<canonical-string>",
    "unit_id": "<code-owned-id-or-NONE>",
    "source_path": "<safe-run-relative-path>",
    "source_sha256": "<sha256>",
    "source_json_pointer": "<rfc6901-pointer>",
    "cfs_schema_version": 1,
    "cfs_sha256": "<sha256>",
    "generation_binding_sha256": "<sha256>"
  }
]
```

The exact bytes are
`canonical_authority_json_text([fact.to_dict() for fact in facts]).encode("utf-8")`.
Records occur in the code-owned fact registry build order, never sorted by
`fact_id`. In v1 the count is exactly `5`; it MUST equal the array length and
the independently rebuilt code-owned count. The artifact SHA-256 covers the
complete file bytes. The artifact hash and count occur only in the authority
manifest, not in this artifact.

Choosing a wrapper would change the B2 `facts_bytes()` canonical identity and
is therefore a contract migration, not a B3 publication choice.

### 9.2 Claim registry artifact

`scientific_claim_registry.json` also has a direct array root. An object
wrapper is invalid. Its records have the exact `ScientificClaimRecord` shape
in Section 6.

```json
[
  {
    "schema_version": 1,
    "claim_id": "<sha256>",
    "claim_kind": "<code-owned-id>",
    "section_id": "abstract|results|discussion|limitations|conclusion",
    "evidence_fact_ids": ["<fact-id>"],
    "renderer_template_id": "<code-owned-id>",
    "renderer_slot_fact_ids": ["<fact-id>"],
    "rendered_sentence": "<exact-sentence>",
    "rendered_sentence_sha256": "<sha256>",
    "mandatory": true,
    "source_path": "<safe-run-relative-path>",
    "source_sha256": "<sha256>",
    "cfs_schema_version": 1,
    "cfs_sha256": "<sha256>",
    "generation_binding_sha256": "<sha256>"
  }
]
```

The exact bytes are the existing code-owned `claims_bytes()`. Records occur in
the renderer template registry build order, never sorted by `claim_id`. In v1
the count is exactly `6`; it MUST equal the array length and the independently
rebuilt code-owned count. The artifact SHA-256 covers the complete file bytes.
The artifact hash and count occur only in the authority manifest.

### 9.3 Five-section selection artifact

`scientific_claim_selection.json` has exactly this top-level key set and
shape:

```json
{
  "schema_version": 1,
  "claim_policy_id": "structured-scientific-claim-v1",
  "generation_binding_sha256": "<sha256>",
  "claim_registry_sha256": "<sha256>",
  "sections": [
    {
      "section_id": "abstract",
      "selected_claim_ids": [],
      "ordered_claim_ids": [],
      "connector_template_ids": []
    },
    {
      "section_id": "results",
      "selected_claim_ids": [],
      "ordered_claim_ids": [],
      "connector_template_ids": []
    },
    {
      "section_id": "discussion",
      "selected_claim_ids": [],
      "ordered_claim_ids": [],
      "connector_template_ids": []
    },
    {
      "section_id": "limitations",
      "selected_claim_ids": [],
      "ordered_claim_ids": [],
      "connector_template_ids": []
    },
    {
      "section_id": "conclusion",
      "selected_claim_ids": [],
      "ordered_claim_ids": [],
      "connector_template_ids": []
    }
  ]
}
```

`schema_version` is true integer `1`. `claim_policy_id` is exact. The
generation binding and registry SHA MUST equal independently rebuilt values.
`sections` is an array of length `5`; each item has exactly the four shown
keys. Its code-owned order is exactly `abstract`, `results`, `discussion`,
`limitations`, `conclusion`, with each section appearing once. Duplicate,
unknown, missing, or reordered sections are rejected.

A section-keyed object is forbidden: lexicographic canonical JSON key sorting
cannot carry policy order. For each section the provider returns only the
three-field object from Section 8. Code parses and semantically validates that
response against the target section, injects `section_id`, appends it in the
fixed order, and injects the schema, policy, generation, and registry binding
at the root. A provider-supplied section, generation, schema, policy, hash, or
response-hash field is an extra field and is rejected.

The selection SHA-256 covers the complete wrapper canonical bytes. Provider
response bytes or their hashes are not authority and MUST NOT authorize the
selection or appear in the authority manifest. They MAY be retained only in
explicitly non-authoritative diagnostics.

### 9.4 Exact Stage 17 authority manifest

Every `FileRef` has exactly two keys:

```json
{
  "path": "<safe-run-relative-regular-file>",
  "sha256": "<sha256>"
}
```

`scientific_claim_authority_manifest.json` has exactly this shape:

```json
{
  "schema_version": 1,
  "publication_stage_id": "stage17",
  "claim_policy_id": "structured-scientific-claim-v1",
  "structured_capability_schema_version": 1,
  "structured_capability_snapshot": {
    "stage17_publication": 1,
    "stage19_revision": 1,
    "stage20_replay": 1,
    "stage24_and_release_integration": 1
  },
  "generation_binding_sha256": "<sha256>",
  "canonical_experiment_evidence": {
    "path": "canonical_experiment_evidence.json",
    "sha256": "<sha256>"
  },
  "experiment_contract": {
    "path": "stage-09/experiment_contract.yaml",
    "sha256": "<sha256>"
  },
  "run_config": {
    "path": "<bound-config-path>",
    "sha256": "<sha256>"
  },
  "cfs": {
    "schema_version": 1,
    "sha256": "<sha256>"
  },
  "facts": {
    "file": {
      "path": "stage-17/scientific_evidence_facts.json",
      "sha256": "<sha256>"
    },
    "record_count": 5
  },
  "claim_registry": {
    "file": {
      "path": "stage-17/scientific_claim_registry.json",
      "sha256": "<sha256>"
    },
    "record_count": 6
  },
  "claim_selection": {
    "file": {
      "path": "stage-17/scientific_claim_selection.json",
      "sha256": "<sha256>"
    },
    "section_count": 5
  },
  "paper_draft": {
    "path": "stage-17/paper_draft.md",
    "sha256": "<sha256>"
  },
  "paper_structure_report": {
    "path": "stage-17/paper_structure_report.json",
    "sha256": "<sha256>"
  },
  "experiment_fact_closure_report": {
    "path": "stage-17/experiment_fact_closure_report.json",
    "sha256": "<sha256>"
  },
  "citation_closure_report": {
    "path": "stage-17/citation_closure_report.json",
    "sha256": "<sha256>"
  },
  "source_authority_manifest": null
}
```

The displayed root keys are the exact top-level key set. Each nested object is
also exact: `cfs` has `schema_version` and `sha256`; `facts` and
`claim_registry` have `file` and `record_count`; `claim_selection` has `file`
and `section_count`; each nested `file` is an exact `FileRef`. Version and
count positions require true JSON integers, not booleans. Every path is a safe
run-relative regular file and every digest binds its exact bytes.

The capability schema version and snapshot are copied from code-owned runtime
authority only. Replay exact-compares both to current code-owned expected
values and requires the four components to be all `1` before reading a
structured `FileRef`. The snapshot records admission; it cannot activate a
path or mutate runtime authority.

For a Stage 17 origin, `source_authority_manifest` MUST be present and its only
legal value is JSON null. Missing, empty object, empty string, or a `FileRef`
is rejected. Stage 19 does not reuse this envelope: Section 11 freezes a
distinct schema-version-2 manifest because Stage 17 schema version 1 cannot
unambiguously represent the Stage 18 review binding, source selection, source
paper, or Stage 19 closure. In that Stage 19 schema,
`source_authority_manifest` is an exact `FileRef` to this Stage 17 manifest;
null or missing is rejected.

The manifest contains no field for its own path, SHA-256, or identity. Its
consumer captures and hashes the manifest's exact bytes externally; a later
source manifest binds that digest. There is no manifest self-hash cycle.

`paper_structure_report.json` is part of the authority because it is a current
Stage 17 success output used by closure validation.
`section_generation_report.json`, provider-response hashes, repair logs,
invalid drafts, and other diagnostics are non-authoritative and MUST NOT be
referenced by this manifest.

### 9.5 Full rebuild, not mutual consistency

Mutually consistent facts, registry, selection, and manifest hashes are not
sufficient: an external actor could edit all four and recompute every
downstream digest. Independent replay MUST:

1. capture and replay canonical evidence, contract, and run config from the
   held namespace;
2. rebuild the CFS and generation binding;
3. rebuild all facts and claims from code-owned registries;
4. require facts and registry canonical byte equality, not only matching
   hashes;
5. parse the selection wrapper and rerun section membership, mandatory,
   permutation, and connector checks for every section;
6. rerender and reassemble the paper and require byte equality;
7. replay the experiment-fact and citation closure reports against that paper
   and evidence;
8. compare every manifest `FileRef`, count, version, policy, generation, and
   capability snapshot to independently derived expected values;
9. recapture sources and the final namespace and require a fixpoint.

Replacing canonical evidence is not authorized by synchronously recomputing
these Stage 17 artifacts; the upstream evidence/contract/config authority
chain must independently replay first.

The manifest is written last. It cannot be consumed until independent staged
and final disk replay succeeds.

## 10. Stage 17 publication lifecycle

The structured path owns this exact eight-name canonical authority set in the
held `stage-17` namespace:

- `scientific_evidence_facts.json`;
- `scientific_claim_registry.json`;
- `scientific_claim_selection.json`;
- `paper_draft.md`;
- `paper_structure_report.json`;
- `experiment_fact_closure_report.json`;
- `citation_closure_report.json`;
- `scientific_claim_authority_manifest.json`.

It separately owns the temporary staging entry
`.stage17-structured-publication.staging`. Staging is never canonical
authority, is never referenced by the manifest, and MUST be consumed and
removed by staged publication. Its presence in the final namespace is a
replay failure.

The four new scientific-claim names and the four existing Stage 17
paper/closure names form one structured publication transaction.
`section_generation_report.json`, repair logs, invalid outputs, and other
diagnostics remain outside this authority namespace.

When the capability is active, Stage 17 MUST:

1. run the direct structured admission guard before its first I/O;
2. acquire the existing writer/release epoch and held run namespace;
3. consume the ordinary dispatcher's single strict canonical input capture, or
   perform that same capture when invoked through a direct structured test
   entry; it MUST NOT reopen a second live path;
4. require that capture to prove exact domain-v2, CFS-v1, and one generation,
   returning `FAILED` on any error;
5. use the held run fd to open one `BoundOutputNamespace` for canonical
   `stage-17`;
6. invalidate `scientific_claim_authority_manifest.json` first;
7. independently attempt cleanup of every other owned canonical entry and the
   staging entry, without allowing one collision to prevent cleanup of the
   remaining names;
8. rebuild the CFS and generation binding from the captured authority and
   require equality to the dispatch result;
9. build and validate facts and the closed claim registry;
10. request strict JSON selections only for the five governed sections;
11. validate mandatory selection, exact permutation, and connectors;
12. render governed sections and assemble the paper deterministically;
13. build the structure, experiment-fact, and citation closure reports;
14. stage all non-manifest outputs under the exact staging name;
15. independently reopen and replay the complete staged namespace;
16. verify a source fixpoint against the captured authority;
17. publish all non-manifest outputs and require publication to consume and
    remove the staging entry;
18. verify the sources again;
19. write the authority manifest last with collision-safe atomic publication;
20. independently replay the exact eight-name final authority namespace,
    require the staging entry to be absent, and verify the source fixpoint;
21. repeat the final capture/replay and require both snapshots, sources, held
    directory identities, and manifest bytes to be unchanged before success.

The LLM must not return free prose for Abstract, Results, Discussion,
Limitations, or Conclusion. Other sections retain only the behavior explicitly
allowed by the selected capability design.

All canonical access and mutation is relative to held directory fds. A
symlink, file/directory collision, staging collision, run or stage parent
replacement, source mutation, late namespace mutation, or unsupported held-fd
operation fails closed. No external target may be read through a replacement
path or written at all.

On any error, cleanup again invalidates the manifest first and independently
attempts every other owned name and staging entry. Cleanup failure is attached
to the original failure and cannot turn the result into success. Invalid
paper/selection and diagnostics may be retained only under explicitly
non-authoritative, non-manifest names. A failure must not leave a consumable
manifest.

## 11. Stage 19 source authority: Snapshot A

Structured Stage 19 captures all sources once through held descriptors before
any provider call. `Snapshot A` is the following ordered tuple. Every file
entry contains its safe run-relative path, exact bytes, SHA-256, byte size,
device/inode identity, mode, and link count:

1. the complete Stage 17 authority, in exact executor artifact order:
   `stage-17/scientific_evidence_facts.json`,
   `stage-17/scientific_claim_registry.json`,
   `stage-17/scientific_claim_selection.json`,
   `stage-17/paper_draft.md`,
   `stage-17/paper_structure_report.json`,
   `stage-17/experiment_fact_closure_report.json`,
   `stage-17/citation_closure_report.json`, and
   `stage-17/scientific_claim_authority_manifest.json`;
2. the complete Stage 17 source-file closure, sorted by run-relative path:
   canonical evidence manifest, candidate manifest, selected-result manifest,
   every recursively referenced safe run-relative `FileRef`, every evidence
   artifact and project artifact, selected execution artifact, execution
   policy artifact, experiment contract, and bound run config;
3. `stage-16/citation_plan.json`;
4. `stage-06/citation_allowlist.json`;
5. `stage-18/reviews.md`;
6. `stage-18/review_structure_report.json`.

Duplicate paths across items 1-4 are captured once and MUST have the same exact
bytes and digest at every binding site. The Stage 17 manifest entry includes
its exact path, bytes, SHA-256, size, and identity even though the Stage 17
manifest does not self-reference. The source closure MUST explicitly prove
that the canonical evidence, experiment contract, and run config match the
Stage 17 manifest. Code independently rebuilds canonical CFS bytes, requires
true integer CFS schema version `1`, recomputes the CFS SHA-256, and rebuilds
the generation binding from these captured bytes.

Stage 17 full replay is mandatory. It rebuilds the five facts and six claims,
requires exact facts and registry bytes, strictly replays the five-section
Stage 17 selection, rerenders the governed sentences, requires exact
`paper_draft.md`, and rebuilds all three Stage 17 closure reports. Matching
stored hashes is not sufficient.

Stage 18 has no manifest and has no absence branch. Both Stage 18 files are
mandatory regular single-link files. The schema-version-2
`review_structure_report.json` has exactly these keys:
`schema_version`, `valid`, `source_reviews_path`, `source_reviews_sha256`,
`source_paper_path`, `source_paper_sha256`,
`paper_structure_report_path`, `paper_structure_report_sha256`,
`experiment_fact_closure_report_path`,
`experiment_fact_closure_report_sha256`,
`citation_closure_report_path`, `citation_closure_report_sha256`,
`canonical_experiment_evidence_path`,
`canonical_experiment_evidence_sha256`, `comment_count`, and `issues`.
`schema_version` and `comment_count` are true integers; schema is exactly `2`;
`valid` MUST be true; `issues` MUST be empty. Every path/hash pair MUST bind
the captured Stage 17 or Stage 18 bytes, and strict `ReviewLedger` replay from
`reviews.md` MUST reproduce `comment_count`.

Because current Stage 18 has no independently anchored provenance manifest,
the two-file binding proves internal consistency and the Snapshot A/B
fixpoint; it does not prove the historical origin of a synchronized,
strictly-valid replacement made before Snapshot A. Any Stage 18 byte change
after Snapshot A is rejected. A pre-Snapshot replacement of both files that
correctly rebuilds the schema-version-2 report is mechanically
indistinguishable at this boundary, but remains confined to advisory input and
the selection constraints in Section 13. B4-D0 does not invent an
unimplemented Stage 18 provenance oracle.

The Stage 18 review bytes are provider-advisory authority only. They may
influence which pre-existing claim IDs the provider suggests keeping,
deleting, or reordering. They never become fact, claim, sentence, renderer,
template, slot, connector, or prose authority. Scientific authority remains
the independently rebuilt evidence, facts, claim registry, validated
selection, deterministic rendering, and closure reports.

The citation plan and allowlist are used only for deterministic citation
closure replay. Citation policy, raw evidence prose, configuration prose,
live files, sectional plans, prior provider responses, and any other input are
forbidden from the Stage 19 provider prompt. There is no vague category such
as "related inputs" or "complete publication".

## 12. Exact Stage 19 authority namespace

Structured Stage 19 owns this exact success authority set in the held
`stage-19` namespace:

1. `scientific_claim_selection.json`;
2. `scientific_claim_paper_revised.md`;
3. `scientific_claim_paper_structure_report.json`;
4. `scientific_claim_experiment_fact_closure_report.json`;
5. `scientific_claim_citation_closure_report.json`;
6. `scientific_claim_authority_manifest.json`.

The exact staging entry is
`.stage19-structured-publication.staging`. The success `StageResult.artifacts`
tuple is the six names above in that order; `evidence_refs` is the same order
with `stage-19/` prefixes. The manifest is always last.

The final held `stage-19` structured namespace has exactly those six regular,
single-link files and no staging entry. Structured Stage 19 persists no raw
provider response, invalid selection, repair output, or diagnostics file.
Transport diagnostics exist only as bounded in-memory/log records, are not
authority, and are never referenced by a manifest. Any extra direct entry,
including an old generic-v1 artifact, diagnostic, invalid output, symlink,
directory, FIFO, device, socket, hardlink, or staging collision, fails the
structured exact-namespace check; structured cleanup MUST NOT delete a name it
does not own.

The distinct `scientific_claim_*` paper and closure names intentionally do not
reuse generic-v1 Stage 19 names. Stage 17 facts, registry, source selection,
source paper, closures, and manifest are referenced from their Stage 17 paths;
they are not copied into Stage 19. Stage 19 republishes only its new selection,
revised paper, three rebuilt closure artifacts, and manifest.

Every failure invalidates these commit points: the manifest first, then each of
the five non-manifest outputs independently, then staging. A malformed current
artifact or collision at one name cannot prevent attempted invalidation of the
other owned commit points. Failure returns empty `artifacts` and
`evidence_refs` and MUST leave neither a revised-paper success artifact nor a
consumable manifest.

## 13. Stage 19 selection schema

`stage-19/scientific_claim_selection.json` uses selection schema version `1`
and has this exact root and nested key set:

```json
{
  "schema_version": 1,
  "claim_policy_id": "structured-scientific-claim-v1",
  "generation_binding_sha256": "<sha256>",
  "claim_registry_sha256": "<sha256>",
  "sections": [
    {
      "section_id": "abstract",
      "selected_claim_ids": ["<abstract-claim-id>"],
      "ordered_claim_ids": ["<abstract-claim-id>"],
      "connector_template_ids": []
    },
    {
      "section_id": "results",
      "selected_claim_ids": ["<results-claim-id>"],
      "ordered_claim_ids": ["<results-claim-id>"],
      "connector_template_ids": []
    },
    {
      "section_id": "discussion",
      "selected_claim_ids": ["<discussion-claim-id>"],
      "ordered_claim_ids": ["<discussion-claim-id>"],
      "connector_template_ids": []
    },
    {
      "section_id": "limitations",
      "selected_claim_ids": ["<limitations-claim-id>"],
      "ordered_claim_ids": ["<limitations-claim-id>"],
      "connector_template_ids": []
    },
    {
      "section_id": "conclusion",
      "selected_claim_ids": ["<conclusion-claim-id>"],
      "ordered_claim_ids": ["<conclusion-claim-id>"],
      "connector_template_ids": []
    }
  ]
}
```

`schema_version` is the true integer `1`. The root exact key set is
`schema_version`, `claim_policy_id`, `generation_binding_sha256`,
`claim_registry_sha256`, and `sections`. `sections` is an array of exactly five
objects in the fixed order `abstract`, `results`, `discussion`, `limitations`,
`conclusion`. Each section exact key set is `section_id`,
`selected_claim_ids`, `ordered_claim_ids`, and `connector_template_ids`.
Duplicate keys and unknown, missing, duplicated, or reordered sections fail.

For each section, the provider returns exactly:

```json
{
  "selected_claim_ids": ["<claim-id>"],
  "ordered_claim_ids": ["<claim-id>"],
  "connector_template_ids": []
}
```

Code injects `section_id` and every root field. A provider response containing
section, schema, policy, hash, generation, fact, sentence, rendered prose,
template, slot, explanation, or any other free string/field is rejected.

The Stage 19 selection is a constrained descendant of both the Stage 17
registry and Stage 17 selection:

- a Stage 19 `selected_claim_ids` array is a duplicate-free subset of the
  corresponding Stage 17 selected IDs;
- it may keep or delete only those IDs and may not reintroduce an optional ID
  present in the registry but absent from the Stage 17 selection;
- every registry-mandatory ID for that section MUST remain selected;
- every ID MUST belong to that section and the same generation;
- `ordered_claim_ids` is an exact duplicate-free permutation of the Stage 19
  selected set; it may reorder but may not add, omit, or duplicate;
- `connector_template_ids` has exactly
  `max(len(ordered_claim_ids) - 1, 0)` entries and every entry is exact `NONE`.

Unknown, missing, duplicate, cross-section, or minted IDs; mandatory deletion;
connector injection; wrong connector count; and selection/ordering membership
forks are rejected. Provider bytes and response hashes are not authority.

## 14. Deterministic revised paper and closure

The captured Stage 17 paper is the sole document template. Code parses it
strictly and replaces only the bodies of Abstract, Results, Discussion,
Limitations, and Conclusion. The preamble, governed headings, every
non-governed heading and body, ordering, whitespace, line endings, citation
markers, and all other bytes are copied exactly from Snapshot A.

Each governed body is rerendered from the unchanged, independently rebuilt
facts and registry plus the validated Stage 19 selection. No stored
`rendered_sentence`, sentence hash, fact ID, or claim ID is trusted: all are
recomputed from canonical inputs. The complete
`scientific_claim_paper_revised.md` MUST be byte-for-byte equal to an
independent rerender and splice.

Stage 19 recomputes, canonically serializes, and publishes:

- `stage-19/scientific_claim_paper_structure_report.json`;
- `stage-19/scientific_claim_experiment_fact_closure_report.json`;
- `stage-19/scientific_claim_citation_closure_report.json`.

All three reports bind the revised-paper digest and are independently rebuilt
from Snapshot A, not copied from Stage 17. Citation closure uses only the
captured citation plan and allowlist. Decimal and JSON handling follows
Section 3. Synchronously editing stored sentences, hashes, IDs, reports, and
their downstream digests is not proof; code-owned rebuild and exact byte
comparison are the oracle.

## 15. Exact Stage 19 manifest schema

Stage 19 uses manifest schema version `2`. A `FileRef` still has exactly two
keys, `path` and `sha256`; Snapshot A/B, rather than persisted `FileRef`, owns
size and identity checks. The exact manifest shape is:

```json
{
  "schema_version": 2,
  "publication_stage_id": "stage19",
  "claim_policy_id": "structured-scientific-claim-v1",
  "structured_capability_schema_version": 1,
  "structured_capability_snapshot": {
    "stage17_publication": 1,
    "stage19_revision": 1,
    "stage20_replay": 1,
    "stage24_and_release_integration": 1
  },
  "generation_binding_sha256": "<sha256>",
  "canonical_experiment_evidence": {
    "path": "canonical_experiment_evidence.json",
    "sha256": "<sha256>"
  },
  "experiment_contract": {
    "path": "stage-09/experiment_contract.yaml",
    "sha256": "<sha256>"
  },
  "run_config": {
    "path": "<bound-config-path>",
    "sha256": "<sha256>"
  },
  "cfs": {
    "schema_version": 1,
    "sha256": "<sha256>"
  },
  "facts": {
    "file": {
      "path": "stage-17/scientific_evidence_facts.json",
      "sha256": "<sha256>"
    },
    "record_count": 5
  },
  "claim_registry": {
    "file": {
      "path": "stage-17/scientific_claim_registry.json",
      "sha256": "<sha256>"
    },
    "record_count": 6
  },
  "source_claim_selection": {
    "file": {
      "path": "stage-17/scientific_claim_selection.json",
      "sha256": "<sha256>"
    },
    "section_count": 5
  },
  "claim_selection": {
    "file": {
      "path": "stage-19/scientific_claim_selection.json",
      "sha256": "<sha256>"
    },
    "section_count": 5
  },
  "source_paper_draft": {
    "path": "stage-17/paper_draft.md",
    "sha256": "<sha256>"
  },
  "paper_revised": {
    "path": "stage-19/scientific_claim_paper_revised.md",
    "sha256": "<sha256>"
  },
  "paper_structure_report": {
    "path": "stage-19/scientific_claim_paper_structure_report.json",
    "sha256": "<sha256>"
  },
  "experiment_fact_closure_report": {
    "path": "stage-19/scientific_claim_experiment_fact_closure_report.json",
    "sha256": "<sha256>"
  },
  "citation_closure_report": {
    "path": "stage-19/scientific_claim_citation_closure_report.json",
    "sha256": "<sha256>"
  },
  "stage18_review": {
    "reviews": {
      "path": "stage-18/reviews.md",
      "sha256": "<sha256>"
    },
    "structure_report": {
      "path": "stage-18/review_structure_report.json",
      "sha256": "<sha256>"
    },
    "structure_schema_version": 2,
    "comment_count": 1
  },
  "source_authority_manifest": {
    "path": "stage-17/scientific_claim_authority_manifest.json",
    "sha256": "<sha256>"
  }
}
```

The exact root key set is: `schema_version`, `publication_stage_id`,
`claim_policy_id`, `structured_capability_schema_version`,
`structured_capability_snapshot`, `generation_binding_sha256`,
`canonical_experiment_evidence`, `experiment_contract`, `run_config`, `cfs`,
`facts`, `claim_registry`, `source_claim_selection`, `claim_selection`,
`source_paper_draft`, `paper_revised`, `paper_structure_report`,
`experiment_fact_closure_report`, `citation_closure_report`, `stage18_review`,
and `source_authority_manifest`.

`schema_version` is true integer `2`;
`publication_stage_id` is exact `stage19`. `cfs` has exactly
`schema_version` and `sha256`. The facts and registry objects have exactly
`file` and `record_count`; counts are true integers `5` and `6`. Both selection
objects have exactly `file` and `section_count`; counts are true integer `5`.
`stage18_review` has exactly `reviews`, `structure_report`,
`structure_schema_version`, and `comment_count`; the version is true integer
`2`, and the count equals the independently replayed ledger.

Stage 17 `FileRef`s are used for canonical evidence, contract, config, facts,
registry, source selection, source paper, and source authority manifest. The
Stage 18 structure report separately binds its Stage 17 source paper and
reports through the exact flat path/hash fields frozen in Section 11. Stage 19
`FileRef`s are used only for the new selection, revised paper, and three new
closure reports. Every path is a safe run-relative regular single-link file
and every digest covers exact bytes.

`source_authority_manifest` MUST be the exact Stage 17 manifest `FileRef`.
Unlike the Stage 17 origin schema, null is forbidden. Missing, null, empty
object, wrong path, or another Stage 17 generation is rejected. Both Stage 18
files are mandatory `FileRef`s; there is no null/absence branch.

The capability snapshot is copied only from the validated code-owned map. A
public production publication requires exact `1111`. The private B4
pre-activation helper may test the same schema while the repository remains
`1000`; such a manifest records exact `1000`, is accepted only inside its
live verified context, and is never public/direct or Stage 20 production
authority. `1110`, malformed, caller-supplied, or persisted snapshots cannot
authorize publication.

The manifest contains no path, hash, size, or identity for itself. Its exact
bytes are captured and hashed externally by the consumer and by a later source
manifest, so there is no self-hash cycle.

Replay independently rebuilds all expected fields and bytes. Mutual consistency
among stored files, hashes, IDs, and manifests is never an oracle.

## 16. Provider prompt, no-op behavior, and call bounds

All five non-no-op prompts are precomputed from the same immutable Snapshot A
before the first call. Each prompt is canonical UTF-8 JSON with exactly these
root fields:

```json
{
  "schema_version": 1,
  "claim_policy_id": "structured-scientific-claim-v1",
  "target_section": "abstract",
  "source_selection": {
    "selected_claim_ids": ["<claim-id>"],
    "ordered_claim_ids": ["<claim-id>"],
    "connector_template_ids": []
  },
  "allowed_claims": [
    {
      "claim_id": "<claim-id>",
      "mandatory": true,
      "rendered_sentence": "<code-rendered-sentence>"
    }
  ],
  "review_advice": [
    {
      "comment_id": "<comment-id>",
      "reviewer": "Reviewer A",
      "category": "actionable_revision",
      "exact_text": "<captured-review-comment>"
    }
  ]
}
```

The root exact key set is `schema_version`, `claim_policy_id`,
`target_section`, `source_selection`, `allowed_claims`, and `review_advice`.
`source_selection` has exactly the provider's three selection keys.
`allowed_claims` contains only code-owned `claim_id`, `mandatory`, and
`rendered_sentence` for exactly the claims selected for that target section by
Stage 17; registry-only unselected claims are not shown. `review_advice`
contains every strictly replayed ledger comment, in ledger order, with only
the fields shown. Review text is untrusted data, not instructions or prose
authority. Citation plan, policy, evidence prose, config, CFS, hashes,
generation identifiers, previous call output, and live state are not prompt
fields.

The no-op definition is exact: strict replay of both Stage 18 files produces a
`ReviewLedger.comments` tuple of length zero. Raw whitespace, keywords, caller
or config flags, and model judgment cannot define no-op.

- If comments are empty, Stage 19 makes zero provider calls, inherits the exact
  Stage 17 selection, independently rerenders, rebuilds closures, and completes
  the full Stage 19 lifecycle.
- If comments are nonempty and `llm is None`, Stage 19 first completes Snapshot
  A, full Stage 17 replay, and Stage 18 dual-FileRef replay, then fails before
  staging or success publication. It invalidates old Stage 19 success
  authority, preserves Stage 17/18 sources, and never falls back to
  `generic-v1`.
- If comments are nonempty and an LLM exists, Stage 19 performs exactly five
  semantic selection calls, one per governed section in fixed section order.

The maximum semantic call count is `5`. Each semantic call permits at most two
actual outbound requests, so the total outbound maximum is `10`. The second
request is allowed only after a pure transport failure: connection failure,
timeout, or explicitly retryable HTTP `429`/`5xx`, and only when no response
content exists that can undergo schema or semantic validation.

The second request uses identical model, endpoint, exact prompt bytes, JSON
mode, temperature, max tokens, and request fingerprint. The request uses JSON
mode, temperature `0`, and a code-owned max-token limit. Model fallback,
endpoint fallback, a third request, free-form repair, and semantic repair are
forbidden. Empty, truncated, malformed, duplicate-key, extra-field,
missing-field, invalid-ID, schema-invalid, or semantic-invalid response content
is an immediate `FAILED`, not a retry condition.

B4 implementation MUST NOT call a generic chat helper with hidden retry,
fallback, or repair behavior unless a mechanical test proves that its complete
outbound behavior satisfies this exact contract.

Each outbound attempt emits a non-authoritative diagnostic record with exact
fields `semantic_call_ordinal`, `section_id`, `outbound_attempt_ordinal`,
`request_fingerprint`, `outcome`, and `transport_class`. Ordinals are
one-based; the second outbound attempt retains the same fingerprint. Records
contain no raw response content and are kept only in bounded memory/logging.
A previous call's response never changes a later prompt or authority.

## 17. Held-fd Stage 19 lifecycle

The structured transaction order is normative:

1. **Capability/admission.** Public/direct entry checks the exact code-owned
   map before its first I/O, lock, or provider action. The private B4 test seam
   validates its unforgeable verified context.
2. **Release-graph writer epoch.** Acquire one writer epoch for the complete
   attempt; no nested live-path publication authority is created.
3. **Held run/stage namespace.** Bind the run and canonical `stage-19`
   directories by descriptor and record their device/inode identities.
4. **Manifest-first invalidation.** Attempt to remove
   `scientific_claim_authority_manifest.json` first.
5. **Aggregate cleanup.** Independently attempt each other owned success name
   and `.stage19-structured-publication.staging`; one malformed artifact,
   collision, or cleanup error cannot suppress attempts at the remaining
   commit points.
6. **Snapshot A capture.** Capture the exact ordered source closure from
   Section 11 through held descriptors.
7. **Stage 17 full replay.** Independently rebuild CFS, generation, facts,
   registry, selection, paper, reports, and Stage 17 manifest.
8. **Provider selection.** Strictly replay Stage 18, apply Section 16 no-op/LLM
   rules, and obtain only validated three-field selections.
9. **Deterministic rerender.** Build the Stage 19 selection, splice the five
   code-rendered bodies, and rebuild all three closure reports.
10. **Staged exact replay.** Create the exact staging tree with the five
    non-manifest files, reopen it, require exact names, and independently
    replay every byte.
11. **Source fixpoint.** Recapture all Snapshot A sources and require unchanged
    bytes, hashes, sizes, modes, link counts, and file identities.
12. **Non-manifest publication.** Exclusively publish the five staged files to
    their exact direct names; no overwrite or late collision is permitted.
13. **Staging absence.** Require the staging entry to be consumed and absent.
14. **Manifest-last.** Independently build schema-v2 manifest bytes and publish
    the manifest with collision-safe atomic creation.
15. **Final semantic replay.** Capture the exact six-name final namespace and
    independently replay source manifest, Stage 18 binding, selection, paper,
    closures, and manifest.
16. **Snapshot B.** Recapture the complete source tuple and final namespace,
    including file and directory identities.
17. **A/B equality.** Require Snapshot A source bytes, hashes, sizes, modes,
    link counts, and identities to equal the source portion of Snapshot B;
    require a second final-namespace capture to equal the first and require
    held run/stage identities to remain exact.
18. **Executor postconditions.** Immediately after the stage result and again
    after HITL/terminal hooks, validate the live writer context, exact artifact
    and evidence-ref tuples, exact namespace snapshot, manifest replay, staging
    absence, and run/stage identities.
19. **Success return.** Only after both postconditions can Stage 19 return
    `DONE` with the exact tuple from Section 12.

All canonical reads, writes, staging, publication, and cleanup are
descriptor-relative. Symlink, FIFO, socket, device, special file, directory
collision, hardlink (`st_nlink != 1`), late name collision, unsafe path, or
identity change fails closed.

If deterministic fault injection or a cooperating-lifecycle defect replaces
the live run or stage parent after fd capture, the transaction does not
rediscover, read, write, or delete through the replacement path. Cleanup may
operate only on the already-held detached original inode. It invalidates every
reachable commit point there so detached authority cannot revive if the
original parent is restored. Within Section 1.1, replacement-target behavior
is external-zero-write and external-zero-delete; this is not a claim against
excluded arbitrary syscall interleavings.

Cleanup failure is appended to the original error and never changes failure to
success. A malformed current manifest, paper, staging tree, or other artifact
cannot block independent invalidation of other owned names. Any provider,
replay, publication, final-capture, or executor-postcondition failure runs
manifest-first aggregate cleanup and returns `FAILED` with empty artifact
tuples.

## 18. Structured Stage 20 success authority

### 18.1 Sole source binding

Structured Stage 20 has exactly one source authority:
`stage-19/scientific_claim_authority_manifest.json` plus the exact six-name
Stage 19 namespace from Section 12. Stage 20 captures every source through
held descriptors. It hashes exact held bytes and never rediscovers source
authority from a stored Stage 20 output.

The following five values are exact and appear in both structured
`quality_report.json` and structured `fabrication_flags.json`:

```text
source_paper_path =
  "stage-19/scientific_claim_paper_revised.md"

source_paper_sha256 =
  SHA256(Snapshot A held-fd exact source-paper bytes)

stage19_publication_mode =
  "structured-scientific-claim-v1"

stage19_publication_binding_path =
  "stage-19/scientific_claim_authority_manifest.json"

stage19_publication_binding_sha256 =
  SHA256(Snapshot A held-fd exact Stage 19 manifest bytes)
```

The manifest and degradation signal express the same binding as exact
`FileRef`s named `source_paper` and `stage19_authority_manifest`, plus the same
exact `stage19_publication_mode` string. Path spelling, bytes, and digests MUST
equal the five values above.

All five values are independently derived from Snapshot A. A caller, stored
report, stored flags, stored manifest, config, environment variable, or model
response MUST NOT provide, select, or override them. No sixth caller-provided
Stage 19 schema field exists. Instead, mode-dispatched replay MUST require that
the bound Stage 19 manifest has true-integer `schema_version == 2`,
`publication_stage_id == "stage19"`,
`claim_policy_id == "structured-scientific-claim-v1"`, the exact Section 15
shape, and exact independently rebuilt bytes. A boolean version is rejected.

Before any quality-model call, including the `llm is None` branch, Stage 20
MUST:

1. capture held root-parent, run, and `stage-20` writer identities;
2. capture held Snapshot A for the complete Stage 19 six-file namespace and
   all recursively referenced sources;
3. replay Stage 19 schema version `2` and its exact namespace;
4. follow the Stage 17 source manifest and independently rebuild canonical
   evidence, CFS, generation, five facts, and six claims;
5. replay the Stage 18 dual-FileRef binding without granting review prose
   scientific authority;
6. validate the Stage 19 descendant selection;
7. independently rerender and splice the structured paper;
8. rebuild all Stage 19 closure reports and require exact bytes; and
9. recapture the complete source tuple and require the Snapshot A fixpoint.

Structured Stage 20 MUST NOT fall back to Stage 17 selection or paper,
`stage-19/paper_revised.md`, `revision_evidence_binding.json`,
`section_revision_manifest.json`, live prose, or any generic-v1 alternative.
Missing or invalid Stage 19 authority is `FAILED`, uses zero model calls, and
runs structured cleanup. The quality model may assess only the fully replayed
paper. It cannot authorize facts, claims, sentences, connectors, selections,
CFS, generation, publication bindings, fabrication state, or publication.

### 18.2 `quality_report.json` schema v1

Structured `stage-20/quality_report.json` uses true-integer schema version `1`
and exactly this root and nested shape:

```json
{
  "schema_version": 1,
  "canonical_experiment_evidence": {
    "path": "canonical_experiment_evidence.json",
    "sha256": "<sha256>"
  },
  "cfs": {
    "schema_version": 1,
    "sha256": "<sha256>"
  },
  "generation_binding_sha256": "<sha256>",
  "source_paper_path": "stage-19/scientific_claim_paper_revised.md",
  "source_paper_sha256": "<sha256>",
  "stage19_publication_mode": "structured-scientific-claim-v1",
  "stage19_publication_binding_path": "stage-19/scientific_claim_authority_manifest.json",
  "stage19_publication_binding_sha256": "<sha256>",
  "score_1_to_10": 8.0,
  "verdict": "proceed",
  "strengths": ["<nonempty diagnostic string>"],
  "weaknesses": ["<nonempty diagnostic string>"],
  "required_actions": ["<nonempty diagnostic string>"],
  "generated": "2026-07-27T00:00:00+00:00"
}
```

The exact root key set is `schema_version`,
`canonical_experiment_evidence`, `cfs`, `generation_binding_sha256`,
`source_paper_path`, `source_paper_sha256`,
`stage19_publication_mode`, `stage19_publication_binding_path`,
`stage19_publication_binding_sha256`, `score_1_to_10`, `verdict`,
`strengths`, `weaknesses`, `required_actions`, and `generated`.
`canonical_experiment_evidence` is an exact `FileRef`. `cfs` has exactly
`schema_version` and `sha256`, and its version is true integer `1`.
`generation_binding_sha256` and every source binding MUST equal the
independent Stage 19 replay.

`score_1_to_10` is a finite JSON integer or number in `[0, 10]`; a boolean,
string, null, non-finite token, or out-of-range value is rejected. `verdict`
is exactly `proceed`, `revise`, or `reject`. The three diagnostic arrays are
arrays of nonempty strings and preserve provider order; duplicate array
elements retain the existing qualitative semantics and are not scientific
authority. `generated` is a code-owned nonempty UTC timestamp and is not an
outcome or scientific-authority oracle. The generic optional `criteria`
object is forbidden in this structured v1 schema.

The quality-model response has exactly five keys: `score_1_to_10`, `verdict`,
`strengths`, `weaknesses`, and `required_actions`. Only score and verdict can
affect the deterministic quality outcome. The other three values are bounded
diagnostic prose; weaknesses may be copied into the non-authoritative
degradation signal. Code injects every schema, evidence, CFS, generation,
source, publication, and timestamp field after strict response parsing. A
provider-supplied authority field is an extra field and is rejected.

Duplicate, extra, or missing root or nested keys; a wrong or boolean version;
an unsafe path; a noncanonical digest; and a binding mismatch all fail before
publication. Report bytes are authority only when the v2 manifest binds them
and the complete expected rebuild succeeds. They never become
scientific-claim authority.

### 18.3 `fabrication_flags.json` schema v3

Structured `stage-20/fabrication_flags.json` uses true-integer schema version
`3` and exactly this root and nested shape:

```json
{
  "schema_version": 3,
  "canonical_experiment_evidence": {
    "path": "canonical_experiment_evidence.json",
    "sha256": "<sha256>"
  },
  "cfs": {
    "schema_version": 1,
    "sha256": "<sha256>"
  },
  "generation_binding_sha256": "<sha256>",
  "source_paper_path": "stage-19/scientific_claim_paper_revised.md",
  "source_paper_sha256": "<sha256>",
  "stage19_publication_mode": "structured-scientific-claim-v1",
  "stage19_publication_binding_path": "stage-19/scientific_claim_authority_manifest.json",
  "stage19_publication_binding_sha256": "<sha256>",
  "experiment_failed": false,
  "quality_score": 8.0,
  "real_metric_values": ["0.875"],
  "verified_values_count": 1,
  "verified_conditions": ["proposed"],
  "has_real_data": true,
  "fabrication_suspected": false
}
```

The exact root key set is `schema_version`,
`canonical_experiment_evidence`, `cfs`, `generation_binding_sha256`, the
five flat source/publication fields from Section 18.1, `experiment_failed`,
`quality_score`, `real_metric_values`, `verified_values_count`,
`verified_conditions`, `has_real_data`, and `fabrication_suspected`.
Evidence, CFS, generation, and all five source values have the same exact
meaning as the report and MUST replay independently.

`quality_score` is the exact finite `[0, 10]` report score. It is the only
model-derived value in this file. `experiment_failed`, `has_real_data`, and
`fabrication_suspected` are true JSON booleans. `verified_values_count` is a
true nonnegative integer. `real_metric_values` is the sorted, duplicate-free
array of canonical decimal strings independently rebuilt from canonical
evidence. `verified_conditions` is the sorted, duplicate-free array of
nonempty canonical condition IDs.

The deterministic relations are exact:

```text
has_real_data == (verified_values_count > 0)
experiment_failed == (not has_real_data)
fabrication_suspected == experiment_failed
```

Structured scientific-claim replay establishes what the paper is allowed to
say; fabrication reconstruction independently establishes whether canonical
real-data and numeric support exist. Neither substitutes for the other. The
quality response cannot change any deterministic fabrication field. A stored
flags file, even if synchronously rehashed into a forged report and manifest,
is never a Stage 19 replay oracle. Code independently reconstructs state from
held canonical evidence and requires exact bytes.

Duplicate, extra, or missing fields; a wrong or boolean version; unordered or
duplicate canonical arrays; noncanonical decimals; inconsistent booleans or
counts; or any binding mismatch fails closed.

### 18.4 `quality_gate_manifest.json` schema v2

Structured `stage-20/quality_gate_manifest.json` uses true-integer schema
version `2`. A passed manifest has exactly this shape:

```json
{
  "schema_version": 2,
  "publication_stage_id": "stage20",
  "stage19_publication_mode": "structured-scientific-claim-v1",
  "canonical_experiment_evidence": {
    "path": "canonical_experiment_evidence.json",
    "sha256": "<sha256>"
  },
  "cfs": {
    "schema_version": 1,
    "sha256": "<sha256>"
  },
  "generation_binding_sha256": "<sha256>",
  "source_paper": {
    "path": "stage-19/scientific_claim_paper_revised.md",
    "sha256": "<sha256>"
  },
  "stage19_authority_manifest": {
    "path": "stage-19/scientific_claim_authority_manifest.json",
    "sha256": "<sha256>"
  },
  "quality_report": {
    "path": "stage-20/quality_report.json",
    "sha256": "<sha256>"
  },
  "fabrication_flags": {
    "path": "stage-20/fabrication_flags.json",
    "sha256": "<sha256>"
  },
  "outcome": "passed",
  "quality_verdict": "proceed",
  "quality_score": "8",
  "quality_threshold": "6",
  "graceful_degradation": true,
  "degradation_signal": null,
  "generated": "2026-07-27T00:00:00+00:00"
}
```

The exact root key set is `schema_version`, `publication_stage_id`,
`stage19_publication_mode`, `canonical_experiment_evidence`, `cfs`,
`generation_binding_sha256`, `source_paper`,
`stage19_authority_manifest`, `quality_report`, `fabrication_flags`,
`outcome`, `quality_verdict`, `quality_score`, `quality_threshold`,
`graceful_degradation`, `degradation_signal`, and `generated`. Every object
named as a file is an exact two-key `FileRef`. `cfs` has exactly
`schema_version` and `sha256`.

`publication_stage_id` is exact `stage20`; the publication mode is exact
`structured-scientific-claim-v1`. The evidence, CFS, generation, paper, and
Stage 19 manifest values are independently derived from Snapshot A and must
equal the bindings in the report and flags. The Stage 19 authority manifest
is independently rebuilt and replayed; mutual hash consistency among stored
Stage 20 files is not an oracle.

`quality_report` and `fabrication_flags` bind the exact already-published
bytes. `quality_score` and `quality_threshold` are canonical decimal strings,
while the report score remains the exact finite JSON number from which
`quality_score` is canonically derived. `quality_verdict` equals the report
verdict. `graceful_degradation` is the exact canonical-config boolean.
`generated` is code-owned and non-authoritative. Every generated timestamp is
issued exactly once into the live verified transaction context before its
object is serialized. Expected rebuild receives that held context value; it
MUST NOT rediscover a timestamp from a stored report, flags, signal, or
manifest.

`outcome` is exactly `passed` or `degraded` and is independently derived:

```text
passed =
  has_real_data
  and not fabrication_suspected
  and quality_verdict == "proceed"
  and quality_score >= quality_threshold

degraded =
  has_real_data
  and not fabrication_suspected
  and quality_verdict == "revise"
  and quality_score < quality_threshold
  and graceful_degradation is true
```

Every other combination has no publishable outcome. In particular, `reject`
never publishes at any score; `proceed` below threshold and `revise` at or
above threshold are contradictions; and no-real-data or suspected
fabrication always blocks.

For `passed`, `degradation_signal` is JSON null and both root
`degradation_signal.json` and `degradation_signal.json.tmp` MUST be absent.
For `degraded`, `degradation_signal` is an exact `FileRef` whose path is
`degradation_signal.json`; that root file MUST exist as a regular,
single-link file with the exact bound bytes. Missing, extra, stale, wrong-path,
wrong-hash, null/FileRef branch mismatch, or a signal for `passed` fails
closed.

The manifest contains no path, digest, size, or identity for itself. Snapshot
A/B and the executor hold those external facts. This avoids a self-hash
cycle. The manifest is published last as the Stage 20 commit candidate and is
accepted as the current authority commit point only by an independent expected
rebuild of every field and every referenced byte.

### 18.5 Exact structured degradation signal

The degraded-only root `degradation_signal.json` is operational evidence that
the run is degraded. It is not scientific authority and is never included in
the Stage 20 artifact or evidence-ref tuple. It uses true-integer schema
version `1` and exactly this shape:

```json
{
  "schema_version": 1,
  "publication_stage_id": "stage20",
  "outcome": "degraded",
  "stage19_publication_mode": "structured-scientific-claim-v1",
  "canonical_experiment_evidence": {
    "path": "canonical_experiment_evidence.json",
    "sha256": "<sha256>"
  },
  "cfs": {
    "schema_version": 1,
    "sha256": "<sha256>"
  },
  "generation_binding_sha256": "<sha256>",
  "source_paper": {
    "path": "stage-19/scientific_claim_paper_revised.md",
    "sha256": "<sha256>"
  },
  "stage19_authority_manifest": {
    "path": "stage-19/scientific_claim_authority_manifest.json",
    "sha256": "<sha256>"
  },
  "quality_report": {
    "path": "stage-20/quality_report.json",
    "sha256": "<sha256>"
  },
  "fabrication_flags": {
    "path": "stage-20/fabrication_flags.json",
    "sha256": "<sha256>"
  },
  "quality_verdict": "revise",
  "quality_score": "4",
  "quality_threshold": "6",
  "weaknesses": ["<nonempty diagnostic string>"],
  "generated": "2026-07-27T00:00:00+00:00"
}
```

The exact root key set is `schema_version`, `publication_stage_id`, `outcome`,
`stage19_publication_mode`, `canonical_experiment_evidence`, `cfs`,
`generation_binding_sha256`, `source_paper`,
`stage19_authority_manifest`, `quality_report`, `fabrication_flags`,
`quality_verdict`, `quality_score`, `quality_threshold`, `weaknesses`, and
`generated`. FileRef, CFS, digest, decimal-string, diagnostic-array, and
generated-field rules are identical to the corresponding manifest and report
rules. `outcome` is exact `degraded`; `quality_verdict` is exact `revise`.
The signal is published before the manifest, then captured, hashed, bound by
the manifest, and included in final replay and fixpoint checks.

### 18.6 Exact namespace and publication lifecycle

The three canonical Stage 20 success names are exactly:

```text
quality_report.json
fabrication_flags.json
quality_gate_manifest.json
```

Structured Stage 20 has no staging directory. Its four temporary ordinary-file
names are exactly:

```text
stage-20/quality_report.json.tmp
stage-20/fabrication_flags.json.tmp
stage-20/quality_gate_manifest.json.tmp
degradation_signal.json.tmp
```

Each temporary is a regular file created through the already-held Stage 20 or
run-root directory fd with exact `O_CREAT|O_EXCL|O_NOFOLLOW`. The creating
`openat` returns the only fd through which content may be written. The
transaction immediately records that fd's `fstat` identity and MUST NOT reopen
the temporary by path for writing. A pre-existing entry at any temporary name,
whether a regular file, directory, symlink, FIFO, socket, device, or other
special file, is a collision: admission fails before writing and MUST NOT
overwrite, follow, enter, or otherwise mutate the collision target through the
normal publication path. Later failure cleanup may unlink the injected
reserved-name directory entry under the fixed-name rules below; it never
follows or modifies the entry's target.

Before publication, the transaction requires every temporary path still to
bind the identity recorded from its creating fd. Missing names, replacement,
or any identity mismatch fail closed and block normal publication. Formal
publication does not link, rename, or reopen a temporary path. Instead, each
absent formal target is created through its held directory fd with exact
`O_CREAT|O_EXCL|O_NOFOLLOW`, its creating-fd identity is recorded, and the
already-held temporary bytes are copied only into that newly created formal
fd. A replacement after formal creation therefore receives no content writes,
and identity verification cannot confer publication authority on it. Any
formal target that exists or reappears after admission invalidation makes the
exclusive create fail before content writing. No random or alternate staging
name and no platform-specific publication primitive is authorized. This
four-file ordinary-file transaction is specific to Stage 20. Stage 19 retains
its existing exact staging-directory design unchanged.

B4-D3 explicitly corrects the deletion claim made by B4-D2. Portable
POSIX/macOS provides no atomic `unlink-if-inode`; an identity check followed by
`unlinkat` is not an OS-level identity-conditional delete. Identity checks
detect in-scope drift, reject publication, and support diagnostics. They MUST
NOT be described as proof that a later `unlinkat` removed the checked inode
against an excluded malicious syscall race. Inside the trusted-local boundary,
cooperating writers do not mutate the name between the check and cleanup.

The writer epoch coordinates all in-model ResearchClaw writers. An arbitrary
noncooperating same-UID process racing these operations is outside Section
1.1 and requires OS isolation. Every consumer nevertheless captures through
held directory fds and repeats strict schema, source binding, generation,
Snapshot/fixpoint, and semantic replay on every consumption because stored
artifacts, crash residue, and cooperating-lifecycle faults remain untrusted.

The three formal Stage 20 names, the four exact temporary names, the root
signal name, and the two diagnostic names are a code-owned reserved namespace.
Cleanup MAY call descriptor-relative `unlinkat` on fixed reserved names.
Identity comparison before cleanup remains drift evidence and a trusted-local
check; it does not create an atomic OS unlink-if-inode primitive.

For this threat model, `external-zero-write/delete` means all of the following
and nothing stronger:

- never follow or modify a symlink target;
- never recursively enter or delete unknown replacement-directory contents;
- never rediscover or write through a replacement path after held-fd capture;
- never modify a path outside the code-owned reserved namespace; and
- a fixed reserved-name entry is code-owned namespace state and may itself be
  removed by the defined cleanup.

A reserved-name directory collision is not recursively removed. A symlink,
FIFO, socket, device, or other special-file collision is never opened for
content I/O. Cleanup attempts the fixed-name non-following operation that is
safe for that entry type, aggregates any refusal or error, and continues
checking the other reserved names. If a reserved name cannot be reduced to a
strict regular manifest whose bytes, schema, source binding, generation, and
complete semantic replay all succeed, no successful Stage 20 authority exists.

This correction changes only the Stage 20 operating-system threat and cleanup
claims. It does not relax quality semantics, source authority, any frozen
schema or outcome, capability `1000`, Stage 21-25 deferral, or any release
check or gate.

The only persisted Stage 20 diagnostic sidecar names are:

```text
quality_gate_llm_diagnostics.json
quality_gate_llm_diagnostics.json.tmp
```

Diagnostics are accumulated in memory during an attempt. A bounded
`quality_gate_llm_diagnostics.json` may be published only after a structured
failure and its aggregate success-authority cleanup; it is non-authoritative
and excluded from every manifest and StageResult tuple. Its temporary name
MUST be absent after diagnostic publication. Admission independently
invalidates both diagnostic names so stale failure diagnostics cannot affect a
later attempt. Structured success requires both diagnostic names absent; the
exact successful direct namespace therefore contains only the three authority
files. No raw provider response, prompt, invalid report, repair response, or
free-form diagnostic name may persist. The root
`degradation_signal.json` belongs to the Stage 20 writer epoch even though it
is outside `stage-20`; it is present only for a committed degraded outcome.

Structured Stage 20 permits at most two semantic quality calls: one initial
call and, only after a syntactically received but empty, truncated, malformed,
duplicate-key, wrong-field, wrong-type, or otherwise invalid response, one
repair call. A valid first response uses one semantic call. A transport failure
does not consume the semantic repair branch. Each semantic call permits at
most one identical retry after a connection error, timeout, retryable 429, or
retryable 5xx before response content, for a total maximum of four outbound
attempts. The retry endpoint, model, prompt bytes, JSON mode, token limit, and
request fingerprint are identical. Once any response content is received,
transport retry is forbidden. A valid parsed response completes that semantic
call; an invalid first response may enter the one repair call. A
non-retryable error, second transport failure, second invalid response, third
semantic call, or fifth outbound attempt fails immediately. No hidden model,
endpoint, credential, or prompt fallback is allowed.

Structured publication follows this exact order:

1. reject public/direct structured admission before I/O, lock, or model access
   unless the complete code-owned capability is exact `1111`;
2. for the B4-B private seam, require a live under-lock non-forgeable verified
   context before any structured source or output I/O;
3. hold root-parent, run, stage, source, and output descriptors and reject
   unsafe identities or file types;
4. perform authority-first invalidation by attempting descriptor-relative
   `unlinkat` on the fixed manifest name first, then on the other formal,
   temporary, root-signal, and diagnostic reserved names; never follow a
   symlink or recursively enter a directory collision;
5. aggregate every cleanup collision or error; a collision cannot hide
   another owned name;
6. capture Snapshot A and complete all source replay and fixpoint checks;
7. only after replay succeeds, call the quality model within the exact
   two-semantic/four-outbound bound and strictly parse its five-field response;
8. issue each code-owned timestamp once into the live verified transaction
   context, then deterministically construct report v1, flags v3, the outcome,
   and all expected manifest fields in memory;
9. create the report and flags temporary ordinary files, and only for a
   degraded outcome the root signal temporary ordinary file, through held
   directory fds with exact `O_CREAT|O_EXCL|O_NOFOLLOW`; record each creating
   fd identity and write content only through that fd;
10. replay report, flags, and any degradation signal from their held
    transaction bytes against independent evidence, CFS, generation, source,
    fabrication state, and the null/FileRef outcome branch;
11. recapture the full Snapshot A source tuple and require an exact fixpoint;
12. validate that every existing temporary path still binds its recorded
    identity; for `passed`, require both root signal names absent;
13. create the manifest temporary ordinary file through the held Stage 20 fd,
    record its creating-fd identity, write only through that fd, and replay its
    held bytes against the independently rebuilt expected manifest;
14. validate the manifest temporary identity, then formally publish report
    first, flags second, and the degraded root signal third when present; each
    absent formal name is created with exact
    `O_CREAT|O_EXCL|O_NOFOLLOW`, its creating-fd identity is recorded, and
    exact held temporary bytes are written only through that formal fd;
15. create and write formal `quality_gate_manifest.json` by the same
    creating-fd rule strictly last; the complete, fsynced, identity-matched
    formal manifest is only a publication commit candidate until the complete
    replay and final captures below succeed;
16. replay manifest v2 from held bytes against an independently rebuilt
    expected object, then replay report, flags, and the null/FileRef signal
    branch;
17. after each complete formal entry exists, attempt descriptor-relative
    `unlinkat` on its fixed temporary reserved name; require all four
    temporary names physically absent for success, while treating any
    non-removable or reintroduced entry as `FAILED`/DoS rather than authority;
18. capture Snapshot B over the complete source tuple, final Stage 20
    namespace, root signal state, and root-parent/run/stage identities;
19. capture the same final state a second time and require exact equality;
20. require Snapshot A source bytes, digests, sizes, modes, link counts, and
    identities to equal the source portion of Snapshot B; and
21. require both diagnostic names absent, publish the live private executor
    context, and return success only after the immediate postcondition
    succeeds.

All reads, writes, captures, invalidations, and cleanup are
descriptor-relative. Direct-path rediscovery is not authority. Temporary
creation and content writes use the creating fd; path lookup is permitted only
to compare the current entry with the recorded identity before formal
creating-fd publication or to operate on a fixed reserved name. Formal content
writes likewise use only the fd returned by the successful exclusive create.
Identity checks do not make a subsequent `unlinkat` identity-conditional.
Symlink, FIFO, socket, device, directory collision, hardlink
(`st_nlink != 1`), unsafe path, late name collision, missing or extra direct
entry, identity change, or parent replacement detected at an in-scope phase
boundary fails closed. Parent/name replacement tests are deterministic
held-fd fault injection or cooperating-lifecycle checks, not claims against
the excluded same-UID syscall adversary.

Authority-first cleanup attempts the fixed manifest reserved name before the
other formal, temporary, root-signal, and diagnostic names on every structured
failure. It MAY unlink a non-directory entry at a fixed reserved name even
after identity drift; this is a namespace cleanup operation, not an
identity-conditional delete. It MUST NOT follow a symlink, recursively enter a
directory, write through a replacement path, or operate outside the reserved
namespace. Directory and unsafe special-file collisions are retained with
aggregated diagnostics when a non-following fixed-name unlink is not safe or
fails.

`Authority-first invalidation` and `no stale authority` mean that no manifest
at the reserved Stage 20 manifest name can pass the complete strict schema,
source binding, generation binding, Snapshot/fixpoint, and semantic replay.
The mechanical authority commit point is therefore a currently captured
strict regular manifest at that exact reserved name that passes this complete
replay, not merely a file created by the producer or a producer's earlier
successful return.
They do not require every canonical or temporary directory entry to be
physically absent after an ordinary cleanup or crash failure. Cleanup errors
are appended to the original failure and never change failure into success.
Restoring a detached original, leaving a malformed entry, or reintroducing a
reserved name cannot revive authority without a newly captured current
manifest that passes complete replay. This is stored-state recognition, not
historical producer attribution.

### 18.7 Exact terminal outcome matrix

The only structured success tuple is:

```text
artifacts = (
  "quality_report.json",
  "fabrication_flags.json",
  "quality_gate_manifest.json",
)

evidence_refs = (
  "stage-20/quality_report.json",
  "stage-20/fabrication_flags.json",
  "stage-20/quality_gate_manifest.json",
)
```

The three manifest-bound files are quality-gate authority. They are not
scientific-claim authority. The degradation signal is operational
non-authority and is excluded from both tuples. Every structured failure has
`status=FAILED`, `decision="retry"`, `artifacts=()`, and
`evidence_refs=()`. `decision=None` is forbidden, and the publication-mode
string `structured-scientific-claim-v1` is never a quality outcome.

| Case | Status / decision | Report | Flags | Manifest | Root signal | Authority and downstream |
|---|---|---|---|---|---|---|
| `proceed` and score at or above threshold | `DONE` / `proceed` | required | required | required v2 `passed` | forbidden; manifest field null | exact success tuples; not consumable by current Stage 21 |
| `revise`, low score, graceful degradation true | `DONE` / `degraded` | required | required | required v2 `degraded` | required exact bound file | exact success tuples; degraded operational state; not consumable by current Stage 21 |
| `reject` at any score | `FAILED` / `retry` | cleanup attempted | cleanup attempted | invalidated first | cleanup attempted | no replayable authority; diagnostics only; empty tuples; no downstream |
| verdict/score contradiction | `FAILED` / `retry` | cleanup attempted | cleanup attempted | invalidated first | cleanup attempted | no replayable authority; diagnostics only; empty tuples; no downstream |
| fabrication suspected or no real data | `FAILED` / `retry` | cleanup attempted | cleanup attempted | invalidated first | cleanup attempted | no replayable authority; deterministic block; empty tuples; no downstream |
| Stage 19 replay or source fixpoint failure | `FAILED` / `retry` | not created; collision cleanup attempted | not created; collision cleanup attempted | invalidated first | cleanup attempted | no replayable authority; zero model calls; empty tuples; no downstream |
| quality transport or strict parser failure | `FAILED` / `retry` | cleanup attempted | cleanup attempted | invalidated first | cleanup attempted | no replayable authority; bounded diagnostic sidecar may remain; empty tuples; no downstream |
| `llm is None` | `FAILED` / `retry` | not created; collision cleanup attempted | not created; collision cleanup attempted | invalidated first | cleanup attempted | no replayable authority; replay still required, zero calls, no default report or fallback |
| post-publication replay, Snapshot B, or final-capture failure | `FAILED` / `retry` | cleanup attempted | cleanup attempted | invalidated first | cleanup attempted | no replayable authority; diagnostic only; empty tuples; no downstream |
| immediate or terminal executor postcondition failure | `FAILED` / `retry` | cleanup attempted | cleanup attempted | invalidated first | cleanup attempted | current-epoch withdrawal is `FAILED` / `retry`, empty tuples, cleared live published context, and manifest-first invalidation; the current pipeline MUST NOT enter Stage 21 |

Report, flags, signal, or manifest bytes found after a failure are injected,
stale, or cleanup-failure evidence, never authority unless a new consumer
captures and validates a complete current manifest by the full strict replay.
A diagnostic sidecar may describe the failure but cannot use a success name or
enter a tuple.

### 18.8 Admission and B4-B pre-activation

At exact `1000`, future `1110`, malformed maps, and partial maps, ordinary
production dispatch remains generic-v1. Public or direct structured Stage 20
entrypoints call the code-owned complete-capability guard as their first
operation and reject before I/O, lock acquisition, model access, cleanup, or
artifact enumeration.

B4-B may exercise structured Stage 20 only through a private, under-lock,
non-forgeable verified context. The context binds the exact Stage 19 v2
manifest and six-file namespace, root-parent/run/stage inode identities,
writer owner and epoch, generation binding, CFS, canonical evidence, Snapshot
A source closure, and structured output namespace. It is live only in the
issuing writer epoch and cannot be serialized, copied, caller-constructed, or
reused.

A fake, copied, reader-mode, inactive, expired, wrong-run, wrong-stage,
wrong-generation, wrong-source, wrong-namespace, or wrong-owner context is
rejected before model or publication. The private seam never makes a B4-B
artifact public production authority. Only exact `1111` plus the exact
domain-v2 discriminator formally enters structured production. Once that
entry begins, every later error is structured `FAILED`; generic fallback is
forbidden.

### 18.9 Executor immediate and terminal postconditions

The executor recognizes a structured Stage 20 attempt only from exact
domain-v2 dispatch or the live private published context, never from artifact
presence, `decision`, config, or stored mode strings.

Immediately after the producer returns and before ordinary output checks,
PRM, HITL, or gate hooks, the executor MUST:

1. if the producer is non-`DONE`, run manifest-first cleanup in the same writer
   epoch and force empty tuples;
2. for `DONE`, require the live private context in published phase;
3. require the exact success artifact and evidence-ref tuples;
4. require the exact three-file report-v1, flags-v3, and manifest-v2 namespace
   with no diagnostic, missing, empty, extra, or temporary entry;
5. replay the Stage 19 source closure and Snapshot A fixpoint;
6. independently rebuild and replay report, flags, outcome, manifest, and the
   passed-null/degraded-FileRef signal branch;
7. require exact Snapshot B bytes, digests, sizes, modes, link counts, inode
   identities, and two-capture equality;
8. require the original root-parent, run, stage, root-signal, namespace, and
   writer epoch identities; and
9. reject any producer `DONE` with missing, empty, reordered, duplicated, or
   mismatched tuples or disk bytes.

After PRM, HITL, gate, checkpoint, and other terminal hooks, the executor runs
the same validator again against the same live context and original writer
epoch. If a hook converts `DONE` to any non-`DONE` status, or if any source,
paper, report, flags, signal, manifest, namespace, tuple, identity, or parent
changes after the immediate check, terminal handling converts the result to
`FAILED`, sets `decision="retry"`, runs aggregate cleanup, and returns empty
tuples.

Late mutation of source, output, or manifest after a final read is caught by
the second final capture or a postcondition. B4-B cleanup invalidates only the
Stage 20 manifest, report, flags, signal, and four exact temporary ordinary
files in the code-owned reserved namespace. Fixed-name cleanup is not
identity-conditional and may unlink an injected reserved-name entry, but it
does not follow symlink targets, recursively enter replacement directories,
write through replacement paths, or touch any non-reserved path.

B4-B creates no persistent or serialized revocation authority. It adds no
revocation registry, tombstone, marker, sidecar, config field, caller-provided
state, or independent live revocation interface. Within the current
writer/executor epoch, withdrawal is expressed only by the complete
combination of:

1. `StageResult.status == FAILED` and `decision == "retry"`;
2. `artifacts == ()` and `evidence_refs == ()`;
3. the live structured Stage 20 published context being cleared;
4. manifest-first aggregate invalidation of the Stage 20 reserved success
   namespace; and
5. the current pipeline not entering Stage 21.

This combination is current-authority state, not a durable historical
tombstone. Files remaining after failed cleanup are not authority unless a new
consumer captures a complete current manifest and passes the full strict
semantic replay. If a fully replayable current manifest is later restored, it
is evaluated anew under the current-manifest commit-point rules; B4-D4 makes no
claim of irreversible historical revocation. B4-B does not enumerate, open,
mutate, or delete any Stage 21+ output.

### 18.10 B5 downstream deferral

B4-B owns only private structured Stage 20 source replay, quality evaluation,
publication, self-replay, postconditions, and invalidation. It MUST NOT modify
Stage 21, Stage 22, Stage 24, independent release reconstruction,
`release_check`, or any release gate.

Current Stage 21 accepts only the generic versionless report, flags v2, and
manifest v1. Current Stage 22 accepts only Stage 19 publication modes
`legacy` and `sectional`. Stage 24 and independent reconstruction inherit that
generic chain, while direct `release_check` score/flag reads are not a
structured schema oracle. Therefore no current Stage 21/22/24 or release
consumer may consume structured Stage 20 output, and existing fields are not
sufficient for safe structured recognition.

B5 MUST independently freeze each downstream schema before its implementation.
Sections 18.11-18.13 now freeze Stage 21-23 design authority; Stage 24-25,
independent reconstruction, and release schemas remain deferred to B5-D2.
Every Stage 21+ structured authority MUST bind:

- the exact current `stage-20/quality_gate_manifest.json` FileRef, including
  its exact path and SHA-256 over held bytes;
- the exact independently replayed `generation_binding_sha256`; and
- the exact Stage 20 source identity, including the source paper and Stage 19
  authority-manifest FileRefs, while retaining the canonical-evidence and CFS
  closure required by complete Stage 20 replay.

Before opening any Stage 21+ path, a B5 consumer MUST use held run and Stage 20
directory fds to capture the current Stage 20 manifest and source closure. It
MUST require the manifest to be present, regular, single-link, strict-schema,
and fully semantically replayable, then require its generation and source
identity to equal the independently rebuilt current-run values. Missing,
non-regular, malformed, incomplete, unreplayable, wrong-generation, or
wrong-source current Stage 20 authority rejects before any downstream artifact
read or write.

Only after that upstream admission succeeds may the consumer capture a
Stage 21+ authority manifest. Before reading or writing any other downstream
artifact, it MUST require that downstream manifest to bind the exact current
Stage 20 manifest FileRef and the same generation/source identity. A missing,
stale, or inconsistent binding rejects the old downstream authority. A stored
downstream hash is not an authority oracle. There is no fallback to Stage 17,
Stage 19, a generic paper, a stored downstream hash, config, caller state, or
artifact presence.

B5 owns Stage 21/22 dual-schema dispatch, structured publication-mode
propagation, Stage 24 binding, independent reconstruction, release-check and
gate integration, and actual invalidation or cleanup of downstream commit
points. Generic-v1 Stage 20, Stage 21, and Stage 22 schemas, paths, bytes, and
behavior remain unchanged. Stale structured Stage 19 or Stage 20 files cannot
discriminate generic dispatch. The current `1110` declaration remains
inactive; only completed B5 integration followed by a
separately reviewed exact `1111` declaration can activate the full structured
path.

### 18.11 B5-D1A structured Stage 21 deterministic archive authority

#### 18.11.1 Boundary, dispatch, and pre-admission

Structured Stage 21 is a deterministic archival projection over one fully
replayed structured Stage 20 success authority. It makes zero LLM calls, zero
provider calls, zero semantic-repair calls, and zero HITL calls. It does not
read a prompt, caller prose, runtime topic, stored summary, paper excerpt, or
live config to construct its outputs.

The exact current code-owned map is `1110`. Ordinary, public, and direct
structured Stage 21 entry therefore calls the complete-capability guard and
rejects before a lock, filesystem I/O, provider, prompt, or HITL operation.
Only a private integration helper may exercise this design before global
activation. That helper initially accepts exactly one code-issued,
non-serializable, non-copyable `PreAdmissionContext`. The private issuance
registry binds it only to:

- the active `ReleaseGraphLock` owner and writer epoch;
- the held root-parent, run, Stage 20, and root-signal-parent descriptors and
  device/inode identities;
- the canonical run path and logical target stage ID exact `stage21`;
- structured capability schema version `1` and exact code-owned snapshot
  `1110`;
- the current Stage 20 manifest, generation binding, source paper, Stage 19
  manifest, canonical evidence, CFS, and complete pre-admission source
  bindings; and
- the single-use phase exact `pre_admission`.

`PreAdmissionContext` MUST NOT open, stat, enumerate, create, roll over, clean,
or otherwise access any `stage-21` path. It MUST NOT contain or bind a Stage 21
directory fd, device, inode, namespace identity, entry-existence fact, or
output identity. The logical target stage ID is a lexical code-owned label, not
a filesystem identity. Whether `stage-21` exists remains unknown until after
complete Stage 20 pre-admission replay.

A caller-created, copied, serialized, expired, reused, cross-run, cross-owner,
cross-epoch, cross-stage, or cross-generation `PreAdmissionContext` rejects
before Stage 20 or Stage 21 I/O. Config, environment, caller mode, persisted
capability snapshot, artifact presence, and a test flag cannot create or
select it.

Before opening, creating, reading, rolling over, cleaning, or writing any
`stage-21` path, the private route performs a `Pre-admission Capture` through
held root/run/Stage 20 descriptors. It captures the exact current:

- `stage-20/quality_report.json`;
- `stage-20/fabrication_flags.json`;
- `stage-20/quality_gate_manifest.json`;
- root `degradation_signal.json` only for the degraded branch; and
- recursively required Stage 19, Stage 17, canonical-evidence, CFS,
  experiment-contract, run-config, and closure sources.

Every captured file is a safe canonical run-relative, regular, single-link
file. Capture records exact bytes, SHA-256, byte length, mode, link count,
device, inode, and stable before/after metadata. Pre-admission duplicate-safely
and strictly replays structured report schema v1, flags schema v3, manifest
schema v2, and the conditional signal schema v1. It independently rebuilds the
Stage 20 outcome, Stage 19 manifest v2, source paper, canonical evidence, CFS,
generation binding, fabrication state, and complete source closure. It
requires the current Stage 20 manifest name still to bind the captured
identity.

Only after that complete replay succeeds and the current Stage 20 manifest
name still binds the captured identity may the code-owned registry consume the
exact live `PreAdmissionContext`. As one registry-only, single-use transition,
under the same owner, canonical run, writer epoch, target stage, capability,
and replayed upstream bindings, it uses the already-held run fd to open or
create the canonical `stage-21`, validates and captures the Stage 21 held fd,
device, inode, and parent/name identity, and only then issues exactly one
non-serializable, non-copyable `Stage21AttemptContext`. A failed open,
creation, identity check, or transition consumes or expires the
`PreAdmissionContext`, closes any newly opened fd, issues no attempt context,
performs no Stage 21 deletion or output-file write, and fails closed. The
transition cannot occur twice and has no caller-visible intermediate token.

`Stage21AttemptContext` is the sole legal context for all later Stage 21
manifest-first invalidation, Snapshot A, build, staging, publication, immediate
postcondition, terminal postcondition, and cleanup. Its registry identity
binds the original pre-admission tuple plus the captured Stage 21 held
fd/device/inode/name identity. Caller code cannot construct, copy, serialize,
replace, phase-advance, or bypass it. A forged context, wrong Stage 21
identity, owner/run/epoch drift, or phase mismatch rejects before any deletion
or write.

The exact successful registry phase order is:

```text
PreAdmissionContext(pre_admission)
  -> Stage21AttemptContext(namespace_bound)
  -> invalidated
  -> snapshot_a
  -> staged
  -> published
  -> provisional_done
  -> immediate_validated
  -> terminal_validated
  -> cleared
```

Every arrow is code-owned, one-way, and permitted once. From any
`Stage21AttemptContext` phase, failure instead moves through
`failed_cleanup -> cleared`; cleanup errors append to the original failure.
Neither a cleared/failed context nor the consumed `PreAdmissionContext` can be
revived or reused.

For `passed`, the Stage 20 manifest signal is null and the root signal and its
temporary name are absent. For `degraded`, the signal is an exact `FileRef` to
root `degradation_signal.json`, that regular single-link file is present and
fully replayed, and its temporary name is absent. A missing, non-regular,
hard-linked, malformed, duplicate-key, extra-field, wrong-version,
wrong-source, wrong-generation, mixed-generation, identity-drifted, or
semantically unreplayable source fails with zero Stage 21 output-path I/O.
There is no fallback to Stage 17, Stage 19, a generic paper, config, caller
state, a stored Stage 21 index, or archive prose.

#### 18.11.2 Exact namespace and deterministic `archive.md`

The complete structured Stage 21 owned namespace is exactly:

```text
stage-21/archive.md
stage-21/bundle_index.json
stage-21/archive.md.tmp
stage-21/bundle_index.json.tmp
```

The two `.tmp` names are fixed Stage 21 ordinary-file temporaries. No staging
directory, random name, diagnostic sidecar, alternate suffix, symlink,
hardlink, rename-based commit, or platform-specific publication primitive is
permitted. Structured Stage 21 has no persisted diagnostics. Diagnostics are
bounded in-memory data, exception notes, or `StageResult.error` text only.

`archive.md` is a bound payload, not a manifest and not scientific-claim,
numeric, citation, paper, or release authority. Stage 22 may later consume only
a strict replay of `bundle_index.json` v2 and its upstream authority; it MUST
NOT use archive prose as paper text, evidence, numeric support, citation
support, a dispatch discriminator, or an authority oracle.

The renderer has exactly the following grammar:

```text
# Structured Stage 21 Archive

## Policy
Archive-Schema-Version: 1
Publication-Stage-ID: stage21
Publication-Mode: structured-scientific-claim-v1

## Stage 20 Outcome
Quality-Outcome: <passed|degraded>
Degradation-Signal-Path: <null|degradation_signal.json>
Degradation-Signal-SHA256: <null|sha256>

## Source Bindings
Canonical-Evidence-Path: canonical_experiment_evidence.json
Canonical-Evidence-SHA256: <sha256>
Source-Paper-Path: stage-19/scientific_claim_paper_revised.md
Source-Paper-SHA256: <sha256>
Stage19-Manifest-Path: stage-19/scientific_claim_authority_manifest.json
Stage19-Manifest-SHA256: <sha256>
Stage20-Manifest-Path: stage-20/quality_gate_manifest.json
Stage20-Manifest-SHA256: <sha256>
Generation-Binding-SHA256: <sha256>
CFS-Path: null
CFS-Schema-Version: 1
CFS-SHA256: <sha256>

## Artifact Inventory
Artifact-Count: <7|8>

## Authority Boundary
This archive is a deterministic operational projection only; it is not scientific claim, numeric, or citation authority.
```

Angle-bracket expressions are grammar metavariables, not stored characters.
The renderer substitutes only values from Snapshot A. The passed branch writes
literal `null` for both degradation lines and literal `7` for the count. The
degraded branch writes literal `degradation_signal.json`, its lowercase
SHA-256, and literal `8`.

CFS is not a standalone persisted file. `CFS-Path: null` is therefore exact
and MUST NOT be replaced by an invented path. The CFS schema and digest are
independently rebuilt from canonical evidence.

The complete archive uses the displayed line order and spelling, ASCII
characters encoded as UTF-8, LF line endings, no BOM, no CR, no trailing
spaces, no Unicode normalization step, and exactly one terminal LF. It
contains no timestamp. Paths and digests are already canonical ASCII tokens
with no newline or whitespace. Public replay independently rerenders these
exact bytes. The renderer never copies a paper paragraph, provider response,
review, weakness, recommendation, future work, comparison, causal statement,
mechanism, generalization, scientific conclusion, caller string, config
string, or stored summary.

#### 18.11.3 Exact `bundle_index.json` schema v2

`stage-21/bundle_index.json` is the sole structured Stage 21 manifest and
commit point. It uses true-integer schema version `2` and exactly 17 root keys.
A passed example is:

```json
{
  "schema_version": 2,
  "publication_stage_id": "stage21",
  "publication_mode": "structured-scientific-claim-v1",
  "structured_capability_schema_version": 1,
  "structured_capability_snapshot": {
    "stage17_publication": 1,
    "stage19_revision": 1,
    "stage20_replay": 1,
    "stage24_and_release_integration": 0
  },
  "generation_binding_sha256": "<sha256>",
  "canonical_experiment_evidence": {
    "path": "canonical_experiment_evidence.json",
    "sha256": "<sha256>"
  },
  "cfs": {
    "schema_version": 1,
    "sha256": "<sha256>"
  },
  "source_stage19_manifest": {
    "path": "stage-19/scientific_claim_authority_manifest.json",
    "sha256": "<sha256>"
  },
  "source_stage20_manifest": {
    "path": "stage-20/quality_gate_manifest.json",
    "sha256": "<sha256>"
  },
  "source_paper": {
    "path": "stage-19/scientific_claim_paper_revised.md",
    "sha256": "<sha256>"
  },
  "quality_outcome": "passed",
  "degradation_signal": null,
  "archive": {
    "path": "stage-21/archive.md",
    "sha256": "<sha256>"
  },
  "artifact_count": 7,
  "artifacts": [
    {
      "role": "canonical_experiment_evidence",
      "path": "canonical_experiment_evidence.json",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "source_stage19_manifest",
      "path": "stage-19/scientific_claim_authority_manifest.json",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "source_paper",
      "path": "stage-19/scientific_claim_paper_revised.md",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "stage20_quality_report",
      "path": "stage-20/quality_report.json",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "stage20_fabrication_flags",
      "path": "stage-20/fabrication_flags.json",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "source_stage20_manifest",
      "path": "stage-20/quality_gate_manifest.json",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "stage21_archive",
      "path": "stage-21/archive.md",
      "sha256": "<sha256>",
      "size": 1
    }
  ],
  "generated": "2026-07-28T00:00:00+00:00"
}
```

The exact root key set is `schema_version`, `publication_stage_id`,
`publication_mode`, `structured_capability_schema_version`,
`structured_capability_snapshot`, `generation_binding_sha256`,
`canonical_experiment_evidence`, `cfs`, `source_stage19_manifest`,
`source_stage20_manifest`, `source_paper`, `quality_outcome`,
`degradation_signal`, `archive`, `artifact_count`, `artifacts`, and
`generated`. Duplicate, unknown, or missing root or nested keys reject.

`publication_stage_id` is exact `stage21`; `publication_mode` is exact
`structured-scientific-claim-v1`. The capability schema is true integer `1`.
The snapshot has exactly the four code-owned keys and, for B5 implementation
and private pre-activation integration, exact true-integer values `1110`.
Persisted `1110` records the private admission state but never activates
public or direct structured entry. After a separately reviewed global
activation, a new public publication must bind the then-current exact `1111`;
an old `1110` manifest cannot be promoted or reused.

`canonical_experiment_evidence`, `source_stage19_manifest`,
`source_stage20_manifest`, `source_paper`, `degradation_signal` when non-null,
and `archive` are exact two-key `FileRef`s. `cfs` is not a `FileRef`; it has
exactly `schema_version` and `sha256`, and the version is true integer `1`.
There is no authoritative standalone CFS path.

Each artifact inventory entry has exactly `role`, `path`, `sha256`, and `size`.
`size` is a true nonnegative integer, never a boolean, and equals both the held
byte length and stable `fstat` size. Paths are safe NFC run-relative paths;
digests are lowercase SHA-256. Source and output files are regular,
single-link, stable-identity files. JSON sources are nonempty, duplicate-safe
strict JSON; the source paper is nonempty valid UTF-8; the archive is exactly
the nonempty deterministic bytes from Section 18.11.2. A role is the logical
type; no separate caller-supplied MIME or type field exists.

The passed role array is exactly this order:

```text
canonical_experiment_evidence
source_stage19_manifest
source_paper
stage20_quality_report
stage20_fabrication_flags
source_stage20_manifest
stage21_archive
```

The degraded role array has exactly eight entries and inserts
`stage20_degradation_signal` after `stage20_fabrication_flags` and before
`source_stage20_manifest`. Its path is exact root
`degradation_signal.json`. No role may repeat.

For every root `FileRef`, exactly one inventory row with its prescribed role
has the same path and digest. The canonical-evidence, Stage 19, source-paper,
Stage 20 manifest, conditional signal, and archive rows cross-match their root
fields. The report and flags rows cross-match the exact `FileRef`s inside the
fully replayed Stage 20 manifest. Their sizes come from held bytes. The
inventory is the exact directly bound Stage 21 set; the recursively replayed
Stage 17/19 source closure remains transitively bound by the independently
rebuilt Stage 19 and Stage 20 manifests and is not redundantly copied here.

For `passed`, `quality_outcome` is exact `passed`, `degradation_signal` is
null, the signal and signal temporary are absent, the count is true integer
`7`, and the seven-role array is exact. For `degraded`, the outcome is exact
`degraded`, the signal is its exact `FileRef`, the signal is present and fully
replayed, the count is true integer `8`, and the eight-role array is exact.
Unknown outcomes, crossed branches, a passed signal, a degraded null, or
wrong count/order/path/hash/size fail closed.

`generated` is not a new Stage 21 clock reading. It is exactly the
seconds-resolution UTC `generated` string in the fully replayed current
Stage 20 manifest, with exact form `YYYY-MM-DDTHH:MM:SS+00:00`. `Z`,
fractional seconds, other offsets, invalid calendar values, or a value that
differs from Stage 20 reject. Stage 21 makes zero clock calls. The value is a
non-authoritative deterministic source-generation timestamp and cannot select
content, outcome, dispatch, or authority.

Authority JSON follows Section 3: duplicate-safe strict parsing, sorted object
keys, no insignificant whitespace, UTF-8, and one terminal LF. The displayed
field order is explanatory; the array role order is normative. The manifest
contains no field for its own path, hash, size, or identity. `artifacts` also
excludes the manifest. Consumers capture and hash its exact bytes externally,
so there is no manifest self-hash cycle.

#### 18.11.4 Exact publication lifecycle

Pre-admission and Snapshot A are two distinct full captures. Snapshot A is a
post-invalidation revalidation and not the first Stage 20 admission:

1. **Dispatch first.** Ordinary/public/direct entry requires exact `1111` and
   rejects current `1110` before lock, filesystem, provider, prompt, or HITL
   I/O. Only the code-owned private route may continue.
2. **Pre-admission context.** Validate the single-use registry-issued
   `PreAdmissionContext`, its upstream-only owner/run/epoch/identity/source
   bindings, logical target stage, capability schema, and exact `1110`.
   Assert that it has no Stage 21 fd, identity, existence fact, or output
   binding; caller/config/environment/artifact presence cannot supply any
   value.
3. **Pre-admission capture.** Through held upstream descriptors only, capture
   Stage 20 report, flags, manifest, conditional root signal, and the complete
   recursively required source closure as rich regular-file snapshots.
4. **Pre-admission replay.** Fully replay Stage 20 and independently rebuild
   Stage 19, evidence, CFS, generation, fabrication, source, namespace, and
   outcome. Require the current Stage 20 manifest name still to bind its
   captured identity. Failure has zero `stage-21` I/O.
5. **Single transition and first Stage 21 open.** Only now may the code-owned
   registry consume the exact `PreAdmissionContext`. In one same-owner,
   same-run, same-epoch transition it opens or creates the current canonical
   `stage-21` through the held run descriptor, captures and validates its held
   fd/device/inode/name identity, and then issues the sole
   `Stage21AttemptContext` in `namespace_bound`. No Stage 21 stat, enumeration,
   mkdir, rollover, cleanup, deletion, metadata write, or output write may
   precede this operation. Transition failure issues no attempt context and
   performs no deletion or output-file write.
6. **Manifest-first invalidation.** Descriptor-relatively attempt
   `bundle_index.json` first, then independently clean `archive.md`,
   `bundle_index.json.tmp`, and `archive.md.tmp`. Preexisting temporary
   collisions are errors even when fixed-name cleanup removes their
   non-directory entries. Aggregate all cleanup errors and do not publish after
   any error. The persisted diagnostic set is empty. This and every later step
   requires the same live `Stage21AttemptContext` and its next legal registry
   phase.
7. **Snapshot A.** Recapture and fully replay the upstream closure through the
   held descriptors. Require exact equality with Pre-admission Capture in
   bytes, hashes, sizes, modes, link counts, device/inode identities,
   generation, source bindings, branch, and namespace. This second capture is
   the formal Snapshot A.
8. **Deterministic build.** With zero LLM, provider, repair, HITL, prompt, and
   clock calls, build exact archive and v2 manifest bytes in memory from
   Snapshot A.
9. **Archive temporary.** Create fixed `archive.md.tmp` with
   `O_CREAT|O_EXCL|O_NOFOLLOW`, require a regular single-link file, record its
   creating-fd identity, and write, flush, sync, and read back only through that
   descriptor.
10. **Manifest temporary.** Apply the same creating-fd-only rules to fixed
    `bundle_index.json.tmp`. Do not replace, rename, link, or reopen either
    temporary for writing.
11. **Staged semantic replay.** Read held temporary descriptors, strictly parse
    the index, independently rerender the archive, and require exact bytes,
    root and nested keys, roles/order/count, FileRefs/hash/size, capability,
    branch, timestamp, and no manifest self-reference.
12. **Source fixpoint.** Capture the complete upstream closure again and
    require exact Snapshot A equality and the current Stage 20 manifest-name
    identity. Any late source or current-manifest mutation fails.
13. **Formal archive.** Require `archive.md` absent, create it with
    `O_CREAT|O_EXCL|O_NOFOLLOW`, record the formal creating-fd identity, and
    copy only the exact held archive-temporary bytes through the formal
    creating fd; flush, sync, and read back.
14. **Formal manifest last.** Strictly last, require `bundle_index.json`
    absent, create it with the same exclusive creating-fd-only rules, and copy
    only the exact held manifest-temporary bytes. It is a commit candidate, not
    final success.
15. **Temporaries absent.** Fixed-name descriptor-relative unlink both
    temporaries, require them physically absent, and require the direct Stage 21
    namespace to contain exactly the two formal names.
16. **Final semantic replay.** From held formal descriptors, strictly replay
    the index and independently rebuild both archive and index from Snapshot A.
    Reassert the archive authority boundary.
17. **Snapshot B.** Capture the full upstream closure, both formal outputs,
    root/run/stage/current-Stage20 identities, names, bytes, hashes, sizes,
    modes, and link counts. Require its source portion equal Snapshot A.
18. **Second final capture.** Capture the same complete state again and require
    exact Snapshot B equality, detecting final-read mutation, late collision,
    parent replacement, or name-to-inode drift.
19. **Provisional producer handoff and executor postconditions.** After
    publication and both final captures, the producer advances the same
    `Stage21AttemptContext` to `provisional_done` and gives only the executor an
    internal provisional `DONE`, the exact candidate tuples, and that live
    context. It does not return a public `StageResult` or final `DONE`. The
    context contains no executor result and therefore creates no circular
    result dependency. The executor first runs the immediate validator over
    exact tuples, bytes, namespace, source fixpoint, snapshots, identities, and
    context. It then unconditionally skips PRM, HITL, edit, prompt,
    semantic-repair, and repair hooks for structured Stage 21, and runs the
    terminal validator against the same context and original writer epoch. No
    second published context is issued. A changed provisional status, tuple,
    byte, name, identity, source, phase, or context is failure.
20. **Public DONE.** Only after both postconditions pass may public
    `execute_stage` construct and return final `DONE` with exactly
    `artifacts=("archive.md", "bundle_index.json")` and
    `evidence_refs=("stage-21/archive.md",
    "stage-21/bundle_index.json")`, then clear the live context. No reusable or
    serializable activation capability remains. Failure at the producer,
    handoff, immediate validator, skipped-hook assertion, terminal validator,
    or public-result construction returns `FAILED` / `retry` with empty tuples
    after manifest-first aggregate cleanup.

Temp and formal publication use fixed-name exclusive creating descriptors, not
the existing replace-based generic helper. Cleanup uses descriptor-relative
`unlinkat(fixed_reserved_name)` as a reserved-namespace operation. It does not
claim that a preceding inode check and later unlink form an atomic
unlink-if-inode primitive. Under the Section 1.1 trusted-local boundary,
detected replacement or identity drift causes `FAILED` and cannot authorize
alternate bytes. Cleanup never follows a symlink, recurses into a directory,
writes through a rediscovered parent, changes a non-reserved name, or switches
to a replacement parent. A directory or unsafe special-file collision is
retained as an aggregate cleanup error. Parent replacement fault injection
uses only the held original descriptors.

Rollover, resume, `FAILED`, and `PAUSED` do not inherit Stage 21 authority. Any
rollover may occur only after successful current Stage 20 pre-admission and
inside the registry-only one-time transition under the same owner, run, and
writer epoch; the transition captures the resulting canonical `stage-21`
identity before issuing `Stage21AttemptContext`. The new canonical `stage-21`
starts without a v2 manifest. Archived `stage-21_vN` directories are
historical bytes, not current authority. Resume must repeat dispatch,
pre-admission, the one-time context transition, manifest-first invalidation,
Snapshot A, publication, and both postconditions from the beginning. It cannot
adopt a temporary, formal archive, stored index, issued context, provisional
handoff, or prior producer result. Any non-`DONE` status, including `FAILED`,
`PAUSED`, retry, cancellation, or exception, withdraws the current attempt
through manifest-first aggregate cleanup. PRM, HITL, edit, and repair
conversion cannot occur because structured Stage 21 skips those hooks;
attempting to invoke one is itself an executor contract failure.

#### 18.11.5 Passed, degraded, and failure semantics

| Input or event | Stage 21 result | Manifest/signal branch | Calls | Result tuples |
|---|---|---|---:|---|
| valid Stage 20 `passed` | deterministic `DONE` after both postconditions | v2 passed; null signal; seven roles | 0 | exact success tuples |
| valid Stage 20 `degraded` | deterministic `DONE` after both postconditions | v2 degraded; exact root signal `FileRef`; eight roles | 0 | exact success tuples |
| Stage 20 failed, missing, invalid, or unreplayable | `FAILED` / `retry` before Stage 21 output I/O | no Stage 21 publication | 0 | empty |
| mixed Stage 19/20 generation, source, evidence, CFS, or current manifest | `FAILED` / `retry` before Stage 21 output I/O | no Stage 21 publication | 0 | empty |
| passed with a signal, or degraded without the exact signal | `FAILED` / `retry` before Stage 21 output I/O | no Stage 21 publication | 0 | empty |
| any Stage 21 build, collision, replay, fixpoint, publication, capture, or postcondition failure | `FAILED` / `retry` | manifest-first aggregate cleanup | 0 | empty |
| cleanup also fails | original failure remains primary; cleanup errors append | no success claim; v2 index must be absent or unreplayable at final check | 0 | empty |

A degraded Stage 20 changes only the exact code-owned outcome and signal
projection in the archive and manifest. It does not change facts, claims,
paper, numeric support, citation support, release status, or scientific
authority. The archive remains non-authoritative in both branches. Future
Stage 22 structured admission may accept only a complete replay of the Stage
21 v2 manifest and the current Stage 20 authority; it cannot trust archive
prose.

Every failure attempts cleanup in manifest-first order and returns empty
artifact and evidence tuples. Cleanup error text is appended after the
original error and cannot replace it. Success requires that no complete
replayable current v2 index remains after a failed attempt. This is a
current-epoch operating contract, not historical creator provenance or a
claim against excluded same-UID syscall interference. A later consumer must
re-admit the then-current Stage 20 manifest and independently replay all
expected bytes; it cannot inherit an old producer success, stored
archive/index mutual hashes, or a stale live context.

#### 18.11.6 Generic boundary and required adversarial coverage

Generic-v1 Stage 21 retains its current LLM and no-LLM behavior, bundle-index
schema v1, output names, retries, bytes, and cleanup. Exact `1110` ordinary
dispatch remains generic after the mandatory common canonical replay and
cannot enter the private structured helper. A stale v2 index, deterministic
archive, temporary, domain-v2 artifact, or synchronized hash set cannot select
structured dispatch. Generic-v1 does not parse a v2 index as v1 authority and
structured-only semantics do not alter generic bytes.

B5-D1A implementation tests MUST cover:

1. a spy proving zero `stage-21` open, stat, mkdir, rollover, unlink, cleanup,
   metadata write, or output write before complete Stage 20 pre-admission;
2. missing, plain, copied, serialized, expired, reused, wrong-owner, wrong-run,
   wrong-epoch, wrong-stage, or inactive `PreAdmissionContext`; a caller-made
   or forged `Stage21AttemptContext`; transition reuse; and any transition not
   preserving the same registry, owner, run, and writer epoch;
3. exact `1110` public/direct entry; malformed, boolean, missing, or extra
   capability fields; a caller-supplied `1111`; a wrong Stage 21 identity
   rejected before deletion or write; and proof that transition occurs once
   only after complete Stage 20 replay;
4. forged, missing, extra, duplicate, wrong, or boolean Stage 20 report,
   flags, manifest, signal, and Stage 21 schema fields;
5. wrong FileRef path/hash, inventory size/role/order/count, unsafe path,
   hardlink, non-regular file, and mixed Stage 19/20 generation;
6. passed with a signal, degraded with null or wrong signal, and unknown
   outcome or publication mode;
7. stored archive plus manifest with synchronously recomputed hashes, including
   a changed generated value or authority disclaimer;
8. preexisting and late temp/formal collision using regular file, symlink,
   hardlink, FIFO, socket, device, or directory;
9. creating-fd identity differing from the fixed temp/formal name identity;
10. Stage 20 current-manifest or source mutation between Pre-admission,
    Snapshot A, source fixpoint, Snapshot B, and second final capture;
11. archive or index mutation after staged replay, formal publication, final
    read, immediate postcondition, or before terminal postcondition;
12. root, run, Stage 20, root-signal-parent, or Stage 21 parent replacement,
    including identity drift before the first invalidation;
13. producer provisional status, tuple, evidence-ref, order, context, or phase
    mutation; public `execute_stage` returning before both postconditions;
    context-to-result circular reference; context copy/replacement between
    postconditions; and invocation of a PRM, HITL, edit, prompt,
    semantic-repair, or repair hook;
14. generic-v1 with stale structured v2 formal and temporary names;
15. caller-supplied context, config, mode, map, timestamp, summary, or artifact
    presence; and
16. raising LLM, provider, prompt, semantic-repair, PRM, HITL, and clock spies,
    all of which must remain at zero calls on structured success.

These tests may exercise only the registry-issued `PreAdmissionContext` and its
single resulting `Stage21AttemptContext` while the repository remains `1110`.
Those Stage 21 tests do not activate production or authorize Stage 22-25
implementation, reconstruction, `release_check`, a release gate, resume, API
use, or fresh F0. Sections 18.12-18.14 separately freeze only the Stage 22-23
design contracts.

### 18.12 B5-D1B-R1 structured Stage 22 export authority

#### 18.12.1 Boundary, dispatch, and two-stage admission

Structured Stage 22 is a deterministic export of one fully replayed current
structured Stage 21 publication and its complete Stage 20/19/17/evidence
closure. It makes exactly zero LLM, provider, semantic-repair, PRM, HITL,
prompt, edit, and clock calls. It does not use `archive.md` prose as paper,
claim, evidence, citation, numeric, or dispatch authority. The sole Stage 21
commit point is the current, strict, fully replayed
`stage-21/bundle_index.json` schema v2.

The exact current code-owned capability map remains `1110`. Ordinary dispatch
therefore retains unchanged `generic-v1` behavior after common canonical
replay. Public or direct structured Stage 22 entry invokes the complete
capability guard and rejects before lock, filesystem, compiler, provider, or
output I/O. Artifact presence, a persisted capability snapshot, caller mode,
configuration, environment, a test flag, or a v2 manifest cannot select the
structured route.

Only a private implementation seam may exercise this design before global
activation. It accepts one registry-issued, non-serializable, non-copyable,
single-use `Stage22PreAdmissionContext`. That context binds only:

- the active `ReleaseGraphLock` owner and writer epoch;
- held root-parent, run, Stage 20, Stage 21, and conditional root-signal parent
  descriptors and their device/inode identities;
- the canonical run path and lexical target stage ID exact `stage22`;
- capability schema version true integer `1` and exact snapshot `1110`;
- the current Stage 20 and Stage 21 manifest identities; and
- the complete Stage 21/20/19/17/canonical-evidence/CFS generation closure.

`Stage22PreAdmissionContext` contains no Stage 22 fd, device, inode,
existence fact, namespace snapshot, output identity, compiler state, or
cleanup permission. Before any `stage-22` stat, enumeration, open, creation,
rollover, cleanup, deletion, compiler workspace creation, or write, code uses
only the held upstream descriptors to capture and fully replay the current
Stage 21 manifest v2 and archive bytes, the current Stage 20 manifest v2 and
its passed/degraded branch, the Stage 19 manifest v2, Stage 17 authority,
canonical experiment evidence, exact CFS-v1 bytes, generation, source paper,
bibliography, project artifacts, template selection inputs, and all recursive
closure bindings. Every file capture records exact bytes, digest, length,
mode, link count, device, inode, and stable before/after metadata. The current
Stage 20 and Stage 21 manifest names must still bind their captured
identities.

Only after this upstream replay succeeds may the registry consume the exact
pre-admission context and, in one same-owner/run/epoch/generation transition,
observe canonical `stage-22` absent, create it, and open it through the held
run fd. It validates and captures the Stage 22 held fd, device, inode, and
parent/name identity, then issues exactly one `Stage22AttemptContext`.
Transition failure consumes the pre-admission context, closes any new fd,
issues no attempt context, performs no output-file deletion or write, and
fails closed. A caller-created, copied, serialized, expired, reused, cross-run,
cross-owner, cross-epoch, cross-stage, or cross-generation context rejects.

The canonical stage directory is a code-owned reserved stage namespace under
the held trusted run fd. Its canonical name must be absent on the initial
non-following descriptor-relative observation. Any preexisting entry,
including a real empty directory, recorded prior-epoch directory, crash
residue, symlink, special file, different-device object, or mount-point
collision rejects without adoption, cleanup, or downstream output write. The
transition calls descriptor-relative `mkdirat`, requires success, and
immediately opens the new name with
`O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`; these are two operations, not an
atomic mkdir-and-return-fd primitive. Before issuing the attempt context or
performing any child write, code requires a real empty directory, supported
same-filesystem/device boundary, expected owner/mode/link semantics, stable fd
identity, expected held-parent binding, and equality between the held identity
and the current non-following parent/name stat. A failed open or validation
may attempt only best-effort removal of the newly created empty reserved
directory through the held parent and then rejects. Rollover or retry may
start a new acquisition only after a separately authorized prior lifecycle
has ended and the canonical name is again observed absent; this transition
never reuses or rediscovers an existing stage. These guarantees are
conditioned on the Section 1.1 trusted-local boundary.

The successful registry phases are exact:

```text
Stage22PreAdmissionContext(pre_admission)
  -> Stage22AttemptContext(namespace_bound)
  -> invalidated
  -> snapshot_a
  -> built
  -> compiler_complete
  -> payloads_published
  -> source_fixpoint
  -> manifest_published
  -> provisional_done
  -> immediate_validated
  -> terminal_validated
  -> cleared
```

Every arrow is code-owned, one-way, and usable once. Any failure from an
attempt phase moves through `failed_cleanup -> cleared`; cleanup errors append
to the original error. Because stage acquisition is absent-only, the
`invalidated` phase means an idempotent absence/emptiness verification of the
newly created namespace; it does not delete a preexisting stage or a
post-acquisition foreign collision. No context can be revived or replaced.

#### 18.12.2 Exact common shapes and Stage 22 namespace

Every Stage 22 `FileRef` has exactly three keys:

```json
{
  "path": "stage-21/bundle_index.json",
  "sha256": "<sha256>",
  "size": 1
}
```

`path` is one safe NFC run-relative path with no empty, `.`, or `..` segment,
no leading slash, backslash, NUL, control character, or platform alias.
`sha256` is exactly 64 lowercase hexadecimal characters over the held exact
bytes. `size` is a true nonnegative integer, never a boolean, and equals both
the held byte length and stable `fstat` size. A missing, extra, duplicate, or
wrong-typed field rejects. Stage 22 does not accept any older two-key
`FileRef`.

An exact CFS binding has exactly two keys and deliberately has no path:

```json
{
  "schema_version": 1,
  "sha256": "<sha256>"
}
```

The version is the true integer `1`, never a boolean. The digest is rebuilt
from held canonical experiment evidence. No standalone CFS file may be
invented.

Every Stage 22 output entry has exactly five keys:

```json
{
  "role": "paper_markdown",
  "logical_name": null,
  "path": "stage-22/paper_final.md",
  "sha256": "<sha256>",
  "size": 1
}
```

The exact key set is `role`, `logical_name`, `path`, `sha256`, and `size`;
there is no `kind` field. `path`, `sha256`, and `size` obey the Stage 22
`FileRef` rules. The eight fixed direct roles and optional `paper_pdf` have
`logical_name=null`. `template_file`, `project_file`, and
`code_support_file` have one nonempty safe canonical logical name derived by
code from their frozen source set. Caller-supplied logical names, aliases,
media types, or output roles reject.

The complete structured Stage 22 namespace contains only:

```text
stage-22/paper_final.md
stage-22/paper_final_latex.md
stage-22/references.bib
stage-22/paper.tex
stage-22/compile_status.json
stage-22/paper_verification.json
stage-22/sanitization_report.json
stage-22/canonical_source.json
stage-22/<template-direct-file>...
stage-22/code/<project-relative-regular-file>...
stage-22/code/README.md
stage-22/code/requirements.txt
stage-22/paper.pdf                         # compiler-success only
stage-22/stage22_export_manifest.json      # always last
```

There is no run-internal `.tmp`, staging, quarantine, backup, random,
rollover, work, cache, compiler, log, auxiliary, or diagnostic entry. There is
no `charts/` branch in this structured schema. Compiler logs and bounded
diagnostics exist only in memory and in the exact `compile_status.json`
payload. A compiler may use an isolated workspace outside the run directory;
no file from that workspace is authoritative unless its exact bytes are
copied through an absent-only formal creating fd and bound by the manifest.

Template direct names are the code-owned, sorted, nonempty style-file set for
the independently rebuilt template selection. They may not equal or be a
prefix of a reserved direct name, `code`, or the manifest name. Project
logical paths are the independently rebuilt canonical project-artifact set.
They are nonempty safe relative paths below `code/`; their implied directory
tree is prefix-closed and contains no empty directory. A path may not be both
a file and a directory prefix. Project logical names may not equal
`README.md` or `requirements.txt`; those names are reserved for the two
code-owned support files. Case-fold, Unicode-normalization, slash, or platform
aliases are rejected before publication.

The output array order and role groups are normative:

```text
1  paper_markdown          logical_name=null  stage-22/paper_final.md
2  paper_latex_markdown    logical_name=null  stage-22/paper_final_latex.md
3  bibliography            logical_name=null  stage-22/references.bib
4  paper_latex             logical_name=null  stage-22/paper.tex
5  compile_status          logical_name=null  stage-22/compile_status.json
6  paper_verification      logical_name=null  stage-22/paper_verification.json
7  sanitization_report     logical_name=null  stage-22/sanitization_report.json
8  canonical_source        logical_name=null  stage-22/canonical_source.json
9  paper_pdf               logical_name=null  optional; compiler-success only
10 template_file           canonical logical name; sorted by logical_name
11 project_file            canonical logical name; sorted by logical_name
12 code_support_file       README.md
13 code_support_file       requirements.txt
```

The first eight roles occur exactly once and in the displayed order.
`paper_pdf` is absent for compiler-failure and otherwise occurs exactly once
immediately after `canonical_source`, never at the end. `template_file`
occurs once per frozen template file and is sorted by canonical
`logical_name`. `project_file` occurs once per canonical project artifact,
is nonempty, and is sorted by canonical `logical_name`. The final two entries
are always `code_support_file` with exact logical names and exact order
`README.md`, then `requirements.txt`. Within each repeated role, canonical
logical names and paths are unique. No other role, logical-name branch,
repetition, order, count, path, or digest is legal.

Each directory below `code/` is opened or created descriptor-relatively,
component by component, with no symlink following. Every directory must be a
real directory with stable identity and link semantics appropriate to the
platform; every leaf must be a regular, single-link file. A symlink,
hardlinked leaf, FIFO, socket, device, static different-device directory,
preexisting observable mount-point collision, special file, unexpected
directory, empty directory, extra leaf, missing leaf, or file/directory prefix
collision fails.
Privileged or same-filesystem mount injection during the attempt is outside
Section 1.1. Cleanup never follows a symlink, recurses through an unvalidated
directory, or modifies a non-reserved path.

#### 18.12.3 Exact compiler contract and deterministic payloads

Structured Stage 22 invokes the code-owned `compile_latex` semantic operation
exactly once. That single invocation receives exact held `paper.tex`,
bibliography, and template bytes and literal `max_attempts=2`. The compiler
adapter may therefore perform at most two internal compiler attempts. It may
not invoke an LLM, provider, fallback template, source rewrite, missing-figure
rewrite, citation repair, numeric repair, cache, or a second semantic
`compile_latex` call. Compiler-produced changes to `paper.tex`, bibliography,
template, code, or any non-PDF payload fail.

`compile_status.json` is canonical authority JSON with exactly:

```json
{
  "schema_version": 2,
  "semantic_calls": 1,
  "max_attempts": 2,
  "attempts": 1,
  "status": "success",
  "tooling_available": true,
  "errors": [],
  "paper_pdf": {
    "path": "stage-22/paper.pdf",
    "sha256": "<sha256>",
    "size": 1
  }
}
```

The exact key set is `schema_version`, `semantic_calls`, `max_attempts`,
`attempts`, `status`, `tooling_available`, `errors`, and `paper_pdf`.
`schema_version` is true integer `2`; `semantic_calls` is true integer `1`;
`max_attempts` is true integer `2`; `attempts` is a true integer in `0..2`.
`errors` is empty on success and contains only code-owned error codes on
failure. The exact legal arrays are `["toolchain_missing"]` when status is
`toolchain_missing`, `["compiler_attempt_1_failed"]` for a one-attempt
`latex_error`, and
`["compiler_attempt_1_failed", "compiler_attempt_2_failed"]` for a
two-attempt `latex_error`. Raw compiler text, paths, secrets, exceptions, and
provider text are never persisted, so no truncation, redaction, normalization,
or platform-dependent diagnostic bytes enter authority.

For success, `status` is exact `success`, `tooling_available` is true,
`attempts` is `1` or `2`, `errors` is empty, `paper_pdf` is the exact formal
PDF `FileRef`, and PDF bytes start with `%PDF-`, contain a terminal `%%EOF`
within the last 1024 bytes, and pass the code-owned structural validator. For
failure, `status` is exact `toolchain_missing` or `latex_error`,
`paper_pdf` is null, `errors` is nonempty, and formal `paper.pdf` is absent.
`toolchain_missing` requires `tooling_available=false` and `attempts=0`;
`latex_error` requires `tooling_available=true` and `attempts` in `1..2`.
No placeholder, stale, empty, touched, or prior PDF is legal.

The other seven fixed direct payloads and every template, project, and
code-support leaf are rebuilt deterministically from Snapshot A under the
frozen Stage 17-21 authority.
`paper_final.md` is the exact structured Stage 19 revised paper plus only the
code-owned Stage 20 degradation notice when the replayed outcome is degraded.
`paper_final_latex.md`, `references.bib`, `paper.tex`,
`paper_verification.json`, `sanitization_report.json`, and
`canonical_source.json` are exact deterministic projections. They cannot
change scientific claims, numbers, citations, source bindings, or generation.
The Stage 21 archive is never copied into them.

#### 18.12.4 Exact Stage 22 manifest schema v2

`stage-22/stage22_export_manifest.json` is the sole structured Stage 22 commit
point. It has true-integer schema version `2` and exactly 23 root keys. This
compiler-success, upstream-passed example is exact:

For persistent/current recognition, `commit point` means the newly captured
canonical v2 manifest and its complete namespace pass strict parsing,
independent expected rebuild, upstream/source/generation closure, and full
semantic replay. A stored manifest or an earlier producer return alone is not
authority. For the live producer, public `DONE` additionally requires the
same live attempt context, held identities, Snapshot/fixpoint, and both
immediate and terminal postconditions. Manifest-last creation is only a
commit candidate until those producer checks pass.

```json
{
  "schema_version": 2,
  "publication_stage_id": "stage22",
  "publication_mode": "structured-scientific-claim-v1",
  "structured_capability_schema_version": 1,
  "structured_capability_snapshot": {
    "stage17_publication": 1,
    "stage19_revision": 1,
    "stage20_replay": 1,
    "stage24_and_release_integration": 0
  },
  "generation_binding_sha256": "<sha256>",
  "canonical_experiment_evidence": {
    "path": "canonical_experiment_evidence.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "cfs": {
    "schema_version": 1,
    "sha256": "<sha256>"
  },
  "selected_result_manifest": {
    "path": "stage-12/selected_result_manifest.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_stage19_manifest": {
    "path": "stage-19/scientific_claim_authority_manifest.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_stage20_manifest": {
    "path": "stage-20/quality_gate_manifest.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_stage21_manifest": {
    "path": "stage-21/bundle_index.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_stage21_archive": {
    "path": "stage-21/archive.md",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_paper": {
    "path": "stage-19/scientific_claim_paper_revised.md",
    "sha256": "<sha256>",
    "size": 1
  },
  "quality_outcome": "passed",
  "degradation_signal": null,
  "bibliography_source": {
    "path": "stage-4/references.bib",
    "sha256": "<sha256>",
    "size": 1
  },
  "template": {
    "name": "example-template",
    "files": [
      {
        "role": "template_file",
        "logical_name": "template.cls",
        "path": "stage-22/template.cls",
        "sha256": "<sha256>",
        "size": 1
      }
    ]
  },
  "project_files": [
    {
      "role": "project_file",
      "logical_name": "main.py",
      "path": "stage-22/code/main.py",
      "sha256": "<sha256>",
      "size": 1
    }
  ],
  "compile": {
    "outcome": "compiler-success",
    "semantic_calls": 1,
    "max_attempts": 2,
    "attempts": 1,
    "status": "success",
    "tooling_available": true,
    "errors": [],
    "status_file": {
      "path": "stage-22/compile_status.json",
      "sha256": "<sha256>",
      "size": 1
    },
    "paper_pdf": {
      "path": "stage-22/paper.pdf",
      "sha256": "<sha256>",
      "size": 1
    }
  },
  "output_count": 13,
  "outputs": [
    {
      "role": "paper_markdown",
      "logical_name": null,
      "path": "stage-22/paper_final.md",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "paper_latex_markdown",
      "logical_name": null,
      "path": "stage-22/paper_final_latex.md",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "bibliography",
      "logical_name": null,
      "path": "stage-22/references.bib",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "paper_latex",
      "logical_name": null,
      "path": "stage-22/paper.tex",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "compile_status",
      "logical_name": null,
      "path": "stage-22/compile_status.json",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "paper_verification",
      "logical_name": null,
      "path": "stage-22/paper_verification.json",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "sanitization_report",
      "logical_name": null,
      "path": "stage-22/sanitization_report.json",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "canonical_source",
      "logical_name": null,
      "path": "stage-22/canonical_source.json",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "paper_pdf",
      "logical_name": null,
      "path": "stage-22/paper.pdf",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "template_file",
      "logical_name": "template.cls",
      "path": "stage-22/template.cls",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "project_file",
      "logical_name": "main.py",
      "path": "stage-22/code/main.py",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "code_support_file",
      "logical_name": "README.md",
      "path": "stage-22/code/README.md",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "code_support_file",
      "logical_name": "requirements.txt",
      "path": "stage-22/code/requirements.txt",
      "sha256": "<sha256>",
      "size": 1
    }
  ],
  "generated": "2026-07-28T00:00:00+00:00"
}
```

The exact root key set is:

```text
schema_version
publication_stage_id
publication_mode
structured_capability_schema_version
structured_capability_snapshot
generation_binding_sha256
canonical_experiment_evidence
cfs
selected_result_manifest
source_stage19_manifest
source_stage20_manifest
source_stage21_manifest
source_stage21_archive
source_paper
quality_outcome
degradation_signal
bibliography_source
template
project_files
compile
output_count
outputs
generated
```

Duplicate, unknown, or missing root or nested keys reject.
`publication_stage_id` is exact `stage22`; `publication_mode` is exact
`structured-scientific-claim-v1`. The capability schema is true integer `1`;
the snapshot has exactly the four code-owned keys and exact true-integer
`1110` for this implementation slice. It records private admission state and
does not activate public entry.

`generation_binding_sha256`, `canonical_experiment_evidence`, `cfs`,
`selected_result_manifest`, `source_stage19_manifest`,
`source_stage20_manifest`, `source_stage21_manifest`,
`source_stage21_archive`, `source_paper`, `quality_outcome`,
`degradation_signal`, `bibliography_source`, and `generated` are independently
rebuilt from the held Stage 21/20/19/17/evidence closure. Every named file is
an exact three-key Stage 22 `FileRef`. `degradation_signal` is null for
`passed` and the exact root `degradation_signal.json` `FileRef` for
`degraded`; crossed branches reject. `generated` is copied exactly from the
replayed Stage 21 manifest and causes zero clock calls.

`template` has exactly `name` and `files`. `name` is the independently
selected canonical template name. `files` contains exactly the
`template_file` output-entry subsequence, including canonical logical names,
in the same sorted order and with byte-for-byte identical path/hash/size
bindings. `project_files` is exactly the nonempty `project_file`
output-entry subsequence in the same canonical logical-name order. Neither
nested field accepts a different output-entry shape or caller-supplied name.

`compile` has exactly `outcome`, `semantic_calls`, `max_attempts`, `attempts`,
`status`, `tooling_available`, `errors`, `status_file`, and `paper_pdf`.
`status_file` is the exact three-key `FileRef` for
`stage-22/compile_status.json`. `paper_pdf` is the exact three-key PDF
`FileRef` for compiler-success and null for compiler-failure. The other values
cross-match the exact compile-status payload and Section 18.12.3.
`outcome` is exact `compiler-success` iff `status=success`; otherwise it is
exact `compiler-failure`.

`output_count` is a true positive integer equal to `len(outputs)` and exactly
`10 + template_file_count + project_file_count + pdf_present`: eight fixed
direct rows, two fixed support rows, all template/project rows, and one PDF
row iff compiler-success. `outputs` has the exact five-key shape,
logical-name branches, role multiplicity, paths, and normative order in
Section 18.12.2. Every output is a regular, single-link held file whose bytes,
digest, size, identity, and independent deterministic rebuild match. The
manifest is not an output.

The compiler-failure manifest uses the same exact 23 root keys, sets
`compile.outcome` to `compiler-failure`, sets `compile.paper_pdf` to null,
omits the optional `paper_pdf` output row, decrements `output_count` by one,
and binds an exact failure `compile_status.json`. No alternate root union
exists.

Authority JSON follows Section 3. Displayed object-key order is explanatory;
array order is normative. The manifest contains no field for its own path,
digest, size, identity, or inventory row. Its exact bytes are captured and
hashed externally by Stage 23. Therefore no edge originates at the manifest
and points back to itself: source manifests -> payloads -> Stage 22 manifest
is a directed acyclic binding graph, not a self-hash cycle.

#### 18.12.5 Exact result tuples and outcome matrix

Structured Stage 22 deliberately exposes coarse stable result tuples even
though its manifest binds the complete direct and nested namespace:

```text
artifacts = (
  "paper_final.md",
  "code/",
  "stage22_export_manifest.json",
)

evidence_refs = (
  "stage-22/paper_final.md",
  "stage-22/code/",
  "stage-22/stage22_export_manifest.json",
)
```

The tuples are exact three-tuples in this order for all four publishable
branches. The trailing slash on `code/` and `stage-22/code/` is normative and
denotes the complete manifest-bound code directory tree, not a regular file,
glob, or caller-normalized alias. `paper.tex`, `paper.pdf`, template,
bibliography, reports, and status remain manifest-bound but are not added to
the coarse tuple. Any failure returns `status=FAILED`,
`decision="retry"`, `artifacts=()`, and `evidence_refs=()`.

| Upstream quality | Compiler | Stage 22 result | PDF branch | Release meaning |
|---|---|---|---|---|
| `passed` | success | `DONE`, exact three-tuples | required exact PDF and `compile.outcome=compiler-success` | no release verdict in B5-D1B-R1 |
| `degraded` | success | `DONE`, exact three-tuples | required exact PDF and `compile.outcome=compiler-success` | preserves degraded upstream state; no release verdict |
| `passed` | failure | `DONE`, exact three-tuples | PDF forbidden; exact `compile.outcome=compiler-failure` | publishable no-PDF export state; release decision deferred |
| `degraded` | failure | `DONE`, exact three-tuples | PDF forbidden; exact `compile.outcome=compiler-failure` | degraded no-PDF state; release decision deferred |
| invalid/unreplayable/withdrawn upstream | not called | `status=FAILED`, `decision="retry"`, empty tuples | no publication | fail closed |
| deterministic build, compiler-contract, collision, publication, replay, fixpoint, or postcondition failure | irrelevant | `status=FAILED`, `decision="retry"`, empty tuples | manifest-first cleanup | fail closed |

A compiler failure is an honest publishable Stage 22 operational state, not a
release success, PDF substitute, or permission to package Markdown. Whether
either compiler branch is release-eligible is owned only by B5-D2 Stage 24/25,
independent reconstruction, and release-gate adjudication.

#### 18.12.6 Held-fd publication and executor lifecycle

After the two-stage admission in Section 18.12.1:

1. **Manifest-first absence verification.** Through the newly acquired held
   Stage 22 fd, verify the fixed manifest name absent first, then independently
   verify every fixed direct formal and expected template/code root absent and
   the directory still empty. Any entry means post-acquisition collision or
   identity drift and rejects without adoption or deletion. This phase is the
   idempotent `invalidated` state transition; it performs no stale-stage
   cleanup.
2. **Snapshot A.** Recapture and completely replay upstream through held
   descriptors. Require exact equality with pre-admission bytes, hashes,
   sizes, modes, link counts, device/inode identities, generation, outcome,
   current Stage 20/21 manifest-name identities, and namespace closure.
3. **Build in memory.** Deterministically build all non-compiler payload bytes,
   expected code/template paths, manifest inputs, and expected result tuples
   with zero LLM/provider/repair/HITL/clock calls.
4. **Compiler call.** Invoke `compile_latex` exactly once with
   `max_attempts=2` in an isolated workspace outside the run. Capture its
   bounded result in memory. Revalidate that every input byte is unchanged.
5. **Absent-only directories.** Require `code/` and every required nested
   directory component physically absent after namespace verification. For each safe
   single component, require a non-following absent observation through the
   held trusted parent fd, call descriptor-relative `mkdirat` and require
   success, then immediately call
   `openat(O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC)`. Before any child write,
   require a real directory, strict initial emptiness, supported
   same-filesystem/device boundary, expected owner/mode/link semantics, stable
   held-fd identity, and equality between the held identity and the current
   non-following parent/name stat. Retain that fd for all child operations.
   This is trusted-local create-then-acquire, not an atomic mkdir-and-fd
   primitive or historical creator proof. `EEXIST` and every preexisting
   component, including an expected real empty directory, are collisions;
   never adopt or reopen them. A failed open/validation attempts only
   best-effort reserved-name cleanup through the held parent and then fails
   publication.
6. **Absent-only payloads.** In normative output-array order, require each
   formal name absent, create it descriptor-relatively with
   `O_CREAT|O_EXCL|O_NOFOLLOW`, require a regular single-link file, and retain
   its creating fd. Write, flush, sync, read back, hash, and validate only
   through that fd. Never rename, replace, link, reopen for writing, or publish
   via a run-internal temporary.
7. **Nested-tree replay.** From held directory and leaf fds, require the exact
   prefix tree, no extra or special entries, exact code bytes, and exact
   template/code output entries. Directory enumeration is validation only,
   never authority.
8. **Source fixpoint.** Recapture the complete upstream closure and require
   exact Snapshot A equality plus current Stage 20/21 manifest-name identity.
9. **Manifest last.** Only after all payloads and source fixpoint pass, require
   `stage22_export_manifest.json` absent and create it with the same
   exclusive creating-fd protocol. Write exact v2 bytes through that fd. It is
   only a commit candidate.
10. **Final replay and Snapshot B.** Duplicate-safely parse and independently
    rebuild the manifest, payloads, compiler branch, code tree, namespace,
    tuples, and all upstream bindings. Snapshot B's upstream/source portion
    must exactly equal Snapshot A. Snapshot B additionally captures the
    complete formal outputs, manifest, namespace, and held parent/leaf
    identities. A second final capture must exactly equal that complete
    Snapshot B, including current upstream manifest-name identities.
11. **Provisional producer handoff.** The producer may advance only the same
    live attempt context to `provisional_done` and hand the executor an
    internal provisional `DONE`, exact three-tuples, and that context. It
    cannot return a public `StageResult`.
12. **Immediate postcondition.** Before any hook or public return, verify
    status, decision, exact tuple count/order, complete disk namespace,
    manifest-last replay, Snapshot A/B, source fixpoint, current upstream
    authority, writer epoch, held identities, and live context.
13. **Terminal postcondition.** Structured Stage 22 skips PRM, HITL, edit,
    prompt, semantic-repair, and repair hooks. Immediately before public
    return, repeat capture/replay and require unchanged provisional result,
    context, tuples, bytes, identities, upstream current manifests, and
    compiler call counters.
14. **Public result or withdrawal.** Only both postconditions permit public
    `DONE` with the exact three-tuples. A non-`DONE` conversion, tuple drift,
    late mutation, upstream withdrawal, identity drift, or postcondition
    failure triggers manifest-first aggregate cleanup and returns exact
    `status=FAILED`, `decision="retry"`, and empty tuples.

After acquisition, all child creation uses the held directory fd. If a
deterministic phase-boundary fault or cooperating-lifecycle bug changes the
canonical name, no operation reopens or rediscovers the replacement. Every
documented capture boundary rechecks canonical name-to-fd identity; once
drift is detected, payload and manifest writes stop and only cleanup through
the held original may continue. If drift is first observed by final replay,
all earlier writes necessarily targeted only the held original, the
replacement remains external-zero, and authority is withdrawn. Cleanup never
follows or recursively traverses an unvalidated replacement. These are
held-fd invariants inside Section 1.1, not an absolute claim against excluded
syscall races.

Cleanup uses only the held original parent and directory fds. A collision that
cannot safely be classified and unlinked remains an error; cleanup does not
follow, overwrite, truncate, or recursively traverse it. A fully validated
expected nested code tree may be cleaned bottom-up through held fds; an
unknown directory is never recursively cleaned. Identity-check-then-unlink is
not an atomic OS unlink-if-inode guarantee. The original failure remains
primary and cleanup errors append. If canonical name drift makes the held
directory's outer inode unreachable by name, POSIX cleanup may leave detached
non-authoritative residue; it never rediscovers or acts through the
replacement merely to remove that residue.

### 18.13 B5-D1B-R1 structured Stage 23 citation-verification authority

#### 18.13.1 Authority boundary, admission, and cited closure

Structured Stage 23 verifies only citation existence/identity metadata and
paper-level relevance for the exact current structured Stage 22 paper. Its
report is citation metadata and relevance evidence only. It is not scientific
claim authority, numeric authority, experimental evidence, claim-support
authority, a replacement for the Stage 17 citation plan/allowlist, or release
authority. It cannot add, delete, rewrite, weaken, or strengthen a paper
sentence or citation marker. `paper_final_verified.md` is byte-identical to
the held `stage-22/paper_final.md`.

Admission uses the same two-stage pattern. A registry-issued,
upstream-only `Stage23PreAdmissionContext` binds the active owner/epoch, held
root/run/Stage 20/21/22 descriptors, exact current manifest identities,
generation, CFS, capability `1110`, and lexical target `stage23`, but no Stage
23 identity or existence fact. Before any Stage 23 I/O, code fully replays
Stage 22 manifest v2, its exact namespace and compiler branch, Stage 21 v2,
current Stage 20 v2, Stage 19/17, canonical evidence, CFS, source paper,
bibliography, and citation closure. Pre-admission itself requires the exact
unique sorted cited-key count `1..32`; an out-of-range or malformed closure
therefore fails with zero Stage 23 path I/O. Only then may one code-owned
transition observe canonical `stage-23` absent, create it, and open the newly
created directory through the held run fd before issuing one
`Stage23AttemptContext`.

That transition uses the same trusted stage-namespace acquisition contract as
Stage 22: initial non-following absence through the held parent,
descriptor-relative `mkdirat`, immediate
`openat(O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC)`, real-directory and strict
emptiness checks, supported filesystem, expected owner/mode/link and held
parent binding, stable fd identity, and current non-following name-to-fd
identity before child write. Every preexisting entry, including a known old
stage or crash residue, rejects without adoption or cleanup by this
transition. Stored state, caller claims, process restart, matching disk bytes,
or a prior registry record cannot waive absence. It makes no claim against
excluded same-UID or privileged-mount races.

The final cited-key closure is extracted independently from both held
`stage-22/paper_final.md` and `stage-22/paper.tex`. The two sets must be
identical. It is then required to be unique, strict bytewise sorted, fully
present exactly once in the held bibliography, and have exact count `1..32`.
Zero citations, 33 or more citations, a duplicate bibliography key, missing
key, unplanned key, unknown key, Markdown/LaTeX mismatch, or changed paper
fails before provider and Stage 23 output I/O. No full-bibliography
verification and no same-name shadow bibliography are permitted.

At current `1110`, ordinary dispatch remains unchanged `generic-v1`; public or
direct structured Stage 23 mechanically rejects before lock, filesystem,
provider, or output I/O. Only the private registry-issued seam is eligible for
implementation tests. Stale v2 files, config, caller state, cache state, or
artifact presence cannot select it.

#### 18.13.2 Exact provider, semantic, and outbound contract

For each cited key, code selects exactly one deterministic provider route from
held bibliography metadata:

```text
normalized nonempty DOI present  -> crossref-doi
else normalized arXiv ID present -> arxiv-id
else nonempty normalized title   -> openalex-title
```

The DOI, arXiv-ID, and title normalization and exact request bytes are
code-owned and independently rebuilt. Precedence is mandatory: a DOI always
routes only to Crossref; without DOI, an arXiv ID routes only to arXiv; only
the absence of both permits a title route to OpenAlex. A citation has exactly
one semantic metadata-verification operation and at most one outbound
request. There is no fallback, retry, alternate provider/endpoint, cache read,
cache write, stale result, batched substitution, hidden SDK retry, redirect
to another provider, or semantic repair. A connection error, timeout, DNS/TLS
error, 429, 5xx, truncated body, invalid JSON, duplicate key, extra/missing
consumed-projection field, wrong key/DOI/arXiv-ID/title, empty result,
ambiguous result, or metadata mismatch yields a non-`verified` existence
result. Bounded unconsumed provider-envelope fields are ignored under Section
18.13.2.2 rather than copied into authority. By the fixed
outcome matrix, any non-`verified` existence result is `FAILED` for every
claim scope. Receiving no response is not permission to try again.

Citation routes run in cited-key order. Provider spies and the terminal
postcondition require:

```text
metadata semantic calls per citation = 1
metadata outbound requests per citation <= 1
metadata semantic calls total = cited_count
metadata outbound requests total <= cited_count <= 32
metadata fallback calls = 0
metadata retry calls = 0
metadata cache reads/writes = 0
```

Only after all citation metadata results are exact `verified` does Stage 23
make at most one semantic paper-level relevance call over the ordered verified
metadata and exact paper identity. That semantic call has at most two outbound
attempts. A second outbound is legal only when the first failed with a pure
transport error before any response status, header, or body byte was received.
It must use the identical provider, model, endpoint, prompt bytes, request
bytes, timeout policy, and request fingerprint. HTTP 429/5xx, redirects,
partial content, invalid content, decoding/schema/key-closure error, tool
error after response content, or semantic rejection is not pure transport and
cannot retry. There is no fallback, cache, repair, second semantic call, or
third outbound.

The relevance semantic-call count is `0` only when the structured relevance
handle is exact `llm is None` before semantic request construction, producing
the code-owned `missing` branch; otherwise it is exactly `1`, always within
the frozen maximum.
Its outbound count is `0` for `missing`, `1` on first-attempt response or
failure, `2` on an identical pure-transport retry, or `1..2` when all legal
attempts end in pure transport failure. Complete output has one finite
canonical decimal string in `[0,1]` for each cited key and exact key/order
closure. Malformed, duplicate, extra, missing, nonfinite, out-of-range, or
wrong-ordered semantic content is relevance `failed` without retry. Exhausted
pure transport is relevance `unavailable`. These adverse branches are routed
strictly by `claim_scope`: `research_release` fails;
`pipeline_validation` and `exploratory` degrade.

The aggregate hard bounds are:

```text
provider semantic calls <= 32
provider outbound requests <= 32
relevance semantic calls <= 1
relevance outbound requests <= 2
all outbound requests <= 34
fallback/cache/repair calls = 0
```

Any wrapper, SDK, redirect, or background task counts toward these bounds.
An attempt to exceed one of them fails even if returned bytes would otherwise
verify.

##### 18.13.2.1 Canonical HTTP transcripts and bounded transport

Stage 23 owns a private HTTP/1.1 transport. It does not reuse
`ClaimVerifier`, a generic citation verifier, `LLMClient.chat()`, an SDK,
environment proxy discovery, `netrc`, a cookie jar, cache, fallback chain,
HTTP/2 negotiation, redirect handler, or hidden retry. TLS uses hostname
verification, the system trust store, and TLS 1.2 or newer. Metadata and
relevance transports have an exact 5-second connect timeout, 10-second read
inactivity timeout, and 15-second monotonic total deadline. A transport may
read a monotonic clock solely to enforce those limits; Stage 23 makes no
authority wall-clock call and persists no transport time.

Every actual request head is at most 16,384 bytes. Every response head is at
most `MAX_RESPONSE_HEAD_BYTES = 16384` bytes and each response header line is
at most 8,192 bytes. The first byte beyond either applicable limit is captured
before rejection: total-head overflow therefore captures exactly 16,385
bytes; line overflow includes its first over-limit byte without exceeding the
16,385-byte total overflow capture. These limits and capture boundaries are
byte counts independent of socket read chunking.

`request_sha256` is lowercase SHA-256 over this canonical non-secret request
transcript, with literal LF bytes and no terminal bytes beyond the entity:

```text
METHOD LF
HTTPS_ORIGIN LF
REQUEST_TARGET LF
Header-Name: value LF
Header-Name: value LF
LF
ENTITY_BODY
```

The header lines are the route-specific ordered non-secret headers frozen
below. The transcript binds method, parsed HTTPS origin, complete path/query
request target, header spelling/order/value, and exact entity bytes.
Authorization values are excluded. A change to origin, path, query, header,
or body changes the digest, except that the held Authorization value is
cross-checked only by private telemetry as explicitly frozen. The transcript
contains neither the report nor a manifest and therefore creates no self-hash
edge.

All outbound requests use `Accept-Encoding: identity`; a response
`Content-Encoding` is legal only when absent or exact case-insensitive
`identity`. No decompression occurs. Transfer-Encoding is either absent or
one exact case-insensitive `chunked`; a simultaneous Content-Length, multiple
transfer codings, chunk extension, or nonempty trailer rejects. Dechunked
entity bytes, not chunk-size lines or chunk boundaries, enter the response
transcript. A Content-Length must be one canonical nonnegative decimal, must
not be duplicated, and must equal the complete entity size.

`response_sha256` is lowercase SHA-256 of this canonical bounded response
transcript:

```text
STATUS_CODE LF
Content-Type: <value-or-absent> LF
Content-Encoding: <value-or-absent> LF
Content-Length: <value-or-absent> LF
Transfer-Encoding: <value-or-absent> LF
LF
CAPTURED_ENTITY_BYTES
```

Header names have the spelling and order above. A completely parsed selected
header has surrounding optional whitespace removed; an absent selected
header uses exact `<absent>`. Duplicate, conflicting, malformed, overlong, or
incompletely parsed response heads do not use first-value, last-value, or
comma-join behavior. They use exactly:

```text
STATUS_OR_000 LF
invalid-head LF
raw-head-prefix-sha256:<sha256> LF
raw-head-prefix-size:<decimal> LF
head-overflow:<true|false> LF
selected-fields:unavailable LF
LF
```

`STATUS_OR_000` is the three-digit status only if it was completely and
strictly parsed; otherwise it is `000`. The raw-head prefix begins at the
first response byte. A normal complete head is captured through and including
its terminating empty line. An early parser failure hashes every response-head
byte actually received through the bytewise abort boundary. Head overflow
hashes exactly the 16,385-byte prefix. Once an invalid head is recognized,
Stage 23 reads no entity body. Raw head bytes and the transcript are held only
in bounded memory and are never persisted.

For a valid head, a normal response transcript contains the complete bounded
entity. A truncated response contains every bounded entity byte actually
received. Entity overflow reads and captures exactly
`MAX_RESPONSE_BYTES + 1` bytes, then stops without reading an unbounded
remainder. Thus chunking never changes a digest. Before any status, header, or
body byte, pure transport has `response_sha256 = null`. Once any response
material is observed, `response_sha256` is non-null, including status-only,
invalid-head, empty-entity, truncation, and overflow failures. Raw response
heads, bodies, and transcripts are discarded after the strict in-memory
projection and digest cross-check.

##### 18.13.2.2 Exact metadata provider wires

All three routes issue exactly one `GET`, have an empty entity, accept only
HTTP status `200`, use the common timeouts and response-head limits above, and
send these on-wire headers in exact order:

```text
Host: <origin authority>
User-Agent: AutoResearchClaw-Stage23/1
Accept: <route media type>
Accept-Encoding: identity
Connection: close
```

There is no explicit default port in the fixed metadata origins. Query and
path values are strict UTF-8 RFC 3986 encoding: only ASCII letters, digits,
`-`, `.`, `_`, and `~` remain literal; every other byte uses uppercase `%HH`.
Spaces are `%20`, never `+`. Parameter names, parameter order, separators, and
empty/nonempty values are exact.

| Route | Exact origin and request target | Accept and accepted response media type | Entity cap |
|---|---|---|---:|
| `crossref-doi` | `https://api.crossref.org` plus `/works/{doi-segment}`; normalized DOI is one encoded segment, so `/` is `%2F`; no query | request `application/json`; response media type exact `application/json` or `application/vnd.crossref-api-message+json`, charset absent or UTF-8 | 1,048,576 |
| `arxiv-id` | `https://export.arxiv.org` plus `/api/query?id_list={arxiv-id}&start=0&max_results=2` | request `application/atom+xml`; response media type exact `application/atom+xml` or `application/xml`, charset absent or UTF-8 | 1,048,576 |
| `openalex-title` | `https://api.openalex.org` plus `/works?search={title-match-key}&select=id%2Cdoi%2Ctitle%2Cpublication_year%2Cids&per-page=200` | request `application/json`; response media type exact `application/json`, charset absent or UTF-8 | 2,097,152 |

Media-type tokens and the optional charset name are compared
case-insensitively after strict parsing; no other parameter is accepted.
A redirect, 206, 3xx, 4xx including 429, 5xx, wrong content type, illegal
encoding, header failure, truncation, overflow, malformed payload, ambiguity,
or transport failure is non-`verified`. Metadata never retries and never
falls back.

Private in-memory metadata telemetry uses this exact exhaustive
classification before all classes collapse to non-`verified`:
`provider_dns`, `provider_connection`, `provider_tls`, `provider_timeout`,
`provider_http_status`, `provider_redirect`, `provider_header_invalid`,
`provider_content_type`, `provider_content_encoding`, `provider_truncated`,
`provider_oversized`, `provider_malformed`, `provider_identity_mismatch`, and
`provider_ambiguous`. Status 429 and 5xx are `provider_http_status`; a redirect
is `provider_redirect`; duplicate, missing, or wrong-type consumed fields and
JSON/XML/date/title rejection are `provider_malformed`. These are bounded
diagnostics, not report fields, and contain no raw bytes, query value, title,
identifier, or credential.

JSON is strict UTF-8 without BOM. Duplicate object keys at any depth, NaN,
Infinity, a depth over 16, more than 4,096 aggregate arrays/objects, or a
string over 16,384 UTF-8 bytes rejects. A JSON boolean is never an integer.
Provider envelope fields outside the consumed projection may be ignored only
after the entire envelope has passed these bounds and duplicate checks; they
never enter authority.

The arXiv parser is namespace-aware strict XML: UTF-8 without BOM, depth at
most 16, at most 4,096 elements, each text node at most 16,384 UTF-8 bytes,
and aggregate text at most 1,048,576 UTF-8 bytes. DTDs, entity declarations or
expansion, XInclude, external resources, and network reads are forbidden.
Element expanded-name namespace URIs form this exact closed set:

```text
http://www.w3.org/2005/Atom
http://a9.com/-/spec/opensearch/1.1/
http://arxiv.org/schemas/atom
http://www.w3.org/XML/1998/namespace
```

The URI, not a prefix spelling, is compared. An element with no namespace, a
fifth namespace, or namespace rebinding rejects. Ordinary unnamespaced
attributes are never authority inputs. Only Atom `feed/entry/id`, `title`,
and `published` are consumed. OpenSearch and arXiv extension elements are
bounded-parsed and then ignored; they cannot affect candidate selection,
identity, title, year, or outcome.

##### 18.13.2.3 Identity normalization and matching

Normalization exists only for provider identity matching. It cannot change
the held paper, bibliography, citation plan, scientific claim registry,
rendered claim sentence, or any scientific-authority text.

**DOI.** The input is one Unicode scalar string. Code trims Unicode White_Space,
applies NFKC once, and removes at most one case-insensitive prefix from exact
`doi:`, `https://doi.org/`, or `https://dx.doi.org/`. The remainder must be
ASCII, is ASCII-lowercased, has 7..255 bytes, contains no whitespace, percent
sign, control, or DEL, and must fully match:

```text
10\.[0-9]{4,9}/[-._;()/:a-z0-9]+
```

The Crossref root must be a bounded JSON object with exact provider status
`ok`, message type `work`, and one message object. Its DOI is normalized by
the same algorithm and must equal the requested normalized DOI exactly.
Title-only similarity can never substitute for a DOI match. The message title
must be an array containing exactly one nonempty string whose title match key
equals the held bibliography title key. Year uses the first present field in
the exact precedence `published-print`, `published-online`, `issued`; the
selected field must contain exactly one usable first date-parts year.

**arXiv request identity.** Held bibliography input is one scalar string. Code
trims Unicode White_Space, applies NFKC once, lowercases ASCII, and removes at
most one case-insensitive exact prefix: `arxiv:`,
`https://arxiv.org/abs/`, or `https://arxiv.org/pdf/`. Only the PDF URL form
may then remove one terminal `.pdf`. The result is ASCII, at most 64 bytes,
and fully matches one of:

```text
[0-9]{4}\.[0-9]{4,5}(v[1-9][0-9]*)?
[a-z][a-z0-9-]*(\.[a-z]{2})?/[0-9]{7}(v[1-9][0-9]*)?
```

Code splits that normalized request into `base_id` and optional `version`.
An explicit version is preserved exactly. A versionless request denotes
paper-level arXiv identity and is not rewritten to a caller-selected or
guessed version. The normalized request string, including whether it carried
a version, determines the fixed `id_list` request target and is bound by the
held bibliography, canonical request transcript, and `request_sha256`.

**arXiv response identity.** The Atom `entry/id` string is parsed only as an
identifier and is never dereferenced. It must be ASCII and have exactly one
of these case-sensitive prefixes:

```text
http://arxiv.org/abs/<identifier>
https://arxiv.org/abs/<identifier>
```

The authority is exact lowercase `arxiv.org`, with no userinfo, port, query,
or fragment. The path is exact `/abs/<identifier>` with one nonempty final
identifier, no percent encoding, backslash, double slash, or dot segment.
The extracted complete versioned identifier is ASCII, at most 64 bytes, must
match the same new/legacy base grammar above, and must always end in a legal
`v[1-9][0-9]*`. HTTP and HTTPS response prefixes normalize to the same
`(base_id, version)` identity; provider outbound remains HTTPS-only.

The bounded feed must have exactly one Atom entry and no error entry. Matching
is exhaustive:

| Request | Response | Result |
|---|---|---|
| `base_id vN` | same `base_id vN` | accept |
| `base_id vN` | same base with another version, or no version | reject |
| versionless `base_id` | same base with any legal `vN` | accept |
| versionless `base_id` | same base without a version | reject |
| any request | different `base_id` | reject |

Title key must also equal the held bibliography title key and Atom
`published` must supply a legal four-digit year. Title, year, LLM output,
provider rank, or caller state can never replace base/version matching.
Successful `metadata.arxiv_id` stores the response's complete normalized
versioned identifier, including `vN`; it does not store a versionless request
form.

**Title.** Input must be valid scalar UTF-8 of at most 4,096 bytes. Code applies
NFKC; maps every Unicode White_Space character and every Unicode General
Category `P*` punctuation character to ASCII space; collapses consecutive
spaces; trims; and applies full Unicode casefold. It performs no fuzzy,
token-similarity, deaccenting, stemming, year-based, LLM, or caller-selected
match. The resulting match key must be nonempty, at most 1,024 Unicode scalar
values, and at most 4,096 UTF-8 bytes. Provider titles use the identical
algorithm.

The stored projected title is not the casefolded match key. It is the provider
title after NFKC, Unicode White_Space collapse to ASCII space, trim, scalar
validation, and the same 1,024-scalar/4,096-byte limits, but before punctuation
mapping and casefold. A control character, DEL, empty result, or over-limit
display title rejects.

For OpenAlex, `meta.count` must be a true integer `0..200` and must equal the
bounded `results` array length, proving that the one response contains the
reported candidate set. Exactly one result title must equal the request title
match key. Zero matches, two matches, `meta.count > 200`, a count/length
mismatch, or any inability to prove unique mechanical identity is ambiguous
and non-`verified`. DOI, arXiv ID, year, an LLM, rank, score, caller flag, or
persisted choice cannot break a title tie.

For every route, year is a true integer, never a boolean, in `1000..2999`.
Consumed required values that are missing, duplicated, wrong-type, malformed,
or ambiguous reject. The code-owned bounded authority projection is the
existing citation entry with exact outer keys `cite_key`, `route_class`,
`endpoint_class`, `request_sha256`, `response_sha256`, `outbound_count`,
`status`, and `metadata`, plus exact nested metadata keys `title`, `doi`,
`arxiv_id`, `year`, and `source`. This combined projection binds title, DOI,
arXiv ID, year, source, route/endpoint class, and both wire digests without a
report-root or manifest change. Crossref projects the normalized DOI and null
arXiv ID; arXiv projects the normalized arXiv ID and null DOI; OpenAlex
projects both identifiers as null.

Raw responses exist only in bounded memory. The projection and digests are
built from the same captured bytes, then the raw request/response is
discarded. A stored projection and synchronized stored hashes cannot replace
strict parsing of the current epoch's held transport capture. Prompt,
reasoning, secret, credential, complete provider envelope, and unused
provider fields are never persisted.

##### 18.13.2.4 Private relevance request and response wire

Relevance uses a new private `Stage23RelevanceTransport`, never
`LLMClient.chat()` or a generic verifier. A registry-issued held transport
spec is built only from the fully replayed canonical configuration. It binds
exact provider ID, primary model, raw base URL, parsed HTTPS origin, endpoint
path, wire-policy ID, timeout/cap policy, and one held credential handle.
Fallback models/URLs, extra caller headers, proxy bridges, alternate wire
APIs, and provider SDKs are forbidden.

The exact structured Stage 23 relevance provider-admission map is:

| Canonical provider ID | Exact Stage 23 wire-policy ID | Admission |
|---|---|---|
| `openai` | `stage23-openai-chat-completions-bearer-v1` | allowed |
| `openrouter` | `stage23-openai-chat-completions-bearer-v1` | allowed |
| `deepseek` | `stage23-openai-chat-completions-bearer-v1` | allowed |
| `novita` | `stage23-openai-chat-completions-bearer-v1` | allowed |
| `minimax` | `stage23-openai-chat-completions-bearer-v1` | allowed |
| `openai-compatible` | `stage23-openai-chat-completions-bearer-v1` | allowed |
| `anthropic` | none | structured system/configuration failure |
| `kimi-anthropic` | none | structured system/configuration failure |
| `ollama` | none | structured system/configuration failure |
| `acp` | none | structured system/configuration failure |
| `claude-cli` | none | structured system/configuration failure |
| `codex-cli` | none | structured system/configuration failure |
| any unknown provider | none | structured system/configuration failure |

This closed map applies only to private structured Stage 23. It does not
remove, add, reinterpret, or otherwise change any generic-v1 provider
capability. Exact `wire_api=chat_completions` is mandatory for every allowed
provider. Exact `wire_api=responses` and every other non-`chat_completions`
wire are structured system/configuration failures; no spelling
normalization, compatibility alias, adapter, or alternate wire may admit
them.

Provider admission is rebuilt only from these fields of the fully replayed
canonical configuration: `provider`, `base_url`, `wire_api`,
`primary_model`, `fallback_models`, and the MetaClaw bridge state. The
admitted values additionally require:

- exact `wire_api=chat_completions`;
- `fallback_models` is the exact empty tuple;
- the MetaClaw bridge is disabled;
- canonical `base_url` is nonempty and is used exactly as stored, without a
  provider preset, default, fallback, repair, or inferred value;
- `primary_model` is 1..256 bytes of visible ASCII `0x21..0x7e`; and
- there are no extra headers, fallback URL, or proxy bridge.

`llm is None` remains the sole `missing` admission and is classified during
pre-admission before any Stage 23 pathname or provider I/O. When `llm` is
non-null, it must be the active `LLMClient` created and registered for the
current pipeline run by the code-owned factory. The active registration binds
the same run, writer owner, writer epoch, canonical provider, canonical client
configuration, and live client identity. A fake, copy, caller-created,
unregistered, wrong-run, wrong-owner, wrong-epoch, inactive, or wrong-type
client is a structured system/configuration failure. The active client must
have no Anthropic adapter, fallback chain, fallback URL, extra header,
MetaClaw bridge, proxy bridge, or alternate transport.

For a non-null admitted client, its held `base_url`, `primary_model`, and
`wire_api` must exactly equal the fully replayed canonical values before any
endpoint parsing or request construction. Stage 23 reads the credential
exactly once from that active client's held memory while constructing the
registry-issued transport spec. It never rereads an environment variable,
canonical-config secret field, caller secret, provider preset, or fallback
credential. The resulting credential handle and exact bytes remain bound to
the same live client registration through any legal retry.

For a non-null handle, the complete
provider/configuration/client/credential transport-spec admission occurs
during pre-admission, before any Stage 23 pathname observation, namespace
acquisition, metadata-provider I/O, or relevance-provider I/O. A failure
returns exact Stage 23 `FAILED`, outer `decision="retry"`, empty artifacts and
evidence refs, and zero Stage 23 pathname, provider, or authority I/O. It is
never relevance `missing`, `failed`, or `unavailable`.

The raw base URL is not trimmed, deduplicated, repaired, or guessed. It must
parse as lowercase `https` plus authority and an optional path prefix. It
rejects non-HTTPS, userinfo, query, fragment, trailing slash, empty segment,
double slash, backslash, dot segment, percent-encoded slash/backslash/dot
segment, or a path already ending in or containing `/chat/completions`.
Origin is parsed scheme plus authority. Endpoint path is the validated
existing path prefix plus exact `/chat/completions`:

```text
https://host    -> origin https://host, path /chat/completions
https://host/v1 -> origin https://host, path /v1/chat/completions
```

Failure to construct exactly one provider/model/origin/path/spec is a
structured system/configuration failure, not relevance `missing`, `failed`,
or `unavailable`.

The held credential is a raw ASCII token of 1..4,096 bytes, every byte in
`0x21..0x7e`. Whitespace, CTL, CR, LF, DEL, OWS, Unicode, and header injection
are impossible under that grammar. Code alone constructs
`Authorization: Bearer <token>`. The complete actual request head remains at
most 16,384 bytes. Credential absence, invalidity, or head overflow is a
structured system/configuration failure. Credential bytes never enter a
digest, log, exception, diagnostic, report, or artifact. A retry uses the
same held credential identity and exact bytes.

The code-owned paper projection is built only from replayed upstream
authority and has exact key order:

```json
{
  "schema_version": 1,
  "paper_sha256": "<sha256>",
  "abstract_claim_ids": ["<claim-id>"],
  "abstract_sentences": ["<rerendered sentence>"]
}
```

Paper path/hash/size remains separately bound by the exact held Stage 22
FileRef in the user payload. `abstract_claim_ids` equals the complete Stage 19
abstract `ordered_claim_ids` in exact order. Its count is nonzero and no
greater than the independently replayed scientific claim registry's record
count for `target_section=abstract`. IDs are duplicate-free, belong to that
registry section and generation, and satisfy the replayed Stage 19 selection.
For every ID, code independently rerenders the registry claim from held facts;
`abstract_sentences` has identical length/order and contains those exact
nonempty strings. The complete canonical projection is at most 16,384 UTF-8
bytes. Nothing is truncated. Missing, duplicate, foreign, reordered,
rerender-mismatched, empty, or oversized projection data is a structured
system failure. No complete paper, preamble, free prose, caller title,
caller abstract, or Markdown title/abstract extractor is used.

The user content is canonical UTF-8 JSON with exact key order
`schema_version`, `task`, `paper`, `claim_projection`, `cited_keys`, and
`metadata`. `schema_version` is true integer `1`; task is exact
`stage23_paper_relevance`; `paper` is the exact Stage 22 paper
path/hash/size FileRef; `claim_projection` is the object above; `cited_keys`
is the exact sorted report closure; and `metadata` is the citation projection
array in the same order. The canonical serializer emits no BOM, insignificant
space, or trailing newline. The system message is exactly:

```text
Score each ordered citation for relevance to the bounded governed abstract claims. Return only the required JSON object.
```

The OpenAI-compatible request is one `POST` and sends headers in exact on-wire
order:

```text
Host: <endpoint authority>
User-Agent: AutoResearchClaw-Stage23/1
Accept: application/json
Accept-Encoding: identity
Content-Type: application/json; charset=utf-8
Content-Length: <canonical decimal entity length>
Authorization: Bearer <held token>
Connection: close
```

The non-secret request transcript omits only the Authorization line. The
entity is canonical UTF-8 JSON, at most 131,072 bytes, with exact root order
`model`, `messages`, `temperature`, `max_tokens`, `response_format`, and
`stream`. `model` is the held primary model. `messages` has exactly two
objects, each with exact key order `role`, `content`: the first is role
`system` with the fixed system message; the second is role `user` with the
canonical user JSON string. Temperature is the true integer `0`; `max_tokens`
is the true integer `2048`;
`response_format` is exact `{"type":"json_object"}`; `stream` is false.

The relevance response entity cap is 262,144 bytes. Status must be 200 and
media type must be `application/json` with charset absent or UTF-8. The
bounded raw provider envelope is duplicate-safe strict JSON under the common
JSON limits. Standard unrelated bounded envelope fields such as ID, object,
created, and usage may be ignored. The consumed projection requires the
returned model to equal the requested model, exactly one choice, assistant
role, one string content, finish reason exact `stop`, and no tool call.
Assistant content is strict JSON with exact root key `scores`; each array item
has exact keys `cite_key` and `score`. The array has exact cited-key
order/closure. Score is a JSON string matching `[0-9]\.[0-9]{6}`, parsed as
Decimal, canonical in `0.000000..1.000000`; JSON numbers, booleans, NaN, and
Infinity reject.

Only a first-attempt DNS, connect, reset, timeout, or TLS I/O failure before
any response status/header/body byte is a retryable pure transport failure.
Certificate, hostname, configuration, type, credential, endpoint, or policy
failure is a structured system failure. Any error after any response material
is relevance `failed`, including status-only, redirect, 429/5xx, wrong media
type, invalid head, partial/truncated/overflow entity, provider-envelope
failure, empty content, or score-schema/semantic failure. A legal second
outbound uses identical provider, model, origin, target, ordered non-secret
headers, entity bytes, request digest, timeout/cap policy, and held credential
identity. It cannot lead to a third outbound.

##### 18.13.2.5 Relevance states and system-failure boundary

`missing` is produced if and only if the structured relevance LLM handle is
exactly `llm is None` before semantic request construction. It is never
selected by a model response, empty response, malformed response, config
string, caller flag, persisted artifact, transport error, or missing/invalid
credential. If `llm` is non-null, Stage 23 begins exactly one semantic
operation and its report state can only be `complete`, `failed`, or
`unavailable`.

Canonical configuration that cannot uniquely construct provider, model,
origin, endpoint, held credential, or transport spec; a wrong type; or any
policy/system validation failure is outside the four-state relevance union.
It returns exact Stage 23 `FAILED`, outer `decision="retry"`, empty artifacts
and evidence refs, manifest-first cleanup where applicable, and zero Stage 23
authority publication. It is not relevance `missing`, `failed`, or
`unavailable`, and cannot degrade.

The four persisted report states are mutually exclusive and exhaustive:

| Status | Semantic calls | Outbound attempts | Request SHA | Response SHA | Retry fingerprint | Scores | Error class |
|---|---:|---:|---|---|---|---|---|
| `complete` | 1 | 1 or 2 | non-null | non-null | null for 1; request SHA for 2 | exact complete ordered closure | null |
| `failed` | 1 | 1 or 2 | non-null | non-null | null for 1; request SHA for 2 | `[]` | `relevance_semantic_failure` |
| `unavailable` | 1 | 1 or 2 | non-null | null | null for 1; request SHA for 2 | `[]` | `pure_transport_exhausted` |
| `missing` | 0 | 0 | null | null | null | `[]` | `relevance_missing` |

`incomplete` is not a fifth persisted status; it denotes only an invalid or
unfinished in-memory operation and fails Stage 23. In particular, this legal
sequence:

```text
attempt 1 -> pure transport failure before any response material
attempt 2 -> identical request -> malformed/schema-invalid/semantic-invalid response
```

is exact `failed`, `semantic_calls=1`, `outbound_attempts=2`,
`retry_fingerprint=request_sha256`, empty scores, and
`error_class=relevance_semantic_failure`. It is not `unavailable`, cannot
retry again, and follows the existing claim-scope outcome matrix:
`research_release` fails; `pipeline_validation` and `exploratory` degrade.

#### 18.13.3 Exact verification report schema v2

`verification_report.json` has exactly 13 root keys. A complete passed example
for one citation is:

```json
{
  "schema_version": 2,
  "publication_stage_id": "stage23",
  "paper": {
    "path": "stage-22/paper_final.md",
    "sha256": "<sha256>",
    "size": 1
  },
  "bibliography": {
    "path": "stage-22/references.bib",
    "sha256": "<sha256>",
    "size": 1
  },
  "cited_count": 1,
  "cited_keys": [
    "Example2026"
  ],
  "claim_scope": "research_release",
  "provider_policy": {
    "route_policy": "doi-else-arxiv-else-title-v1",
    "max_outbound_per_citation": 1,
    "fallback": false,
    "retry": false,
    "cache": false
  },
  "relevance_policy": {
    "max_semantic_calls": 1,
    "max_outbound_attempts": 2,
    "retry_class": "pure-transport-before-response-only",
    "threshold": "0.500000"
  },
  "citations": [
    {
      "cite_key": "Example2026",
      "route_class": "crossref-doi",
      "endpoint_class": "crossref",
      "request_sha256": "<sha256>",
      "response_sha256": "<sha256>",
      "outbound_count": 1,
      "status": "verified",
      "metadata": {
        "title": "Example title",
        "doi": "10.1000/example",
        "arxiv_id": null,
        "year": 2026,
        "source": "crossref"
      }
    }
  ],
  "relevance": {
    "status": "complete",
    "semantic_calls": 1,
    "outbound_attempts": 1,
    "request_sha256": "<sha256>",
    "response_sha256": "<sha256>",
    "retry_fingerprint": null,
    "scores": [
      {
        "cite_key": "Example2026",
        "score": "0.750000"
      }
    ],
    "error_class": null
  },
  "outcome": "passed",
  "generated": "2026-07-28T00:00:00+00:00"
}
```

The exact root key set is `schema_version`, `publication_stage_id`, `paper`,
`bibliography`, `cited_count`, `cited_keys`, `claim_scope`,
`provider_policy`, `relevance_policy`, `citations`, `relevance`, `outcome`,
and `generated`. Duplicate, extra, or missing root/nested keys reject.

`schema_version` is true integer `2`; the stage ID is exact `stage23`.
`paper` and `bibliography` are exact Stage 22 `FileRef`s. `cited_count` is a
true integer `1..32` equal to both array lengths. `cited_keys` is the exact
unique sorted closure.

`claim_scope` is exact `pipeline_validation`, `exploratory`, or
`research_release` and comes only from the replayed canonical experiment
contract. `provider_policy` has exactly `route_policy`,
`max_outbound_per_citation`, `fallback`, `retry`, and `cache`, with values
exact `doi-else-arxiv-else-title-v1`, true integer `1`, false, false, false.
`relevance_policy` has exactly `max_semantic_calls`,
`max_outbound_attempts`, `retry_class`, and `threshold`, with exact values true
integer `1`, true integer `2`,
`pure-transport-before-response-only`, and canonical decimal string
`0.500000`.

Every `citations` entry has exactly `cite_key`, `route_class`,
`endpoint_class`, `request_sha256`, `response_sha256`, `outbound_count`,
`status`, and `metadata`; array order equals `cited_keys`. Route is exact
`crossref-doi`, `arxiv-id`, or `openalex-title`, and endpoint class is exact
`crossref`, `arxiv`, or `openalex` under the fixed precedence rule.
`request_sha256` binds the exact canonical non-secret request transcript and
`response_sha256` binds the exact canonical bounded response transcript under
Section 18.13.2.1. Both are lowercase SHA-256 and are independently recomputed
from held in-memory transport captures before those captures are discarded.
`outbound_count` is the true integer `1` for every publishable metadata result
and cross-matches transport telemetry; a publishable metadata result has exact
status `verified`.
`metadata` has exactly `title`, `doi`, `arxiv_id`, `year`, and `source`.
Title is nonempty normalized metadata text. DOI is a normalized DOI string
for the Crossref route and otherwise null. `arxiv_id` is a normalized arXiv ID
for the arXiv route and otherwise null. Year is a true integer in
`1000..2999`; source is exact `crossref`, `arxiv`, or `openalex` matching the
route.
Metadata is evidence of bibliographic identity, not evidence that a paper
supports a manuscript claim.

The only report-schema delta made by B5-D1E is nested: `relevance` changes from
six to exactly eight keys in canonical order `status`, `semantic_calls`,
`outbound_attempts`, `request_sha256`, `response_sha256`,
`retry_fingerprint`, `scores`, and `error_class`. Report schema version remains
true integer `2` and the exact 13 root keys above do not change.

For `complete`, semantic calls is true integer `1`, outbound attempts is `1`
or `2`, both request and response SHA are non-null lowercase SHA-256, scores
has exact cited-key closure/order, and error class is null. Each score has
exactly `cite_key` and `score`; score is a six-place canonical decimal string
in `[0.000000,1.000000]`. `retry_fingerprint` is null for one outbound and
equals `request_sha256` for two.

For exhausted pure transport, status is exact `unavailable`, semantic calls
is true integer `1`, outbound attempts is `1` or `2`, request SHA is non-null,
response SHA is null, scores is empty, and error class is exact
`pure_transport_exhausted`. Retry fingerprint is null for one outbound and
equals request SHA for two.

For exact `missing`, semantic calls and outbound attempts are true integer
zero; request SHA, response SHA, and retry fingerprint are null; scores is
empty; and error class is exact `relevance_missing`. For a response-level
semantic/schema failure, status is `failed`, semantic calls is true integer
`1`, outbound attempts is `1` or `2`, both digests are non-null, scores is
empty, error class is exact `relevance_semantic_failure`, and retry
fingerprint is null for one outbound or request SHA for two. No missing,
unavailable, or failed branch fabricates a score. Low relevance is represented
only by a complete score array containing at least one value below `0.500000`.
No raw provider body, prompt, secret, URL query credential, traceback, or
unbounded diagnostic is persisted.

`outcome` is exact `passed` or `degraded` and is mechanically derived from
`claim_scope`, upstream quality, existence closure, and relevance under
Section 18.13.5. The report persists no raw response, raw request, prompt, API
key, credential, reasoning, chain-of-thought, or provider body. Only exact
request/response digests and the bounded parsed metadata are retained.
`generated` is copied exactly from the fully replayed Stage 22 manifest;
Stage 23 makes zero authority wall-clock calls; bounded transport deadlines
use only an unpersisted monotonic clock as frozen in Section 18.13.2.1.

#### 18.13.4 Exact Stage 23 manifest schema v2 and four-file tuples

The complete structured Stage 23 namespace is exactly four regular,
single-link formal files:

```text
stage-23/verification_report.json
stage-23/references_verified.bib
stage-23/paper_final_verified.md
stage-23/stage23_verification_manifest.json
```

There is no run-internal temporary, staging, nested directory, cache,
diagnostic, raw response, repair output, or alternate name. The first three
are payloads; the manifest is strictly last.

`stage23_verification_manifest.json` has true-integer schema version `2` and
exactly 26 root keys:

```json
{
  "schema_version": 2,
  "publication_stage_id": "stage23",
  "publication_mode": "structured-scientific-claim-v1",
  "structured_capability_schema_version": 1,
  "structured_capability_snapshot": {
    "stage17_publication": 1,
    "stage19_revision": 1,
    "stage20_replay": 1,
    "stage24_and_release_integration": 0
  },
  "generation_binding_sha256": "<sha256>",
  "canonical_experiment_evidence": {
    "path": "canonical_experiment_evidence.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "cfs": {
    "schema_version": 1,
    "sha256": "<sha256>"
  },
  "source_stage19_manifest": {
    "path": "stage-19/scientific_claim_authority_manifest.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_stage20_manifest": {
    "path": "stage-20/quality_gate_manifest.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_stage21_manifest": {
    "path": "stage-21/bundle_index.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_stage22_manifest": {
    "path": "stage-22/stage22_export_manifest.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_paper": {
    "path": "stage-22/paper_final.md",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_bibliography": {
    "path": "stage-22/references.bib",
    "sha256": "<sha256>",
    "size": 1
  },
  "source_latex": {
    "path": "stage-22/paper.tex",
    "sha256": "<sha256>",
    "size": 1
  },
  "quality_outcome": "passed",
  "degradation_signal": null,
  "claim_scope": "research_release",
  "verification_policy": {
    "route_policy": "doi-else-arxiv-else-title-v1",
    "min_cited_count": 1,
    "max_cited_count": 32,
    "max_provider_outbound_per_citation": 1,
    "provider_fallback": false,
    "provider_retry": false,
    "provider_cache": false,
    "relevance_max_semantic_calls": 1,
    "relevance_max_outbound_attempts": 2,
    "relevance_retry_class": "pure-transport-before-response-only",
    "relevance_threshold": "0.500000"
  },
  "outcome": "passed",
  "verification_report": {
    "path": "stage-23/verification_report.json",
    "sha256": "<sha256>",
    "size": 1
  },
  "verified_bibliography": {
    "path": "stage-23/references_verified.bib",
    "sha256": "<sha256>",
    "size": 1
  },
  "verified_paper": {
    "path": "stage-23/paper_final_verified.md",
    "sha256": "<sha256>",
    "size": 1
  },
  "output_count": 3,
  "outputs": [
    {
      "role": "verification_report",
      "logical_name": null,
      "path": "stage-23/verification_report.json",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "verified_bibliography",
      "logical_name": null,
      "path": "stage-23/references_verified.bib",
      "sha256": "<sha256>",
      "size": 1
    },
    {
      "role": "verified_paper",
      "logical_name": null,
      "path": "stage-23/paper_final_verified.md",
      "sha256": "<sha256>",
      "size": 1
    }
  ],
  "generated": "2026-07-28T00:00:00+00:00"
}
```

The exact root key set is:

```text
schema_version
publication_stage_id
publication_mode
structured_capability_schema_version
structured_capability_snapshot
generation_binding_sha256
canonical_experiment_evidence
cfs
source_stage19_manifest
source_stage20_manifest
source_stage21_manifest
source_stage22_manifest
source_paper
source_bibliography
source_latex
quality_outcome
degradation_signal
claim_scope
verification_policy
outcome
verification_report
verified_bibliography
verified_paper
output_count
outputs
generated
```

Every Stage 23 `FileRef` has exact keys `path`, `sha256`, and `size` under the
Stage 22 rules. CFS, capability, generation, and timestamp rules remain exact.
`source_stage19_manifest`, `source_stage20_manifest`,
`source_stage21_manifest`, `source_stage22_manifest`, the three Stage 22
source files, `quality_outcome`, and `degradation_signal` are independently
rebuilt from held recursive authority; stored Stage 23 values are never an
oracle. `degradation_signal` is null for upstream `passed` and the exact
three-key root signal `FileRef` for upstream `degraded`.

`claim_scope` is exact `pipeline_validation`, `exploratory`, or
`research_release` from the canonical experiment contract.
`verification_policy` has exactly `route_policy`, `min_cited_count`,
`max_cited_count`, `max_provider_outbound_per_citation`,
`provider_fallback`, `provider_retry`, `provider_cache`,
`relevance_max_semantic_calls`, `relevance_max_outbound_attempts`,
`relevance_retry_class`, and `relevance_threshold`, with exactly the values
shown in the example. `outcome` is exact `passed` or `degraded` and
cross-matches the strict report and Section 18.13.5.
`verification_report`, `verified_bibliography`, and `verified_paper` are exact
three-key `FileRef`s and cross-match exactly one output row each.

`output_count` is the true integer `3`. `outputs` contains exactly three
five-key output entries in exact order `verification_report`,
`verified_bibliography`, `verified_paper`; every fixed row has
`logical_name=null`. The verified paper
bytes exactly equal the Stage 22 source paper. The verified bibliography
contains exactly the cited keys, in their exact source BibTeX entry bytes and
source order, and contains no uncited or rewritten entry. The report is the
exact schema-v2 metadata/relevance evidence. No fourth output row or nested
entry is legal.

The exact structured success tuples are:

```text
artifacts = (
  "verification_report.json",
  "references_verified.bib",
  "paper_final_verified.md",
  "stage23_verification_manifest.json",
)

evidence_refs = (
  "stage-23/verification_report.json",
  "stage-23/references_verified.bib",
  "stage-23/paper_final_verified.md",
  "stage-23/stage23_verification_manifest.json",
)
```

These exact four-file tuples, in this order, are used for both `passed` and
`degraded`. The manifest itself is deliberately excluded from its `outputs`
array and has no root field for its own path/hash/size/identity. Only the
external result tuple and a future consumer capture the manifest bytes and
digest. Thus the binding DAG is upstream manifests -> three payloads -> Stage
23 manifest; there is no manifest-to-itself edge and no self-hash cycle.

#### 18.13.5 Fixed Stage 23 outcome matrix

The matrix is exhaustive and code-owned:

| Citation existence | Relevance | Claim scope | Upstream quality | Structured Stage 23 result |
|---|---|---|---|---|
| any result not exact `verified`, or cited closure invalid | any/not called | any | either | exact `FAILED`, manifest-first cleanup, empty tuples |
| all exact `verified` | complete and every score `>=0.500000` | any | `passed` | `DONE`, `outcome=passed`, exact four-tuples |
| all exact `verified` | complete and every score `>=0.500000` | any | `degraded` | `DONE`, `outcome=degraded`, exact four-tuples |
| all exact `verified` | missing, unavailable, failed, or any score `<0.500000` | `research_release` | either | exact `FAILED`, manifest-first cleanup, empty tuples |
| all exact `verified` | missing, unavailable, failed, or any score `<0.500000` | `pipeline_validation` | either | `DONE`, `outcome=degraded`, exact four-tuples |
| all exact `verified` | missing, unavailable, failed, or any score `<0.500000` | `exploratory` | either | `DONE`, `outcome=degraded`, exact four-tuples |
| provider/relevance call-bound, retry-class, fallback, cache, or route violation | any | any | either | exact `FAILED`, cleanup, empty tuples |
| publication, source fixpoint, namespace, tuple, immediate, or terminal postcondition fails | irrelevant | any | either | exact `FAILED`, cleanup, empty tuples |

Existence verification is mandatory for every scope. Relevance is
claim-scope-sensitive exactly as shown: any adverse relevance state fails
`research_release` and degrades only `pipeline_validation` or `exploratory`.
`incomplete` is not a matrix state and cannot reach a `DONE` row; an invalid
or unfinished in-memory relevance operation fails Stage 23 through the
publication/postcondition failure row with cleanup and empty tuples.
A low score is paper-level relevance evidence; it neither deletes the
citation nor decides scientific support. Pure-transport exhaustion records
absence of relevance evidence, not a fabricated score. Upstream `degraded`
always remains degraded even when relevance is complete and high. No Stage 23
outcome is a release verdict; B5-D2 owns that decision. For every exact
`FAILED` authority outcome the outer framework disposition is separately and
invariably `decision="retry"`; that disposition is not a Stage 23 authority
outcome and cannot change cleanup, empty tuples, transport limits, or
dispatch.

#### 18.13.6 Held-fd publication and executor lifecycle

The successful context phases are exact:

```text
Stage23PreAdmissionContext(pre_admission)
  -> Stage23AttemptContext(namespace_bound)
  -> invalidated
  -> snapshot_a
  -> metadata_verified
  -> relevance_complete
  -> payloads_published
  -> source_fixpoint
  -> manifest_published
  -> provisional_done
  -> immediate_validated
  -> terminal_validated
  -> cleared
```

Every legal member of the exact four-state relevance union takes the same
phase transition:

```text
metadata_verified -> relevance_complete
```

This includes `complete`, `failed`, `unavailable`, and the zero-semantic-call
`missing` branch. `relevance_complete` means only that the bounded operation
has ended and its result is one valid union member. It does not imply that a
request was constructed, an outbound occurred, scores are available, or the
relevance result is successful.

After the code-owned claim-scope outcome matrix is derived, a publishable
branch takes:

```text
relevance_complete -> payloads_published
```

This includes degraded `pipeline_validation` and `exploratory` results for
`missing`, `failed`, `unavailable`, or a complete low score. A non-publishable
branch takes:

```text
relevance_complete -> failed_cleanup -> cleared
```

This includes the same adverse states under `research_release`. It returns
exact Stage 23 `FAILED`, outer `decision="retry"`, and empty artifacts and
evidence refs. A system/configuration failure and an `incomplete` in-memory
operation are outside the four-state union and never enter
`relevance_complete`; they fail and clear without publishing authority.

1. **Dispatch and pre-admission.** Apply the `1110` mechanical boundary and
   validate the upstream-only single-use context. Fully capture/replay current
   Stage 22 and recursive authority, and validate the exact `1..32` cited-key
   closure, before any Stage 23 path or provider I/O.
2. **Single transition.** Consume pre-admission, observe canonical `stage-23`
   absent through the held run fd, create it, open the newly created directory,
   validate held identity, and issue one attempt context. No prior Stage 23
   existence observation is legal.
3. **Manifest-first absence verification.** Through the newly acquired held
   Stage 23 fd, verify `stage23_verification_manifest.json` absent first, then
   `verification_report.json`, `references_verified.bib`, and
   `paper_final_verified.md` absent in that order, and require the directory
   still empty. Any entry means post-acquisition collision or identity drift
   and rejects without adoption or deletion. This is the idempotent
   `invalidated` transition, not stale-stage cleanup.
4. **Snapshot A and cited closure.** Recapture/replay all upstream bindings and
   require exact pre-admission equality. Independently rebuild and revalidate
   the same exact sorted `1..32` cited-key closure.
5. **Bounded metadata transport.** Execute the single deterministic route and
   at-most-one outbound contract for each key. Validate exact metadata closure
   and counters before relevance.
6. **Bounded relevance transport.** Execute at most one semantic call and at
   most two outbounds under the pure-transport-only identical retry rule.
   Mechanically derive the exact outcome matrix.
7. **Build payloads.** In memory, build strict report bytes containing only
   parsed metadata plus exact request/response digests, select exact
   cited-entry bibliography bytes without rewriting, and byte-copy the held
   paper. Raw requests, responses, prompts, credentials, reasoning, and
   transport logs are discarded and never persisted.
8. **Three exclusive creates.** In exact output/tuple order
   `verification_report.json`, `references_verified.bib`,
   `paper_final_verified.md`, require each payload name absent and create it
   through the held Stage 23 fd with
   `O_CREAT|O_EXCL|O_NOFOLLOW`; require regular single-link identity; write,
   flush, sync, read back, hash, and validate only through the creating fd.
   There is no rename, replace, reopen-for-write, temp, or staging.
9. **Payload replay and source fixpoint.** Require exact three-file flat
   namespace, bytes, identities, report semantics, paper byte identity,
   bibliography closure, call counters, and outcome. Recapture upstream and
   require Snapshot A equality and current Stage 20/21/22 manifest-name
   identities.
10. **Manifest last.** Require the formal manifest absent and create/write it
    through the same exclusive creating-fd protocol. Strictly parse and
    independently rebuild it; it is only a commit candidate.
11. **Final replay and Snapshot B.** Replay manifest, payloads, provider route
    decisions, request/response digests, counters, claim-scope outcome, exact
    tuples, and recursive upstream bindings. Require Snapshot B's
    upstream/source portion equal Snapshot A; the complete Snapshot B also
    captures all payload/manifest bytes and parent/leaf identities. Require a
    second final capture to equal that complete Snapshot B.
12. **Immediate postcondition.** Before a public return, validate provisional
    `DONE`, `degraded` flag, exact four-tuple count/order, disk namespace,
    manifest-last replay, transport bounds, live context, writer epoch,
    Snapshot A/B, and upstream current authority.
13. **Terminal postcondition.** Structured Stage 23 skips PRM, HITL, edit,
    prompt, semantic-repair, and repair hooks. Immediately before return,
    repeat the capture/replay and reject any changed status, tuple, flag,
    byte, identity, route, counter, context, source, or current upstream
    manifest.
14. **Public result or withdrawal.** Only both validators permit public
    `DONE` with the exact four-tuples and the matrix-derived degraded flag.
    Any producer, cleanup, provider, publication, handoff, immediate, terminal,
    or public-result failure performs manifest-first aggregate cleanup and
    returns exact `status=FAILED`, outer `decision="retry"`, and empty tuples.

Cleanup uses held original fds, never a replaced parent path. A preexisting or
late directory, symlink, hardlink, FIFO, socket, device, special file, nested
entry, or identity mismatch at any of the four reserved flat names is a
collision. Cleanup must not follow, truncate, overwrite, or recurse into it.
The original error remains primary; cleanup errors append. A failed current
epoch must not leave a completely replayable current v2 manifest. Later
stored-state reappearance cannot create authority without a fresh complete
admission and strict semantic replay. This is an in-scope content-recognition
rule, not protection against excluded same-UID syscall interference or proof
of historical producer identity.

### 18.14 B5-D1B-R1 adversarial contract

The Stage 22-23 implementation slice MUST add all of these untrusted-data and
deterministic filesystem-fault tests without changing the frozen outcomes.
Filesystem replacement rows mean mutation injected at a documented
phase/capture boundary under Section 1.1, not arbitrary syscall
interleavings:

| Area | Attack | Required result |
|---|---|---|
| Capability | exact current `1110` public/direct structured entry; caller/persisted `1111`; boolean, missing, extra, or unknown capability key | ordinary dispatch remains generic-v1; direct structured rejects before lock/fs/compiler/provider |
| Pre-admission | forged, copied, serialized, reused, cross-run/owner/epoch/stage/generation context | reject before downstream namespace or provider I/O |
| Stage namespace acquisition | initially preexist canonical `stage-22` or `stage-23` as a real empty directory, prior stage, crash residue, symlink, special object, different-device directory, or observable mount-point collision; or fault-inject a newly created directory's pre-write emptiness, parent, name, or fd identity check | reject before compiler/provider/output I/O; no adoption, stale-stage cleanup, or attempt context; validation failure may best-effort remove only its own newly created empty reserved directory through the held parent |
| Parent replacement fault | at a documented boundary replace root, run, Stage 20/21/22/23, code parent, or signal parent after fd capture | no path rediscovery; held-original operations only; final name/identity replay fails closed |
| Upstream withdrawal | remove, replace, or mutate current Stage 20, 21, or 22 manifest between pre-admission, Snapshot A, fixpoint, Snapshot B, or postconditions | reject/withdraw current success; manifest-first cleanup; empty tuples |
| Late mutation | mutate a Stage 22/23 payload or manifest after publication, immediate validation, or final capture | terminal postcondition fails and cleans |
| Synchronous stored forgery | rewrite payloads, reports, nested hashes, sizes, manifests, and timestamps consistently while changing independently expected content, source, or generation | independent expected rebuild and exact-byte replay reject; an exact expected-byte copy is semantically equivalent and makes no historical-producer claim |
| Stage 22 nested tree | preexist or inject at a documented boundary a symlink, hardlink, FIFO, socket, device, static different-device directory, preexisting observable mount-point collision, empty directory, extra leaf, missing leaf, prefix collision, case/NFC alias, or changed held identity | fail closed; never follow or recursively clean an unvalidated entry |
| Stage 23 flat namespace | inject any nested directory or special file at a reserved name | fail closed; exact four-file namespace not published |
| Cleanup collision | precreate a formal name or fault-inject a regular/special/directory collision at a documented boundary; manifest cleanup succeeds but payload cleanup fails | no later create; aggregate cleanup error; no replayable manifest |
| Absent-only publication | fault-inject `O_EXCL` collision or canonical-name drift at a documented boundary, or request rename/replace/link/reopen-for-write | identity/fd contract rejects; cleanup; empty tuples |
| Stage 22 compiler | call LLM/provider; second semantic compile; `max_attempts != 2`; third internal attempt; mutate input; claim success without valid PDF; retain PDF on failure | fail and clean; no success tuples |
| Stage 22 schema | use any withdrawn root; two-key FileRef; output `kind`; non-null fixed logical name; null template/project/support logical name; PDF after template/project; wrong template/project sort; missing or reordered README/requirements | exact 23-key/nested/order replay rejects |
| Stage 22 tuple | substitute `paper.tex` for `code/`; omit `code/`; use `code` or another wrong directory slash/prefix; reorder, omit, or duplicate any tuple member; or return `DONE` before validators | immediate/terminal reject; exact empty failure tuples |
| Stage 23 cited closure | 0, 33, duplicate, missing, unplanned, Markdown/LaTeX mismatch, or shadow bibliography key | fail before provider/output I/O |
| Provider route | violate DOI -> Crossref, else arXiv ID -> arXiv, else title -> OpenAlex precedence; choose alternate provider, fallback, retry, cache, batch substitution, redirect, or second outbound | call-bound/route failure; cleanup; empty tuples |
| Provider request wire | change method/origin/path/query parameter spelling/order, `%HH` case, `%20` to `+`, DOI `%2F`, Host/header order/value, body, proxy, port, or endpoint prefix without changing a body-only digest | canonical non-secret request transcript changes or strict route rejects; no outbound substitution |
| Provider response wire | redirect, 206, 429, 5xx, wrong media/charset, nonidentity compression, TE+CL, bad length, chunk extension/trailer, truncation, body cap+1, response-head 16,385th byte, or line cap+1 | non-`verified`; zero fallback/retry; bounded canonical response digest; raw bytes discarded |
| Provider JSON | duplicate key at any depth, extra consumed key, missing/wrong-type consumed field, depth/container/string overflow, NaN/Infinity, or boolean used as integer | strict bounded parser rejects; citation non-`verified` |
| Provider XML | DTD, entity, XInclude, external read, fifth/no element namespace, namespace rebinding, forged non-Atom authority field, depth/element/text overflow | strict parser rejects; OpenSearch/arXiv extensions never become authority |
| DOI identity | provider title is similar or exact but returned normalized DOI differs; title-only result substitutes for DOI | reject; DOI exact match is mandatory |
| arXiv response URI | dereference Atom ID; use non-arXiv host, uppercase host, userinfo, port, query, fragment, PDF path, percent encoding, backslash, double slash, dot segment, no version, over-64-byte identifier, or non-HTTP(S) scheme | reject; only exact bounded HTTP/HTTPS `arxiv.org/abs/<versioned-id>` identifier strings normalize; outbound remains HTTPS |
| arXiv version identity | explicit request `base vN` receives another/no version; versionless request receives no version; any request receives another base; title/year/LLM/rank/caller substitutes for ID | reject; versionless request accepts only same-base legal versioned response, explicit request requires exact base/version, and projected ID retains response `vN` |
| OpenAlex ambiguity | zero exact-title candidates, multiple exact candidates, `meta.count > 200`, count/results mismatch, rank/year/DOI/LLM/caller tie-break | reject as non-verified; no second page or provider |
| Provider projection | admit complete provider envelope, raw body, unused field, prompt, reasoning, secret, or credential into authority | exact combined citation/metadata projection rejects; sensitive/raw material is never published |
| Provider evidence | change or omit endpoint/route class, canonical request SHA-256, canonical response SHA-256, or hash reserialized/decoded bytes rather than the captured transcript | report/manifest replay rejects; raw request/response remains unpersisted |
| Stored evidence forgery | synchronously rewrite parsed projection, route, counters, request/response hashes, report, and manifest without the current held transport capture | independent current-epoch capture/parse/digest rebuild rejects; stored values are not transport authority |
| Relevance first-response failure | first outbound returns malformed/empty, duplicate/extra/missing field, wrong order/model/choice/role/finish, nonfinite/out-of-range/numeric/bool score, status/header/partial body, redirect, 429, or 5xx | exact `failed` with semantic=1/outbound=1, non-null digests, null retry fingerprint, no retry/repair |
| Relevance second-response failure | first outbound pure-fails before response; identical second receives malformed/schema-invalid/semantic-invalid response or any response material followed by failure | exact `failed` with semantic=1/outbound=2, retry fingerprint=request SHA, empty scores; no third outbound |
| Relevance successful retry | first outbound pure-fails before response; identical second returns exact complete closure | exact `complete` with semantic=1/outbound=2 and retry fingerprint=request SHA |
| Relevance exhausted transport | first and second identical outbounds both pure-fail before any response material | exact `unavailable` with semantic=1/outbound=2, response SHA null, retry fingerprint=request SHA |
| Relevance partial response | any status/header/body byte precedes timeout/reset/truncation, including invalid/conflicting head | response SHA non-null; exact `failed`; zero further retry |
| Relevance missing | `llm is None` before semantic request construction | exact `missing` with semantic/outbound 0/0, all digests/fingerprint null, empty scores |
| Relevance missing impersonation | non-null LLM returns empty/malformed content, caller/config/persisted flag says missing, or credential/provider/spec is absent/invalid | response case is `failed`; config/system case is Stage 23 `FAILED` outside the relevance union; never `missing` |
| Relevance provider admission | use `openai`, `openrouter`, `deepseek`, `novita`, `minimax`, or `openai-compatible` with the exact admitted canonical fields and wire policy | admit only exact `stage23-openai-chat-completions-bearer-v1`; generic-v1 provider behavior remains unchanged |
| Relevance provider rejection | use `anthropic`, `kimi-anthropic`, `ollama`, `acp`, `claude-cli`, `codex-cli`, an unknown provider, `wire_api=responses`, or any non-`chat_completions` wire | structured system/configuration failure before any Stage 23 pathname/provider I/O; FAILED/retry/empty tuples; zero authority |
| Relevance canonical admission | use nonempty canonical base URL, exact `chat_completions`, one visible-ASCII 1..256-byte primary model, empty fallback tuple, and disabled MetaClaw bridge | construct exactly one held transport spec from replayed canonical values; no preset/default/fallback inference |
| Relevance canonical admission drift | preset-filled/empty/repaired base URL; normalized wire alias; nonempty fallback models; enabled MetaClaw bridge; extra headers; fallback URL; proxy bridge; empty, non-ASCII, control-containing, or over-256-byte primary model | structured system/configuration failure before Stage 23 pathname/provider I/O; no degrading or alternate route |
| Relevance client identity | non-null active client is factory-created, registered, same-run/owner/epoch, live, and exact-match for canonical provider/base URL/model/wire | admit; read its held credential exactly once into the same registered transport spec |
| Relevance client forgery | fake, copied, caller-created, unregistered, wrong-run/owner/epoch, inactive, wrong-type, config-mismatched, Anthropic-adapted, fallback-enabled, bridged, or proxy client | structured system/configuration failure before Stage 23 pathname/provider I/O; never `missing`; no env/config/caller secret reread |
| Relevance configuration | ambiguous/non-HTTPS/bad-prefix endpoint, wrong-type provider/model/spec, missing/illegal credential, oversized request head, or paper-projection replay/rerender/cap failure | exact Stage 23 `FAILED`, decision retry, empty tuples, zero authority publication; never degraded |
| Relevance abstract projection | omit, duplicate, reorder, import foreign ID, alter rerendered sentence, include full paper/free prose/caller abstract, truncate, or exceed 16,384 bytes | structured system failure before semantic outbound; no authority publication |
| Relevance request identity | change provider/model/origin/path/header/body/timeout, credential identity, or request SHA on retry; use body-only fingerprint | call-bound failure; no second semantic call or authority publication |
| Relevance hidden behavior | SDK/generic `LLMClient.chat`, ClaimVerifier, hidden retry, model/endpoint fallback, cache, semantic repair, second semantic call, third outbound, or aggregate outbound 35 | exact Stage 23 `FAILED`; not degraded; cleanup |
| Relevance state schema | use old six-key object, omit/extra/reorder digest keys, uppercase/wrong-length digest, `failed(2)` with null fingerprint, response material with null response SHA, or persist `incomplete` | exact eight-key union replay rejects |
| Relevance numeric semantics | JSON numeric score, boolean, duplicate key, NaN/Inf, more/fewer than six decimals, out-of-range Decimal, wrong cited key/order, or fabricated score in adverse state | strict rejection; no retry/repair; claim-scope outcome matrix applies |
| Secret/raw leakage | persist or log Authorization/token, raw request/response, canonical transcript, prompt, complete provider envelope, reasoning, traceback, or unbounded diagnostic | fail closed; no publishable Stage 23 authority |
| Outcome crossing | publish passed with adverse relevance or degraded upstream; degrade `research_release`; degrade a non-verified citation; mismatch claim scope/report/manifest/degraded flag | exact claim-scope matrix rejects |
| Relevance phase union | route `complete`, `failed`, `unavailable`, or zero-call `missing` through any phase other than `metadata_verified -> relevance_complete` | lifecycle rejects; `relevance_complete` records only a completed legal union member |
| Relevance phase publication | publishable complete/high or pipeline-validation/exploratory adverse result bypasses `relevance_complete`, or uses a phase specific to missing/failed/unavailable | reject; every publishable union member uses `relevance_complete -> payloads_published` |
| Relevance phase failure | research-release adverse result reaches payload creation, or system/configuration failure or `incomplete` enters `relevance_complete` | reject and withdraw; legal non-publishable union uses `relevance_complete -> failed_cleanup -> cleared`; system/incomplete never enters the union phase |
| Stage 23 schema | change report v2 exact 13 roots, use old six-key relevance, use a withdrawn 23-key manifest root set, two-key FileRef, wrong 26-key set, old output role/order, non-null fixed logical name, or omit claim scope/policy/upstream quality binding | exact schema and output replay rejects |
| Manifest tuple | retain withdrawn root names; wrong role/logical_name/path/hash/size/count/order; add `kind`; include manifest in outputs; or add manifest self path/hash | exact-key/replay rejection; no self-hash cycle |
| Executor | producer mutates provisional status/decision/degraded flag/context/tuple; hook converts `DONE`; circular context/result; public return before terminal validation | withdraw and clean; exact `status=FAILED`, outer `decision="retry"`, empty tuples |
| Generic-v1 | legal generic input with stale structured v2 formal names | unchanged generic schemas, bytes, calls, retries, cleanup, and result behavior |

Nested-tree attacks are Stage 22-specific. Stage 23 is intentionally flat and
rejects every directory. All cleanup is reserved-name, descriptor-relative,
manifest-first, non-following, and non-recursive unless a Stage 22 code
directory has first passed the exact expected-tree identity validation.

The following are OS-isolation stress/non-goal cases, not ResearchClaw
application acceptance gates: a malicious noncooperating same-UID replacement
in the exact `mkdirat -> openat`, check -> unlink, validation -> write, or
final-capture syscall window; debugger/process-memory/fd interference; and
privileged or same-filesystem bind-mount injection during an attempt. Such
tests may be diagnostic, but their result cannot establish or invalidate the
trusted-local contract. A static preexisting symlink, special object,
wrong-filesystem object, or directory collision remains an in-scope
fail-closed test.

## 19. Generic and release boundary

`generic-v1` Stage 18, Stage 19, Stage 20, Stage 21, Stage 22, and Stage 23
schemas, bytes, artifact names, provider behavior, retries, caches, cleanup,
and failure behavior remain unchanged. At map `1000`, malformed/partial
states, and the current `1110`, ordinary dispatch remains generic after its
mandatory common canonical replay.

Generic code does not enumerate, parse, require, publish, consume, block on, or
clean any structured-only `scientific_claim_*` Stage 19 name or any of the
four structured Stage 20 temporary names. Stale structured artifacts cannot
activate the structured path and cannot affect generic bytes or results.
Artifact presence, caller, config, environment, or persisted snapshot is never
a discriminator.

Once a valid `1111` domain-v2 dispatch enters the structured path, every later
error is `FAILED`; generic fallback is forbidden.

Structured Stage 21-23 implementation is outside the
B5-D1A/B5-D1B-R1/B5-D1D/B5-D1E docs-only slices. Stage 24/25, independent
reconstruction, E9, `release_check`, release gates, and fresh F0 are deferred
to B5-D2 or later.
None of these design slices changes release authority. Fresh F0 requires separate
explicit authorization.

## 20. B1-B5 milestone split

B1 and B2 supplied strict records, identities, deterministic construction,
rendering, and selection. B3 supplied structured Stage 17 publication and the
separate `1000` declaration.

B4-D0 freezes the Stage 19 design, B4-D1 freezes the Stage 20 success
authority, and B4-D2 replaces only the Stage 20 staging transaction with the
four-file creating-fd transaction. B4-D3 corrects the Stage 20 deletion and
portable cleanup claims without changing quality, source, schema, or release
authority. B4-D4 removes the undefined revocation-interface promise, freezes
current-epoch Stage 20 withdrawal, and defines the B5 current-manifest
recognition boundary without adding a persistent schema. B4-A and B4-B
implemented Stage 19 and the private Stage 20 handoff while the map remained
`1000`; their separately reviewed declaration changed it to the current
`1110`, which still does not activate public/direct structured production.

B5 is split into independently frozen and reviewed slices. B5-D1A freezes the
structured Stage 21 deterministic archive authority. B5-D1B-R1 freezes the
structured Stage 22 export and Stage 23 citation-verification authority.
B5-D1D normalizes the trusted-local filesystem boundary across Stages 17-23
without changing the frozen Stage 22 manifest v2, nested `code/` tree, Stage
23 provider/outcome contracts, or capability. B5-D1E freezes the exact
Stage 23 metadata-provider wires, identity normalization, bounded projections,
private relevance request/response transport, four-state nested relevance
schema, and system-failure boundary without changing report schema v2's 13
roots, manifest v2's 26 roots, the outcome matrix, or capability. Separate
Stage 21-23 implementation slices must keep `1110`. B5-D2 or later
separately freezes and implements Stage 24/25, independent reconstruction,
`pipeline_validation`, `research_release`, `release_check`, release gates, and
full release integration; every implementation and pre-activation commit
keeps `1110`. Only a separately named, separately reviewed,
implementation-free global activation declaration may change exact `1110` to
exact `1111`.

## 21. Adversarial test matrix

| Area | Mutation | Required result |
|---|---|---|
| Generation | mix Stage 17 facts/registry/manifest with a Stage 19 generation | reject before provider or publication |
| Stage 18 | after Snapshot A, mutate either review file or synchronously mutate both | source fixpoint rejects and cleanup runs |
| Stage 18 | before Snapshot A, replace both files and correctly rebuild a strict valid report | no false provenance claim: accept only as bounded advisory; subset/mandatory/ID-only replay still prevents scientific-authority escalation |
| Provider | return prose, explanation, section, schema, generation, or hash | reject response; no retry or repair |
| Selection | unknown, missing, duplicate, cross-section, or minted ID; or `ordered_claim_ids` is not an exact permutation of selected IDs | reject |
| Selection | section objects are reordered, missing, or duplicated | reject |
| Selection | `ordered_claim_ids` is a legal exact permutation of selected IDs | accept and deterministically render that exact order |
| Selection | reintroduce registry ID absent from Stage 17 selection | reject |
| Mandatory | delete a mandatory claim | reject |
| Connector | inject connector argument or value other than `NONE` | reject |
| Connector | wrong connector count | reject |
| Forgery | synchronously edit sentence, sentence hash, fact ID, and claim ID | code-owned rebuild/byte replay rejects |
| Manifest union | Stage 17 source manifest is missing or non-null | Stage 17 reject |
| Manifest union | Stage 19 source manifest is null, missing, or wrong path | Stage 19 reject |
| Manifest | bind the wrong Stage 17 authority manifest | reject |
| Manifest | add a self path/hash/size field | exact-key rejection; no self-hash cycle |
| Stage 20 binding | forge `stage19_publication_mode` | reject before quality publication |
| Stage 20 binding | source paper points to generic `stage-19/paper_revised.md` | reject before model; zero calls |
| Stage 20 binding | publication binding points to legacy or sectional file | reject before model; zero calls |
| Stage 20 binding | synchronously recompute a forged Stage 19 manifest and its digest | independent Stage 19 expected rebuild rejects |
| Stage 20 schema | report v1, flags v3, and manifest v2 are synchronously forged | independent source, CFS, generation, fabrication, and outcome rebuild rejects |
| Stage 20 schema | wrong or boolean schema version | exact-type/version rejection |
| Stage 20 schema | duplicate, extra, or missing report/flags/manifest field | duplicate-safe exact-key rejection |
| Stage 20 outcome | `proceed` below threshold or `revise` at/above threshold | `FAILED`; authority-first invalidation; no replayable success manifest |
| Stage 20 outcome | `reject` with a high score | `FAILED`; no manifest or root signal |
| Stage 20 outcome | fabrication suspected or no real data despite high score | `FAILED`; deterministic fabrication block wins |
| Stage 20 forgery | source replay fails, then a green report is forged | zero model calls; forged report cannot authorize; authority-first invalidation |
| Stage 20 publication | report is published but flags or manifest publication fails | authority-first aggregate cleanup leaves empty tuples and no replayable manifest |
| Stage 20 publication | manifest is written, then source or source paper mutates | final replay/Snapshot B rejects and cleanup runs |
| Stage 20 publication | output mutates after final read | second capture or executor postcondition rejects |
| Degradation | passed outcome has any root signal or non-null manifest field | reject and cleanup |
| Degradation | degraded outcome has null, missing, extra, wrong-path, or wrong-hash signal | reject and cleanup |
| Degradation | root signal or temp is a symlink, directory, hardlink, or other collision | fail closed; do not follow a symlink target or recurse into a directory; no replayable manifest |
| Stage 20 parent | replace root parent, run, stage, or root-signal parent | no writes through replacement paths; operate through held original fds; no replayable manifest |
| Stage 20 executor | producer returns `DONE` with tuple mismatch | immediate postcondition converts to `FAILED` and cleans |
| Stage 20 executor | HITL/PRM/gate converts `DONE` to non-`DONE` | terminal cleanup; empty tuples |
| Stage 20 executor | mutate source, report, flags, signal, or manifest after immediate validation | terminal postcondition rejects and cleans |
| Stage 20 no LLM | `llm is None` with otherwise valid source | full source replay, zero calls, then `FAILED`; no default report |
| Source | mutate a Snapshot A source after provider calls | source fixpoint rejects and cleanup runs |
| Final namespace | mutate an output after final replay/read | Snapshot B or executor postcondition rejects |
| File type | canonical name is a hardlink, symlink, FIFO, device, socket, or directory | reject without following or overwriting |
| Collision | a Stage 20 temporary name already exists, or a formal target appears after admission invalidation | fail closed before writing; reserved entry may be unlinked, but never follow its target, recurse into a directory, or modify a non-reserved path |
| Stage 20 temporary | creating-fd identity differs from the temporary path before publication | reject publication and aggregate drift diagnostics; fixed-name cleanup is not identity-conditional |
| Stage 20 temporary | any `.tmp` remains after publication | reject; success namespace/root closure requires every temporary absent |
| Parent fault injection | at a documented phase boundary, replace run or stage parent during attempt | do not rediscover the replacement path; held-original cleanup only; strict replay rejects |
| Out-of-scope OS-isolation stress | malicious noncooperating same-UID syscall race, debugger/fd interference, or privileged/same-filesystem mount injection | not an application-level acceptance test; require separate OS isolation; never claim manifests cover it |
| Stored-state revival | restore detached crash residue or reintroduce reserved names before a new admission | no authority unless a newly captured current manifest passes complete strict semantic replay |
| Executor | return `DONE` with wrong tuple, refs, context, or disk bytes | immediate/terminal postcondition converts to `FAILED` and cleans |
| Stage 19 no LLM | empty strict ReviewLedger | zero calls; inherit selection; full rerender/replay/publication |
| Stage 19 no LLM | nonempty strict ReviewLedger | `FAILED` before staging/success publication; no generic fallback |
| Stage 19 transport | connection/timeout/retryable 429/5xx before response content | at most one identical second outbound attempt |
| Stage 19 transport | third request or changed retry fingerprint | reject/fail call-bound test |
| Stage 19 semantic | empty, truncated, malformed, duplicate/extra/missing fields, or invalid IDs | immediate `FAILED`; no retry/repair |
| Stage 20 transport | first connection/timeout/retryable 429/5xx before content | at most one identical retry for that semantic call |
| Stage 20 transport | changed retry fingerprint, fifth outbound attempt, or hidden fallback | `FAILED`; diagnostics only; cleanup |
| Stage 20 semantic | first empty/truncated/malformed/duplicate/extra/missing/wrong-type response | permit exactly one repair semantic call |
| Stage 20 semantic | second invalid response or third semantic call | `FAILED`; diagnostics only; cleanup |
| Capability | malformed or partial map | ordinary generic unchanged; direct rejects before I/O |
| Capability | exact `1000` or current `1110` | public/direct structured remains blocked |
| Test seam | forged, copied, expired, cross-run, or cross-epoch verified context | reject before provider/publication |
| Discriminator | domain-v2 marker exists but Stage 17/CFS/generation/replay is invalid | `FAILED`; never generic fallback |
| Downstream | current public/direct Stage 21, 22, or 23 is asked to consume structured upstream authority | reject; B5-D1A/B5-D1B-R1 are design-only and no structured parser/dispatch is implemented |
| Downstream | synchronously forge Stage 21/22 hashes around structured Stage 20 | independent reconstruction rejects; no B4 release authority |
| Downstream current manifest | current Stage 20 manifest is missing, non-regular, malformed, cannot complete strict replay, or changes identity during held-fd capture, replay, or source fixpoint | reject before opening any Stage 21+ path; no old downstream authority |
| Downstream generation | current Stage 20 generation or source identity differs from independently rebuilt current-run identity | reject before any Stage 21+ artifact read or write |
| Downstream binding | a Stage 21+ authority manifest binds a stale Stage 20 FileRef, generation, or source identity | reject that downstream authority before reading or writing any other downstream artifact |
| Downstream fallback | current Stage 20 admission fails, then a consumer tries Stage 17/19, a generic paper, stored downstream hash, config, caller state, or artifact presence | reject; none is a structured downstream authority oracle |
| Generic | stale structured-only Stage 19 names or any structured Stage 20 `.tmp` name exists while inactive | generic does not use them as dispatch authority or consume them |
| Generic | overlapping Stage 20 `quality_report.json`, `fabrication_flags.json`, or `quality_gate_manifest.json` is stale | unchanged generic entry cleanup may invalidate it; the name never discriminates structured dispatch |
| Generic | legal generic-v1 input | exact existing schemas, bytes, artifacts, and failure behavior |

## 22. B5-D1D acceptance and non-claims

B5-D1D is ready for a narrow docs-only commit only when:

- the independent filesystem/threat-model reviewer and
  authority/replay/lifecycle reviewer complete a read-only fixpoint;
- no P0 remains and any material safety disagreement returns `STOP`;
- every JSON fence parses with a duplicate-safe strict parser;
- Markdown fences are paired;
- paths, names, versions, counts, capability states, call bounds, lifecycle
  steps, Stage 21 handoff, compiler branches, citation routes, and
  passed/degraded/FAILED matrices are internally consistent;
- no Stage 17, Stage 19, Stage 20, Stage 21, Stage 22, or Stage 23 manifest
  example has a self-hash cycle;
- the task-added tracked diff contains only this document, and the three
  preexisting tracked plus two new B5-A2 file fingerprints remain byte-exact
  to the B5-D1D starting capture;
- `git diff --check` passes;
- all preexisting untracked files and directories remain untouched.

B5-D1D does not modify production code or tests, implement or resume Stage
21-23, define or implement Stage 24-25, reconstruct or integrate a release,
activate a structured production path, change the capability map, weaken
untrusted LLM/provider/caller/stored-artifact or release semantics, accept a
release, authorize commit/push, or authorize API, resume, or fresh F0 work.
It creates no reusable or serialized activation authority. Remaining work is
Stage 22-23 implementation and independent review under exact `1110`,
followed by B5-D2 schema/reconstruction/release adjudication. Separate
adjudication is required for a docs-only commit, B5-A2 continuation, B5-A3,
B5-D2, and eventual activation.
