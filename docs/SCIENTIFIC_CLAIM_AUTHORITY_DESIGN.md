# Structured Scientific Claim Authority Design

Status: `B4-D0 / STAGE 19-20 SCHEMAS FROZEN / NOT ACTIVATED`

Scope: Batch B4-D0 docs-only authority for future structured Stage 19 revision
and Stage 20 replay of the `structured-scientific-claim-v1` capability.

B3 Stage 17 implementation and its separate declaration are complete. The
code-owned structured capability is now exactly `1000`: Stage 17 publication
is declared, while Stage 19 revision, Stage 20 replay, and Stage 24/release
integration remain undeclared. B4-D0 freezes schemas and lifecycle only. It
does not implement Stage 19 or Stage 20, change the capability map, or activate
the structured production path.

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
| 7 | exact domain-v2, CFS-v1, one generation | future declared `1110` | unchanged `generic-v1` | incomplete capability before direct-entry I/O |
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

If the live run or stage parent is replaced, the transaction performs zero
reads, writes, or deletes against the replacement target. Cleanup may operate
only on the already-held detached original inode. It invalidates every
reachable commit point there so detached authority cannot revive if the
original parent is restored. Replacement-target behavior is
external-zero-write and external-zero-delete.

Cleanup failure is appended to the original error and never changes failure to
success. A malformed current manifest, paper, staging tree, or other artifact
cannot block independent invalidation of other owned names. Any provider,
replay, publication, final-capture, or executor-postcondition failure runs
manifest-first aggregate cleanup and returns `FAILED` with empty artifact
tuples.

## 18. Stage 20 handoff

Structured Stage 20 has exactly one source authority:
`stage-19/scientific_claim_authority_manifest.json` plus the exact six-name
Stage 19 namespace from Section 12. Stage 20 captures that manifest and
namespace through held descriptors, hashes the manifest exact bytes
externally, and treats the schema-v2 manifest as its
`source_authority_manifest`.

Before any quality-model call, and also when `llm is None`, Stage 20 MUST:

1. replay Stage 19 schema version `2` and the exact namespace;
2. follow the Stage 17 source manifest and independently rebuild evidence,
   CFS, generation, five facts, and six claims;
3. replay the Stage 18 dual-FileRef binding without granting review prose
   scientific authority;
4. validate the Stage 19 descendant selection;
5. independently rerender and splice the paper;
6. rebuild all Stage 19 closure reports and require exact bytes;
7. require source and namespace fixpoints.

Structured Stage 20 MUST NOT fall back to Stage 17 selection or paper,
`stage-19/paper_revised.md`, `revision_evidence_binding.json`, a sectional
manifest, live prose, or any generic-v1 alternative. Missing or invalid
Stage 19 authority is `FAILED`. B4-B may implement this frozen handoff but may
not invent another Stage 19 output schema.

The quality model may assess only the fully replayed paper. It cannot authorize
facts, claims, sentences, connectors, selections, or publication.

## 19. Generic and release boundary

`generic-v1` Stage 18, Stage 19, and Stage 20 schemas, bytes, artifact names,
provider behavior, cleanup, and failure behavior remain unchanged. At map
`1000`, malformed/partial states, and future `1110`, ordinary dispatch remains
generic after its mandatory common canonical replay.

Generic code does not enumerate, parse, require, publish, consume, block on, or
clean any structured-only `scientific_claim_*` Stage 19 name or structured
staging entry. Stale structured artifacts cannot activate the structured path
and cannot affect generic bytes or results. Artifact presence, caller, config,
environment, or persisted snapshot is never a discriminator.

Once a valid `1111` domain-v2 dispatch enters the structured path, every later
error is `FAILED`; generic fallback is forbidden.

Stage 24, Stages 21-25, E9, `release_check`, release gates, and fresh F0 are
outside B4-D0 and B4-A. B4-D0 changes no release authority. Fresh F0 requires
separate explicit authorization.

## 20. B1-B5 milestone split

B1 and B2 supplied strict records, identities, deterministic construction,
rendering, and selection. B3 supplied structured Stage 17 publication and the
separate `1000` declaration.

B4-D0 freezes this Stage 19/20 design only. Every B4 implementation commit
keeps map `1000`. A separately reviewed B4 declaration may change it to
`1110`, which still does not activate public/direct structured production.
B4-A implements Stage 19 against this frozen schema; B4-B implements the Stage
20 handoff and may not revise Stage 19 schema.

B5 owns Stage 24 and release integration. Only a separately named, reviewed
`1111` declaration activates the full structured path.

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
| Source | mutate a Snapshot A source after provider calls | source fixpoint rejects and cleanup runs |
| Final namespace | mutate an output after final replay/read | Snapshot B or executor postcondition rejects |
| File type | canonical name is a hardlink, symlink, FIFO, device, socket, or directory | reject without following or overwriting |
| Collision | staging or final name already exists | fail closed; aggregate cleanup owned names |
| Staging | staging remains after publication | reject and cleanup |
| Parent | replace run or stage parent during attempt | external-zero-write/zero-delete; clean detached original |
| Revival | restore detached original run after failure | no manifest or revised-paper authority can revive |
| Executor | return `DONE` with wrong tuple, refs, context, or disk bytes | immediate/terminal postcondition converts to `FAILED` and cleans |
| No LLM | empty strict ReviewLedger | zero calls; inherit selection; full rerender/replay/publication |
| No LLM | nonempty strict ReviewLedger | `FAILED` before staging/success publication; no generic fallback |
| Transport | connection/timeout/retryable 429/5xx before response content | at most one identical second outbound attempt |
| Transport | third request or changed retry fingerprint | reject/fail call-bound test |
| Semantic | empty, truncated, malformed, duplicate/extra/missing fields, or invalid IDs | immediate `FAILED`; no retry/repair |
| Capability | malformed or partial map | ordinary generic unchanged; direct rejects before I/O |
| Capability | exact `1000` or future `1110` | public/direct structured remains blocked |
| Test seam | forged, copied, expired, cross-run, or cross-epoch verified context | reject before provider/publication |
| Discriminator | domain-v2 marker exists but Stage 17/CFS/generation/replay is invalid | `FAILED`; never generic fallback |
| Generic | stale structured files exist while inactive | generic does not read, block on, consume, or clean them |
| Generic | legal generic-v1 input | exact existing schemas, bytes, artifacts, and failure behavior |

## 22. B4-D0 acceptance and non-claims

B4-D0 is ready for a narrow docs-only commit only when:

- the original schema and lifecycle reviewers complete a read-only fixpoint;
- no P0 or P1 remains and any new material disagreement returns `STOP`;
- every JSON fence parses with a duplicate-safe strict parser;
- Markdown fences are paired;
- paths, names, versions, counts, capability states, call bounds, lifecycle
  steps, and Stage 20 handoff are internally consistent;
- the Stage 19 manifest has no self-hash cycle;
- tracked diff contains only this document and production/tests have zero diff;
- `git diff --check` passes;
- the original nine untracked groups remain untouched.

B4-D0 does not implement Stage 19 or Stage 20, activate a structured
production path, change the capability map, authorize B4-A/B4-B, accept a
release, authorize commit/push, or authorize API, resume, or fresh F0 work.
