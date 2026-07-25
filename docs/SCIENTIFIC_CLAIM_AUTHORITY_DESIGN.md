# Structured Scientific Claim Authority Design

Status: `ACCEPTED / FROZEN DESIGN / NOT IMPLEMENTED`

Scope: Batch B0 design authority for the future
`structured-scientific-claim-v1` capability.

This document freezes the minimum design for B1-B5. B0 does not implement the
schemas or activate the capability. In particular, the current manuscript
pipeline must not claim scientific-prose safety from regex blacklists,
negation windows, false-absence word lists, or an LLM's prose.

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
Stage 20 CFS build are known B1-B4 blockers. This design does not describe them
as already closed.

## 2. Capability admission and mixed generations

The capability is active only when all of the following are true:

- the canonical evidence capability is exact domain-evaluator v2;
- the CFS builder returns schema version 1;
- the runtime capability map explicitly enables
  `structured-scientific-claim-v1`;
- every Stage 17-20 input belongs to one generation binding.

Otherwise the existing `generic-v1` path is used without new fields or checks.
Partial activation is forbidden. A structured registry, selection, rendered
paper, or manifest from a different evidence/CFS generation MUST fail closed.
There is no conversion, fallback, or best-effort merge between generations.

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

## 9. Registry and selection artifacts

Future B1-B4 artifacts use manifest binding rather than implicit in-memory
state:

- `scientific_evidence_facts.json`
- `scientific_claim_registry.json`
- `scientific_claim_selection.json`
- `scientific_claim_authority_manifest.json`

The authority manifest binds:

- schema and policy versions;
- generation binding;
- canonical evidence path/hash;
- experiment contract path/hash;
- CFS schema/hash;
- each artifact path/hash;
- deterministic rendered paper path/hash;
- source Stage 17 or Stage 19 authority manifest path/hash.

The manifest is written last. It cannot be consumed until independent disk
replay succeeds.

## 10. Stage 17 publication lifecycle

When the capability is active, Stage 17 MUST:

1. acquire the existing writer/release boundary;
2. invalidate only Stage 17 owned canonical claim outputs;
3. capture and replay canonical evidence;
4. rebuild the CFS and generation binding;
5. build and validate facts and the closed claim registry;
6. request strict JSON selections only for the governed sections;
7. validate mandatory selection, exact permutation, and connectors;
8. render governed sections deterministically;
9. assemble the paper deterministically;
10. stage facts, registry, selection, paper, and supporting reports;
11. independently reopen and replay the complete staged namespace;
12. publish non-manifest artifacts;
13. write the authority manifest last;
14. reopen and replay the final namespace before returning success.

The LLM must not return free prose for Abstract, Results, Discussion,
Limitations, or Conclusion. Other sections retain only the behavior explicitly
allowed by the selected capability design.

On any error, canonical paper, registry, selection, and authority manifest are
removed. Invalid paper/selection and diagnostics may be retained under
explicitly non-authoritative names. A failure must not leave a consumable
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
publication replay, and release-audit rebuild are reusable plumbing. In B0
they are not a scientific-safety proof.

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

Tests MUST compare exact dictionaries/bytes/hashes where practical, not merely
semantic success.

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
| generic-v1 | capability inactive | exact legacy schema/bytes/hash behavior |

## 17. B0 acceptance and non-claims

B0 is ready for a narrow commit only when:

- this design is independently reviewed;
- the old scientific regex oracle and its false invariants are absent from the
  tracked diff;
- CFS rebuild/hash comparison/threading/release-audit plumbing still passes
  targeted tests;
- `git diff --check` passes;
- independent verifier P0/P1 findings are zero for B0 scope.

B0 does not claim that structured claim authority exists, that Stage 19 is
already ID-only, that Stage 20 already replays without an LLM, or that a release
run is accepted.
