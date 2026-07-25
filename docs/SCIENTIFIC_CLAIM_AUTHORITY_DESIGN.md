# Structured Scientific Claim Authority Design

Status: `B3-D0 / FROZEN SCHEMAS / NOT ACTIVATED`

Scope: Batch B3-D0 docs-only authority for the future Stage 17 publication of
the `structured-scientific-claim-v1` capability.

The B1-B2 record, construction, and rendering primitives exist, but B3-D0 does
not implement Stage 17 publication, change a runtime capability, or activate
the structured path. In particular, the current manuscript pipeline must not
claim scientific-prose safety from regex blacklists, negation windows,
false-absence word lists, or an LLM's prose.

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

The current non-sectional Stage 19 prose path and the current conditional
Stage 20 CFS build are known B3-B4 blockers. This design does not describe them
as already closed.

## 2. Structured capability admission and mixed generations

The existing `CANONICAL_EVIDENCE_CAPABILITIES` map is frozen independently.
B3 MUST NOT add, remove, or rename a key in that map and MUST NOT route a
partial structured migration through its guard.

Structured admission has code-owned schema version `1` and this separate exact
four-key map. At B3-D0 and before B3 implementation it is exactly:

```json
{
  "stage17_publication": 0,
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

| Strict canonical input result | Code-owned structured map | Ordinary dispatch | Direct structured entry or replay |
|---|---|---|---|
| Strict replay succeeds and confirms a recognized, legal non-domain-v2 generation | any complete, partial, or invalid structured map | unchanged `generic-v1` | not eligible; reject |
| Strict replay succeeds and confirms exact domain-v2, CFS-v1, and one generation | exact map, all four true integer `1` | structured path | continue |
| Strict replay succeeds and confirms exact domain-v2, CFS-v1, and one generation | any true integer `0`, or missing, unknown, or wrong-typed map value | unchanged `generic-v1`; structured remains inactive | reject before direct-entry I/O |
| A domain-v2 discriminator is present, but its evidence, CFS schema, generation binding, or required replay is invalid | any map state | `FAILED`; never generic fallback | `FAILED` |
| Canonical evidence cannot be strictly captured and replayed | any map state | `FAILED`; never generic fallback | `FAILED` |
| Persisted snapshot, artifact presence, caller, config, environment, or LLM claims activation | code-owned map does not independently authorize it | declaration is ignored and cannot select a path | reject it as authority |

`Legal non-domain-v2` means strict replay succeeded and the code-owned
discriminator is either absent where the canonical schema permits absence or
names a recognized non-domain-v2 generation. An unknown, malformed, or
inconsistent discriminator is invalid; it is not evidence for generic
fallback.

The ordinary Stage 17 dispatcher first inspects only the code-owned map, then
reuses the same canonical input capture and strict replay that Stage 17 already
requires. The captured result is passed to the selected implementation; it is
not reopened through a new live path. This common capture is the sole
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

A direct structured entry MUST call a no-caller-map complete-capability guard
before acquiring a lock or performing any filesystem, provider, or output
I/O. Evidence domain, CFS schema, and generation prerequisites are then
derived through held-fd source capture and replay; they MUST fail before any
provider call or creation or publication of a new output. Once structured
entry begins, any error returns `FAILED`; it MUST NOT fall back to
`generic-v1`.

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
| B3-D0 and immediately before B3 implementation | `{"stage17_publication":0,"stage19_revision":0,"stage20_replay":0,"stage24_and_release_integration":0}` | docs-only freeze; no activation |
| Every B3 implementation commit | `{"stage17_publication":0,"stage19_revision":0,"stage20_replay":0,"stage24_and_release_integration":0}` | implementation only; map change forbidden |
| Separately approved B3 declaration commit | `{"stage17_publication":1,"stage19_revision":0,"stage20_replay":0,"stage24_and_release_integration":0}` | only `stage17_publication: 0 -> 1` |
| Every B4 implementation commit | `{"stage17_publication":1,"stage19_revision":0,"stage20_replay":0,"stage24_and_release_integration":0}` | implementation only; map change forbidden |
| Separately approved B4 declaration commit | `{"stage17_publication":1,"stage19_revision":1,"stage20_replay":1,"stage24_and_release_integration":0}` | only B4-owned `stage19_revision` and `stage20_replay: 0 -> 1` |
| Every B5 implementation and pre-activation approval commit | `{"stage17_publication":1,"stage19_revision":1,"stage20_replay":1,"stage24_and_release_integration":0}` | implementation/evidence only; final component remains `0` |
| Separately named and reviewed global activation commit | `{"stage17_publication":1,"stage19_revision":1,"stage20_replay":1,"stage24_and_release_integration":1}` | only `stage24_and_release_integration: 0 -> 1`; activates the full structured path |

The last transition MUST be a commit explicitly named as the global activation
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
is rejected. A future Stage 19 manifest uses the same envelope: it sets
`publication_stage_id` to `stage19` and requires
`source_authority_manifest` to be an exact `FileRef` to the Stage 17 manifest;
null or missing is then rejected. This present-null versus exact-`FileRef`
union prevents a missing/null fork while preserving a compatible origin
chain.

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

## 11. Stage 19 revision lifecycle

Stage 19 captures the complete Stage 17 publication and independently replays
its evidence, CFS, facts, registry, selections, sentences, and manifest.

For governed sections, the provider may return only the strict selection JSON.
The new selection may keep, delete, and reorder existing claim IDs subject to
mandatory-claim rules. It may not:

- edit `rendered_sentence`;
- change renderer/template/slot data;
- mint a fact or claim;
- add connector parameters;
- return prose;
- combine records from another generation.

Stage 19 rerenders from the unchanged registry, publishes a newly bound
selection and deterministic paper, and writes its manifest last. The manifest
must explicitly bind the CFS hash, generation binding, Stage 17 registry hash,
Stage 19 selection hash, and rendered paper hash.

The current CFS rebuild, caller presence/hash comparison, parameter threading,
publication replay, and release-audit rebuild are reusable plumbing. In
B3-D0 they are not a scientific-safety proof.

On failure, Stage 19 runs its owned-output cleanup and leaves no revised paper
or canonical manifest. Diagnostics remain non-authoritative.

## 12. Stage 20 independent replay

Stage 20 MUST perform the following before any quality-model call and even when
`llm is None`:

1. capture the sole Stage 19 publication;
2. independently reload canonical evidence;
3. rebuild the CFS and generation binding;
4. rebuild the facts and claim registry from code;
5. compare their canonical bytes/hashes with the Stage 17/19 manifests;
6. strictly parse and validate the Stage 19 selection;
7. rerender every sentence and the paper;
8. require byte equality with the Stage 19 paper;
9. reject namespace or generation changes during replay.

The quality model may assess a replayed paper, but its report cannot authorize
facts, claims, sentences, connectors, or publication. Stage 20's manifest binds
the CFS, registry, selection, generation, and paper hashes and is written last.

## 13. Stage 24 reuse boundary

The structured path may reuse Stage 24's:

- safe relative-path rules;
- source byte capture and SHA-256;
- byte offsets and exact substring replay;
- identity payloads that exclude their own IDs;
- canonical JSON and Decimal discipline;
- staged namespace, manifest-last publication, final fixpoint, and cleanup.

It MUST NOT use comparative or negation regex, lexical obligation extraction,
or open-English grammar as an oracle for structured scientific claim meaning.
Those mechanisms may remain only where required for unchanged `generic-v1`
compatibility.

## 14. `generic-v1` compatibility

When the structured capability is inactive:

- `build_canonical_fact_sheet` keeps its existing `None` behavior;
- no structured claim artifacts are required or emitted;
- no new validation check code appears;
- existing manifest schemas and canonical bytes remain unchanged;
- Stage 17/19/20/24 dispatch and failure behavior remain unchanged;
- release reconstruction accepts the same historical artifacts as before.
- stale structured files do not activate, block, alter, or get consumed by the
  generic path;
- the generic path does not enumerate, parse, require, publish, or clean the
  structured names.

Tests MUST compare exact dictionaries/bytes/hashes where practical, not merely
semantic success. They MUST also prove that partial and malformed structured
maps cause no structured function call, structured file open, cleanup,
provider call, or output change on the generic path. The existing
canonical-evidence map and every existing caller remain byte-for-byte and
behaviorally unchanged.

## 15. B1-B5 commit split

Each commit is narrow and independently reviewable.

### B1 — contracts and identity

- strict `EvidenceFact` and `ScientificClaimRecord` parsers;
- canonical Decimal/JSON enforcement;
- self-hash-excluding ID computation;
- generation/CFS/source binding;
- registry and selection schema parsers;
- no stage activation.

### B2 — deterministic construction and rendering

- code-built fact and claim registries;
- deterministic renderer and sentence hashes;
- strict selection validation;
- `NONE` connector only;
- adversarial unit tests;
- no Stage 17 publication switch.

### B3 — Stage 17 authority publication

- governed-section selection-only calls;
- deterministic paper assembly;
- manifest-last publication and cleanup;
- Stage 17 replay tests;
- explicit capability gate.

### B4 — Stage 19 and Stage 20 closure

- close all Stage 19 governed-section prose paths;
- ID-only revision and exact registry binding;
- Stage 20 unconditional independent rebuild/replay;
- CFS/registry/selection/paper manifest binding;
- mixed-generation and no-LLM tests.

### B5 — Stage 24 and release integration

- structured-claim Stage 24 mechanical replay path;
- no comparative lexical oracle on the structured path;
- release-audit/reconstruction integration;
- complete generic-v1 byte regression;
- fresh F0 remains a separately authorized acceptance step.

## 16. Adversarial test matrix

| Area | Mutation | Required result |
|---|---|---|
| Strict JSON | extra selection key or prose wrapper | reject |
| Strict JSON | duplicate JSON key | reject |
| Connector | value other than `NONE` | reject |
| Connector | argument object/string channel | reject |
| Connector | wrong connector count | reject |
| Registry | selected unknown ID | reject |
| Registry | selected valid ID from another section | reject |
| Registry | duplicate selected ID | reject |
| Mandatory | omit mandatory ID | reject |
| Ordering | missing, duplicate, or extra ordered ID | reject |
| Ordering | same set but wrong allowed order | accept and render that exact order |
| Fact ID | include `fact_id` in identity payload | reject/recompute mismatch |
| Claim ID | include or edit `claim_id` payload | reject/recompute mismatch |
| Decimal builder | typed, finite non-normalized `Decimal` | canonicalize before publication |
| Decimal parser | float, NaN, infinity, `-0`, exponent/trailing-zero alias | reject |
| Source | traversal, absolute path, symlink, wrong hash | reject |
| CFS | caller CFS differs from rebuilt CFS | reject before provider call |
| CFS | schema/hash changed after selection | reject |
| Generation | mix Stage 17 registry and Stage 19 evidence | reject |
| Renderer | unknown template or wrong slot arity | reject |
| Renderer | altered punctuation/space/Unicode bytes | sentence hash/replay failure |
| Renderer | persisted sentence edited with same facts | byte replay failure |
| Stage 17 | provider returns governed prose | reject, no canonical manifest |
| Stage 17 | failure after paper write, before manifest | cleanup canonical outputs |
| Stage 19 | provider mints claim or edits sentence | reject |
| Stage 19 | late namespace mutation | replay failure and cleanup |
| Stage 20 | `llm=None` | full independent rebuild/replay still required |
| Stage 20 | forged green quality report | cannot bypass replay |
| Stage 24 | comparative wording outside registry | no structured authority inferred |
| Manifest | manifest written before an output | fail manifest-last test |
| Manifest | CFS/registry/selection hash tamper | independent audit rejects |
| Manifest | facts/registry/selection/manifest synchronously recomputed | code-owned rebuild byte mismatch rejects |
| Manifest | missing Stage 17 source manifest field | reject; present JSON null is required |
| Capability | snapshot/config/caller self-reports all components active | no authority; reject direct entry |
| Capability | partial or wrong-typed map | generic unchanged; direct entry rejects before I/O |
| Dispatch | strict replay confirms legal non-domain-v2 | unchanged `generic-v1` |
| Dispatch | domain-v2 discriminator exists but CFS/schema/generation/replay is invalid | `FAILED`; never generic fallback |
| Dispatch | canonical evidence strict replay fails | `FAILED`; never generic fallback |
| Activation | implementation commit changes a capability component | reject commit scope |
| Activation | final component changes outside named global activation commit | reject commit scope |
| Capability | gate occurs after lock or cleanup | fail external-zero-write test |
| Namespace | collision or parent replacement | fail closed; no external write |
| Namespace | source or final output changes after replay | fail fixpoint and cleanup |
| Stale output | structured artifact exists while inactive | generic path does not read or consume it |
| generic-v1 | capability inactive | exact legacy schema/bytes/hash behavior |

## 17. B3-D0 acceptance and non-claims

B3-D0 is ready for a narrow docs-only commit only when:

- this design is independently reviewed;
- every JSON example parses with a strict duplicate-key parser;
- Markdown fences are paired;
- the tracked diff contains only this document;
- `git diff --check` passes;
- an original reviewer completes a read-only fixpoint with no open P0/P1.

B3-D0 does not claim that Stage 17 structured publication exists, that any
structured capability component is active, that Stage 19 is already ID-only,
that Stage 20 already replays without an LLM, or that a release run is
accepted. Implementation, commit, push, and fresh F0 authorization remain
separate decisions.
