# Canonical Experiment Evidence Contract

Status: review draft. This document defines the authority, provenance,
selection, invalidation, replay, and migration contract for experiment evidence
produced by Stages 10-14 and consumed by Stages 15-24 and release audit. It does
not authorize implementation, change an existing gate, declare an existing run
release-ready, or permit backfilling provenance into a historical run.

## 1. Objective

The pipeline currently has several files that can be interpreted as "best"
experiment evidence:

- `stage-12/runs/*.json`;
- `stage-14/experiment_summary.json`;
- `stage-14_v*/experiment_summary.json`;
- root `experiment_summary_best.json`;
- root `analysis_best.md`;
- direct and versioned Stage 14 analysis and chart artifacts.

Different consumers select from these files with different path orderings and
glob rules. A file being newer, copied to the run root, or named `best` does not
establish a replayable binding to the current Stage 12/13 result generation,
contract, configuration, sealed input, or analysis attempt.

This contract establishes one run-local authority:

```text
canonical_experiment_evidence.json
```

Every downstream experiment-fact consumer must load and replay this manifest
through one shared accessor. Root `experiment_summary_best.json` and
`analysis_best.md` remain optional derived compatibility copies. They are never
authority by themselves.

Under the trusted producer/runner assumption, the contract must establish all
of the following replayable run-local byte bindings:

1. the selected Stage 14 candidate binds to the current canonical Stage 12
   baseline or selected Stage 13 refinement result set;
2. the candidate used the current canonical Stage 9 experiment contract and
   run-local configuration snapshot;
3. the primary metric and optimization direction were derived from validated
   policy rather than self-reported by an LLM or manifest;
4. selection among eligible Stage 14 candidates was deterministic;
5. the selected summary, analysis, and derived compatibility copies are
   byte/hash-bound to the same candidate;
6. rollback, pivot, rerun, and resume cannot silently reuse evidence from an
   older experimental generation;
7. all downstream consumers observe the same authority or fail closed.

## 2. Observed Failure

The mixed-source validation run
`runs/hwsec-e0-e9-validation-20260712` exposed a concrete authority split:

- the current Stage 12 `results.json` reported `detection_f1 = 0.0`;
- root `experiment_summary_best.json` retained an older value near `0.639877`;
- Stage 14 analysis described the older value as unsupported;
- Stage 17 experiment-fact closure preferred the root `best` artifact over the
  current direct Stage 12 result because its source selector used an
  existence-first path order;
- other consumers use different combinations of direct paths, versioned globs,
  and root compatibility files.

The resulting paper path could be internally hash-consistent at each local
step while still mixing incompatible experimental generations. This is a
canonical-authority failure, not a justification to weaken Stage 17 fact
closure, Stage 19 sectional validation, Stage 24 provenance, or release gates.

## 3. Scope And Non-Goals

### 3.1 In scope

- Stage 10 sealed producer-input identity;
- Stage 12 canonical baseline result-set identity;
- Stage 13 canonical refinement result-set identity;
- Stage 14 candidate provenance;
- deterministic promotion among eligible Stage 14 candidates;
- root canonical manifest and compatibility-copy generation;
- invalidation on rollback, pivot, rerun, and resume;
- a shared read-only consumer accessor;
- migration of Stage 15-24 experiment-evidence consumers;
- independent release replay;
- adversarial tests for stale, mixed-generation, shadow, and partial-tamper
  artifacts.

### 3.2 Out of scope

- changing experiment semantics or metrics;
- weakening any scientific-quality, citation, claim, or release threshold;
- declaring root `experiment_summary_best.json` authoritative;
- selecting by file modification time;
- inventing producer-attempt identifiers that do not already have a stable,
  canonical definition;
- retroactively minting a canonical manifest for a historical run;
- proving integrity against an attacker who can coherently rewrite every
  source artifact and every hash-bearing manifest in the run;
- Stage 23 DOI/title verification changes, which are a separate bounded
  workstream described in Section 14.

### 3.3 Threat-model boundary

This contract protects against:

- stale root compatibility files;
- accidental mixing of direct and versioned stage artifacts;
- single-point or partial artifact modification;
- consumer-specific path ordering;
- missing provenance;
- incomplete rollback cleanup;
- deletion or insertion of Stage 12 result files;
- selection-policy drift;
- replay against a different contract or configuration snapshot.

SHA-256 bindings do not prove integrity against a party that can coherently
rewrite the complete artifact chain and recompute every hash. Stronger
protection requires an external append-only ledger, signature, transparency
log, or trusted run attestation. No implementation or release message may
describe this contract as providing that stronger guarantee.

## 4. Non-Negotiable Invariants

1. There is exactly one canonical experiment-evidence manifest per run.
2. No consumer may select experiment evidence using existence-first path lists,
   modification time, directory-name sorting, or independent glob logic.
3. `stage-14_v*` directories are candidate history, not implicit authority.
4. Root `experiment_summary_best.json` and `analysis_best.md` are derived
   compatibility copies, not selection inputs.
5. A Stage 14 candidate is eligible only if all of its declared sources can be
   replayed from current canonical files and exact hashes.
6. The canonical Stage 12 baseline and Stage 13 refinement result sets use
   explicit immutable evidence namespaces. Raw sandbox workspaces and
   diagnostic files are not experiment authority.
7. Every canonical result set is an exact, default-deny file set. Added,
   removed, renamed, symlinked, nested, or hash-mismatched evidence files make
   it invalid.
8. Stage 10 strict replay is a prerequisite, not an assumed existing property.
   It binds the canonical contract path/hash and exact sealed input file set.
9. Canonical evidence schema v1 supports only `sandbox` and `docker` experiment
   modes. `collider_agent` fails with
   `canonical_experiment_mode_unsupported` until an equivalent sealed-plan
   provenance schema is implemented.
10. The canonical Stage 9 contract is selected by the repository's single
   canonical contract selector. No local selector may be reimplemented.
11. Run configuration history is selected by the existing active-snapshot
    contract. Experiment-generation identity uses a versioned full semantic
    config hash, not the snapshot filename.
12. `claim_scope`, `dataset_origin`, primary metric, and optimization direction
   are derived from validated contract/config inputs and independently
   recomputed. They are not trusted because a manifest states them.
13. Promotion considers only eligible candidates and uses a versioned,
    deterministic comparison and tie-break policy.
14. Summary, analysis, tables, figure plan, charts, and auxiliary analysis files
    must come from one immutable Stage 14 candidate generation.
15. Candidate identity cannot depend on time, mtime, filesystem enumeration, or
    a producer-supplied ID. It is derived from a canonical identity payload.
16. Any Stage 12, Stage 13, or Stage 14 rerun invalidates the previous canonical
    selection before new downstream work can start.
17. Resume at Stage 15 or later must validate the canonical manifest before the
    requested stage executes.
18. Missing, malformed, stale, incomplete, or unsupported provenance is a hard
    stage/release failure in every claim scope. `pipeline_validation` does not
    permit fabricated or mixed-generation evidence.
19. No waiver may resolve a canonical experiment-evidence mismatch.
20. A runtime capability gate, enforced by both CLI and Python entrypoints,
    prevents Stage 12+ execution while migration components are incomplete or
    version-mismatched.
21. Stage 10, Stage 12, Stage 13, and Stage 14 must bind the same semantic
    configuration identity. An older sealed candidate cannot be executed under
    a semantically different active configuration even when the Stage 9
    contract bytes remain unchanged.

## 5. Authoritative Artifact Model

### 5.1 Stage 10 producer-input prerequisite

Before C0 schemas are implemented, the Stage 10 sealed-candidate schema and
loader must be upgraded. The new loader must:

- reject duplicate JSON keys and unknown or missing top-level fields;
- require `contract_path` to equal the result of the canonical Stage 9 selector,
  not the hard-coded direct path;
- bind the exact canonical contract path and SHA-256;
- bind the exact run-local producer config snapshot and its full semantic hash;
- replay the exact flat selected-candidate file set, per-file hashes, ownership,
  scaffold hash, entry point, and default-deny directory rules;
- reject symlinks, unsafe paths, malformed hashes, and unmanifested files;
- parse its own producer output before Stage 10 reports DONE.

The upgraded exact top-level schema is:

```json
{
  "schema_version": 2,
  "seal_policy_version": 1,
  "producer_input_type": "sealed_python_candidate",
  "contract_path": "stage-09/experiment_contract.yaml",
  "contract_sha256": "<sha256>",
  "run_config_path": "config.yaml",
  "run_config_sha256": "<sha256>",
  "config_semantic_policy_version": 1,
  "config_semantic_sha256": "<sha256>",
  "scaffold_sha256": "<sha256>",
  "entry_point": "main.py",
  "files": {},
  "scaffold_files": {},
  "plugin_files": {}
}
```

The three file maps use exact filename keys and strict metadata objects.
`files` is the complete set; scaffold/plugin ownership sets are disjoint and
their union equals `files`. A timestamp, generated label, or provider response
may exist only in a separate diagnostic artifact and is not part of seal
identity. The Stage 10 loader validates that the recorded producer snapshot
still exists, has the recorded exact-file hash, resolves through run-local
config history, and recomputes the recorded semantic hash. Stage 12 requires
its active semantic hash to equal the Stage 10 value before executing code.

Schema v1 canonical experiment evidence accepts only `sandbox` and `docker`,
which execute this sealed Python candidate. `collider_agent` currently bypasses
that boundary and therefore cannot publish a Stage 12 canonical result set.
Supporting it later requires a separate strict sealed-plan manifest that binds
the plan, contract, model/provider policy, generated execution input, and result
namespace. It must not be added as a permissive optional branch.

### 5.2 Run configuration identity

Every producer manifest records:

```json
{
  "run_config_path": "config.resumed-20260713-120000.yaml",
  "run_config_sha256": "<exact-file-sha256>",
  "config_semantic_policy_version": 1,
  "config_semantic_sha256": "<canonical-json-sha256>"
}
```

The source path/hash identifies the exact snapshot used by the producer and is
validated through `active_config_snapshot.json`, history, and checkpoint
bindings. The semantic hash is computed from the complete strictly parsed
`RCConfig` mapping serialized as canonical JSON. Version 1 intentionally uses
the full config rather than an incomplete hand-selected experiment projection.
This is conservative: an unrelated semantic config change may require evidence
rebuild, but an experiment-affecting field cannot be omitted accidentally.

A resume snapshot with a different path but the same semantic hash does not
create a new experiment generation. The original producer snapshot must remain
present and hash-valid, and the active snapshot must have the same semantic
hash. Stage 10, Stage 12, Stage 13, every Stage 14 candidate, and the canonical
selection manifest all record and must agree on the same
`config_semantic_policy_version` and `config_semantic_sha256`. Any mismatch
invalidates the Stage 10 seal itself and every downstream evidence object; it
cannot be repaired by pairing an old seal hash with a new config hash. A future
narrower experiment-only projection requires a policy-version bump and an
exhaustive field-ownership review.

### 5.3 Stage 12 baseline result set

Stage 12 writes metric evidence only under:

```text
stage-12/evidence-v1/
```

The central execution controller, not the Stage 12 producer, owns the
append-only invocation journal at:

```text
stage-12/execution_invocation_journal.jsonl
```

For `sandbox` and `docker`, schema v1 permits this exact flat grammar:

```text
results.json
run-1.json
```

Schema v1 deliberately models the current implementation as exactly one
sandbox/docker invocation per successful Stage 12 generation. It does not
perform execution retries inside that generation. Before any sandbox process
starts, Stage 12 must acquire one invocation lease from the central execution
controller. The controller generates the opaque invocation token, appends the
`started` event, and rejects a second acquire before another sandbox call can
begin. Stage 12 cannot create, truncate, rewrite, or delete this journal.
Repository guards reject direct sandbox `run`/`run_project` calls from the
canonical Stage 12 path outside the lease wrapper. `run-1.json` is the immutable
per-invocation record and `results.json` is the deterministic aggregate derived
from it. A failed, partial, timed-out, or otherwise incomplete invocation makes
Stage 12 FAILED and publishes no `experiment_result_set.json`. A later pipeline
rerun is a new generation after canonical invalidation; its failed predecessor
may remain only in a diagnostic attempt history outside the canonical evidence
namespace. Supporting multiple invocations or retry-after-failure requires a
new result-set policy version with a predeclared execution plan and immutable
attempt ledger. Raw `runs/sandbox/`, workspaces, stdout/stderr captures,
dependency logs, and temporary files remain diagnostic and cannot be metric
sources.

The journal contains exactly two strict JSONL records for a successful v1
generation, with duplicate-key rejection and no blank lines:

```jsonl
{"schema_version":1,"event":"started","ordinal":1,"invocation_token":"<controller-generated-64-hex-token>","generation_binding_sha256":"<sha256>","experiment_contract_sha256":"<sha256>","sealed_candidate_manifest_sha256":"<sha256>","config_semantic_sha256":"<sha256>"}
{"schema_version":1,"event":"terminal","ordinal":1,"invocation_token":"<same-token>","status":"completed","result_path":"stage-12/evidence-v1/run-1.json","result_sha256":"<sha256>","failure_code":null}
```

`generation_binding_sha256` is recomputed from canonical JSON containing the
contract, Stage 10 seal, config semantic identity, mode, evaluator schema, and
single-invocation policy version. The token is routing/correlation data generated
by the controller, not scientific identity or a selection input. The controller
appends `completed` only after the invocation, domain-owned validation, and
canonical `run-1.json` write all succeed. Any earlier failure or incomplete
lease appends a terminal event with status `failed`, null result path/hash, and
a nonempty versioned failure code, after which no result-set manifest may be
published. Crash after `started` leaves an unterminated journal and also cannot
publish. The manifest is written only after the completed terminal event and
binds the final journal hash.

After successful execution Stage 12 publishes:

```text
stage-12/experiment_result_set.json
```

Its strict schema includes:

```json
{
  "schema_version": 1,
  "result_set_policy_version": 1,
  "result_set_type": "stage12_baseline",
  "experiment_mode": "sandbox",
  "experiment_contract_path": "stage-09/experiment_contract.yaml",
  "experiment_contract_sha256": "<sha256>",
  "sealed_candidate_manifest_path": "stage-10/selected_candidate_manifest.json",
  "sealed_candidate_manifest_sha256": "<sha256>",
  "run_config_path": "config.yaml",
  "run_config_sha256": "<sha256>",
  "config_semantic_policy_version": 1,
  "config_semantic_sha256": "<sha256>",
  "claim_scope": "pipeline_validation",
  "dataset_origin": "synthetic",
  "evaluator_schema": "hpc_anomaly_detection_v1",
  "invocation_journal": {
    "path": "stage-12/execution_invocation_journal.jsonl",
    "sha256": "<sha256>"
  },
  "execution_statuses": [
    {
      "ordinal": 1,
      "status": "completed",
      "result_path": "stage-12/evidence-v1/run-1.json",
      "failure_code": null
    }
  ],
  "evidence_files": [
    {"path": "stage-12/evidence-v1/results.json", "sha256": "<sha256>"},
    {"path": "stage-12/evidence-v1/run-1.json", "sha256": "<sha256>"}
  ]
}
```

The actual contract and config paths come from canonical selectors. The
producer/replay loader enforces exact keys, duplicate-key rejection, canonical
paths, valid hashes, safe regular files, exact evidence-directory closure,
finite numeric payloads, and full contract/config/sealed-input replay. Different
paths may legitimately have identical hashes; only duplicate paths are
forbidden. Its semantic policy version/hash must equal the values replayed from
the Stage 10 seal; comparing each independently with an active config is not
sufficient.

Under policy version 1, `execution_statuses` is exactly one entry with ordinal
`1`, status `completed`, canonical path `stage-12/evidence-v1/run-1.json`, and
null failure code. The result-set loader independently replays the controller
journal, requires exactly the matching started/completed pair, recomputes its
generation binding, and cross-checks token, ordinal, result path/hash, and
terminal status. A failed status is invalid in a successfully published v1
result set. The controller journal plus one-acquire enforcement is the
independent expected-attempt authority; the producer's status array alone is
not proof. `results.json` is aggregate authority and does not correspond to an
execution ordinal. `run-1.json` contributes all metric observations and must
reaggregate exactly to `results.json`.

Canonical `run-1.json` is the normalized per-invocation payload:

```json
{
  "schema_version": 1,
  "invocation_policy_version": 1,
  "ordinal": 1,
  "status": "completed",
  "evaluator_schema": "hpc_anomaly_detection_v1",
  "metric_observations": {
    "detection_f1": [0.5]
  },
  "structured_results": {
    "schema_version": 1,
    "claim_scope": "pipeline_validation",
    "dataset_origin": "synthetic",
    "dataset_name": "synthetic_pipeline_validation_v1",
    "primary_metric": {"key": "detection_f1", "value": 0.5, "direction": "maximize"},
    "metrics": {
      "accuracy": 0.5, "detection_f1": 0.5, "precision": 0.5,
      "tpr": 0.5, "tnr": 0.5, "fpr": 0.5, "latency_ms": 1.0
    },
    "seeds": [42, 123, 256],
    "conditions": ["DetectorPlugin"],
    "per_seed": [
      {"seed": 42, "metrics": {"accuracy": 0.5, "detection_f1": 0.5, "precision": 0.5, "tpr": 0.5, "tnr": 0.5, "fpr": 0.5, "latency_ms": 1.0}},
      {"seed": 123, "metrics": {"accuracy": 0.5, "detection_f1": 0.5, "precision": 0.5, "tpr": 0.5, "tnr": 0.5, "fpr": 0.5, "latency_ms": 1.0}},
      {"seed": 256, "metrics": {"accuracy": 0.5, "detection_f1": 0.5, "precision": 0.5, "tpr": 0.5, "tnr": 0.5, "fpr": 0.5, "latency_ms": 1.0}}
    ],
    "runtime_sec": 1.0,
    "evaluator_owner": "scaffold"
  }
}
```

Canonical `results.json` is the normalized deterministic aggregate, not a copy
of arbitrary sandbox JSON:

```json
{
  "schema_version": 1,
  "aggregation_policy_version": 1,
  "evaluator_schema": "hpc_anomaly_detection_v1",
  "source_ordinals": [1],
  "metric_observations": {
    "detection_f1": [0.5]
  },
  "structured_results": {
    "schema_version": 1,
    "claim_scope": "pipeline_validation",
    "dataset_origin": "synthetic",
    "dataset_name": "synthetic_pipeline_validation_v1",
    "primary_metric": {"key": "detection_f1", "value": 0.5, "direction": "maximize"},
    "metrics": {
      "accuracy": 0.5, "detection_f1": 0.5, "precision": 0.5,
      "tpr": 0.5, "tnr": 0.5, "fpr": 0.5, "latency_ms": 1.0
    },
    "seeds": [42, 123, 256],
    "conditions": ["DetectorPlugin"],
    "per_seed": [
      {"seed": 42, "metrics": {"accuracy": 0.5, "detection_f1": 0.5, "precision": 0.5, "tpr": 0.5, "tnr": 0.5, "fpr": 0.5, "latency_ms": 1.0}},
      {"seed": 123, "metrics": {"accuracy": 0.5, "detection_f1": 0.5, "precision": 0.5, "tpr": 0.5, "tnr": 0.5, "fpr": 0.5, "latency_ms": 1.0}},
      {"seed": 256, "metrics": {"accuracy": 0.5, "detection_f1": 0.5, "precision": 0.5, "tpr": 0.5, "tnr": 0.5, "fpr": 0.5, "latency_ms": 1.0}}
    ],
    "runtime_sec": 1.0,
    "evaluator_owner": "scaffold"
  }
}
```

Both payloads use exact schemas. Metric keys map to nonempty arrays of JSON
numbers. `source_ordinals` is exactly `[1]`, and reaggregating `run-1.json`
must reproduce every metric and structured result in `results.json`. Unknown
aliases remain under their exact keys; they never satisfy another metric by
substring.

`structured_results` is validated by the exact domain-owned evaluator schema
named by `evaluator_schema`; it is not a free-form escape hatch. The evaluator
schema identity is cross-checked against the sealed scaffold and contract.
For `hpc_anomaly_detection_v1`, the exact object shown above is authoritative:
the claim scope, dataset origin/name, primary metric key/direction, owner, seed
sequence, condition, aggregate metric key set, and per-seed metric key sets are
fixed. Every bounded metric is in `0..1`, latency and runtime are nonnegative,
aggregate metrics equal the deterministic mean of the three per-seed values,
and `primary_metric.value` equals the exact aggregate metric. The normalized
wrapper contains exactly the contract primary metric as a singleton observation
array and must equal the value derived from this validated object. Stage 12 and
every Stage 13 initial/repair execution use this same validator.

Stage 12 must stop choosing a sandbox result by mtime. The mode-specific runner
must return the exact result path for the current invocation, and Stage 12 must
copy only that validated output into the immutable evidence namespace.

### 5.4 Stage 13 refinement result set

Stage 13 may improve the baseline and therefore is part of the authority graph.
It writes immutable evidence under:

```text
stage-13/evidence-v1/iterations/iter-<positive-decimal-integer>/
```

Every iteration record binds:

- contiguous ordinal and canonical iteration ID;
- exact refined project file paths and hashes;
- code-validation report path/hash;
- initial sandbox result path/hash;
- a null runtime-repair field reserved for a future policy version;
- accepted status derived from deterministic runtime checks;
- the exact finite primary-metric observation parsed from the accepted initial
  execution result;
- no files outside the iteration namespace.

Stage 13 preserves the Stage 10 owner closure. Its project contains the same
flat logical filenames as the Stage 10 seal. Model-owned files may change;
scaffold-owned files retain their Stage 10 hashes and exact bytes. Under
`hpc_anomaly_detection_v1`, `main.py` must remain scaffold-owned and must still
equal `render_main_py(contract)` for the initial project. A scope without
an equivalent sealed scaffold evaluator is not eligible for this evaluator
schema and fails closed.

The initial project roles use exact paths under
`stage-13/evidence-v1/iterations/<iteration-id>/`: project files are a
canonically path-sorted, nonempty set under `project/` containing
`project/main.py`; the report is exactly `validation_report.json`; and the
execution result is exactly `initial_execution.json`. Refinement policy v1
permits no `runtime_repair/` paths. No path may serve more than one role within
an iteration.

Each validation report has this exact replayable shape:

```json
{
  "schema_version": 1,
  "validation_policy_version": 1,
  "project_files_sha256": "<sha256-of-canonical-path-sorted-file-refs>",
  "checks": {"python_syntax_valid": true}
}
```

The loader recomputes this complete report from the bound project bytes. Under
validation policy v1, every Python file is decoded according to its Python
encoding declaration and compiled in `exec` mode. The report is not an
acceptance oracle: acceptance is independently derived from the recomputed
syntax check plus one finite observation under the exact primary metric key in
the final execution result. The deterministic rejection codes
`python_syntax_invalid` and `primary_metric_missing_or_nonsingular` are reserved
for a future refinement policy version that retains rejected attempts. Under
refinement policy v1, stored `accepted`, `rejection_codes`, and metric fields
must be exactly `true`, `[]`, and the independently derived finite observation.
The manifest field `runtime_repair` is likewise reserved for a future policy
version, but the policy-v1 strict loader requires it to be exactly `null`.
Invalid model-response syntax, execution failure or timeout, a missing evaluator
result, or a missing/non-singular primary metric fails the entire Stage 13
generation; the producer publishes no refinement manifest or evidence
namespace. Support for retained rejected attempts or runtime repair requires a
policy-version bump plus an immutable attempt-publication grammar. A future
producer cannot add an unnecessary repair to replace an already accepted metric.

Stage 13 publishes `stage-13/refinement_result_set.json` with:

- baseline Stage 12 manifest path/hash and full replay;
- Stage 9 contract, Stage 10 sealed input, and config semantic bindings;
- complete ordered accepted iteration records under refinement policy v1;
- `refinement_log.json` path/hash as a diagnostic cross-check, not a selection
  oracle;
- deterministic selected result: either `stage12_baseline` or exactly one
  accepted Stage 13 iteration;
- selection policy version, exact metric key/direction, and independently
  recomputed comparison.

Its exact top-level shape is:

```json
{
  "schema_version": 1,
  "refinement_policy_version": 1,
  "result_set_type": "stage13_refinement",
  "baseline_manifest": {
    "path": "stage-12/experiment_result_set.json",
    "sha256": "<sha256>"
  },
  "experiment_contract_path": "stage-09/experiment_contract.yaml",
  "experiment_contract_sha256": "<sha256>",
  "sealed_candidate_manifest_path": "stage-10/selected_candidate_manifest.json",
  "sealed_candidate_manifest_sha256": "<sha256>",
  "run_config_path": "config.yaml",
  "run_config_sha256": "<sha256>",
  "config_semantic_policy_version": 1,
  "config_semantic_sha256": "<sha256>",
  "claim_scope": "pipeline_validation",
  "dataset_origin": "synthetic",
  "evaluator_schema": "hpc_anomaly_detection_v1",
  "primary_metric_key": "detection_f1",
  "optimization_direction": "maximize",
  "iterations": [
    {
      "ordinal": 1,
      "iteration_id": "iter-1",
      "project_files": [
        {"path": "stage-13/evidence-v1/iterations/iter-1/project/main.py", "sha256": "<sha256>"}
      ],
      "validation_report": {
        "path": "stage-13/evidence-v1/iterations/iter-1/validation_report.json",
        "sha256": "<sha256>"
      },
      "initial_execution": {
        "path": "stage-13/evidence-v1/iterations/iter-1/initial_execution.json",
        "sha256": "<sha256>"
      },
      "runtime_repair": null,
      "accepted": true,
      "rejection_codes": [],
      "primary_metric_observation": 0.8
    }
  ],
  "refinement_log": {"path": "stage-13/refinement_log.json", "sha256": "<sha256>"},
  "selected_result": {"type": "iteration", "iteration_id": "iter-1"}
}
```

Under refinement policy v1, `runtime_repair` is exactly null, `accepted` is
exactly true, `rejection_codes` is exactly an empty array, and
`primary_metric_observation` is a finite JSON number. A repaired-project object
is reserved for a future refinement policy version and is rejected by the v1
loader.
`selected_result.type` is exactly `baseline` or `iteration`; baseline requires a
null iteration ID and iteration requires one accepted canonical ID. Stored
accepted/status/metric fields are independently recomputed rather than trusted.

Removing a failed iteration, changing refined code, replacing a sandbox result,
or editing the selected iteration invalidates the complete refinement manifest.
Stage 14 never parses free-form refinement-log fields to discover metrics.

### 5.5 Selected experiment result union

Stage 14 accepts one tagged source:

```json
{
  "result_set_type": "stage12_baseline",
  "manifest_path": "stage-12/experiment_result_set.json",
  "manifest_sha256": "<sha256>"
}
```

or:

```json
{
  "result_set_type": "stage13_refinement",
  "manifest_path": "stage-13/refinement_result_set.json",
  "manifest_sha256": "<sha256>"
}
```

The type, path, and strict manifest schema must agree exactly. Stage 14 cannot
merge baseline and refinement metrics or scan arbitrary `stage-*/runs` paths.

### 5.6 Stage 14 immutable candidate and candidate ID

Stage 14 generates every candidate in a new temporary staging directory under
`stage-14/.candidate-staging-*`, on the same filesystem and parent publication
boundary as `evidence_candidates/`. Cross-device copy/delete fallback is
forbidden; an `EXDEV` rename failure is a hard publication failure. Stage 14
must remove or invalidate the prior direct success artifacts before work starts.
The owned candidate set includes summary, analysis, results table, figure plan,
charts, and every auxiliary analysis artifact that can reach a later consumer.
Summary, analysis, results table, and figure plan are mandatory; an empty
figure plan deterministically represents a candidate with no charts.

All references between candidate artifacts use normalized
candidate-root-relative logical paths such as `charts/main.png`. Identity-bound
artifacts must not contain a candidate ID, candidate identity digest, candidate
manifest digest, canonical root-manifest path, or final
`stage-14*/evidence_candidates/` prefix. Structured artifact loaders first
validate every path field as a normalized candidate-root-relative path and
recursively inspect every decoded string value for forbidden identity/path
tokens. JSON Unicode escaping therefore cannot bypass the check. After the
identity and candidate manifest are complete and all forbidden digest/path
tokens are therefore known, the producer also scans the raw bytes of every
identity-bound artifact for direct ASCII occurrences before strict replay or
publication. Markdown, LaTeX, and other non-structured producers do not receive
candidate identity or final-directory values as inputs and are raw-scanned as a
second defense.

`experiment_evidence_candidate.json` is the identity envelope. It is never an
entry in `identity_payload.artifacts` and therefore is not hashed into the ID it
records.

After all owned artifacts exist, Stage 14 builds this path-independent identity
payload:

```json
{
  "candidate_identity_policy_version": 1,
  "selected_result_type": "stage13_refinement",
  "selected_result_manifest_sha256": "<sha256>",
  "experiment_contract_sha256": "<sha256>",
  "config_semantic_sha256": "<sha256>",
  "primary_metric_key": "detection_f1",
  "optimization_direction": "maximize",
  "artifacts": [
    {"role": "analysis", "logical_name": "analysis.md", "sha256": "<sha256>"},
    {"role": "figure_plan", "logical_name": "figure_plan.json", "sha256": "<sha256>"},
    {"role": "results_table", "logical_name": "results_table.tex", "sha256": "<sha256>"},
    {"role": "summary", "logical_name": "experiment_summary.json", "sha256": "<sha256>"}
  ]
}
```

`artifacts` is sorted by `(role, logical_name)` and includes the complete owned
set. The payload contains no candidate ID, directory path, timestamp, mtime,
filesystem ordinal, or producer-supplied identity.

```text
candidate_identity_sha256 = sha256(canonical_json(identity_payload))
candidate_id = "cand-" + candidate_identity_sha256
candidate_directory = stage-14/evidence_candidates/<candidate_id>/
```

The full 64-hex digest is used; no truncated collision namespace is accepted.
Before leaving staging, the producer writes
`experiment_evidence_candidate.json`. It binds the candidate ID, full identity
payload/hash, selected result-set union, contract/sealed-input/config bindings,
derived policy, and exact candidate-root-relative artifact paths/hashes. The
producer then runs the complete strict replay against the staging root.

Only after replay succeeds may publication check the destination. If it exists,
every manifest and artifact byte must match staging; otherwise publication
fails. If it does not exist, the complete staging directory, including its
manifest, is atomically renamed into the candidate namespace. Rename is
followed by read-only replay. No file may be written, removed, or renamed inside
the candidate directory after publication.

Candidate acceptance requires recomputing the identity payload and expected
directory name plus validating the candidate manifest hash. The directory key
is deterministic routing data, not an independent authority or producer
assertion.

The primary metric is not read from the LLM-produced summary. It is recomputed
from the selected result set according to Section 6; the summary must mirror the
recomputed value at its exact schema path.

The candidate manifest's exact top-level shape is:

```json
{
  "schema_version": 1,
  "candidate_policy_version": 1,
  "candidate_id": "cand-<64-hex-sha256>",
  "identity_payload": {
    "candidate_identity_policy_version": 1,
    "selected_result_type": "stage13_refinement",
    "selected_result_manifest_sha256": "<sha256>",
    "experiment_contract_sha256": "<sha256>",
    "config_semantic_sha256": "<sha256>",
    "primary_metric_key": "detection_f1",
    "optimization_direction": "maximize",
    "artifacts": [
      {"role": "analysis", "logical_name": "analysis.md", "sha256": "<sha256>"},
      {"role": "figure_plan", "logical_name": "figure_plan.json", "sha256": "<sha256>"},
      {"role": "results_table", "logical_name": "results_table.tex", "sha256": "<sha256>"},
      {"role": "summary", "logical_name": "experiment_summary.json", "sha256": "<sha256>"}
    ]
  },
  "identity_payload_sha256": "<sha256>",
  "selected_result": {
    "result_set_type": "stage13_refinement",
    "manifest_path": "stage-13/refinement_result_set.json",
    "manifest_sha256": "<sha256>"
  },
  "experiment_contract_path": "stage-09/experiment_contract.yaml",
  "experiment_contract_sha256": "<sha256>",
  "sealed_candidate_manifest_path": "stage-10/selected_candidate_manifest.json",
  "sealed_candidate_manifest_sha256": "<sha256>",
  "run_config_path": "config.yaml",
  "run_config_sha256": "<sha256>",
  "config_semantic_policy_version": 1,
  "config_semantic_sha256": "<sha256>",
  "claim_scope": "pipeline_validation",
  "dataset_origin": "synthetic",
  "evaluator_schema": "hpc_anomaly_detection_v1",
  "primary_metric_key": "detection_f1",
  "optimization_direction": "maximize",
  "primary_metric_value": "0.8",
  "artifacts": [
    {
      "role": "analysis",
      "path": "analysis.md",
      "sha256": "<sha256>"
    },
    {
      "role": "figure_plan",
      "path": "figure_plan.json",
      "sha256": "<sha256>"
    },
    {
      "role": "results_table",
      "path": "results_table.tex",
      "sha256": "<sha256>"
    },
    {
      "role": "summary",
      "path": "experiment_summary.json",
      "sha256": "<sha256>"
    }
  ]
}
```

`primary_metric_value` is the canonical Decimal string derived from source
observations. It is comparison output, not an input oracle. The identity and
outer artifact arrays must describe the same complete logical artifact set.
The loader resolves every outer artifact path relative to the directory that
contains this manifest. Moving an unchanged complete candidate between
`stage-14` and `stage-14_vN` parents therefore does not change candidate bytes
or identity. The canonical selection manifest separately records the selected
candidate's current run-relative manifest path.

### 5.7 Canonical selection manifest

Promotion writes one root artifact atomically:

```text
canonical_experiment_evidence.json
```

The exact schema is:

```json
{
  "schema_version": 1,
  "selection_policy_version": 1,
  "selected_result": {
    "result_set_type": "stage13_refinement",
    "manifest_path": "stage-13/refinement_result_set.json",
    "manifest_sha256": "<sha256>"
  },
  "experiment_contract_path": "stage-09/experiment_contract.yaml",
  "experiment_contract_sha256": "<sha256>",
  "sealed_candidate_manifest_path": "stage-10/selected_candidate_manifest.json",
  "sealed_candidate_manifest_sha256": "<sha256>",
  "run_config_path": "config.yaml",
  "run_config_sha256": "<sha256>",
  "config_semantic_policy_version": 1,
  "config_semantic_sha256": "<sha256>",
  "claim_scope": "pipeline_validation",
  "dataset_origin": "synthetic",
  "evaluator_schema": "hpc_anomaly_detection_v1",
  "primary_metric": "detection_f1",
  "optimization_direction": "maximize",
  "selected_candidate": {
    "candidate_id": "cand-<64-hex-sha256>",
    "path": "stage-14/evidence_candidates/cand-<sha256>/experiment_evidence_candidate.json",
    "sha256": "<sha256>"
  },
  "selected_summary": {
    "source_path": "stage-14/evidence_candidates/cand-<sha256>/experiment_summary.json",
    "source_sha256": "<sha256>",
    "canonical_path": "experiment_summary_best.json",
    "canonical_sha256": "<sha256>"
  },
  "selected_analysis": {
    "source_path": "stage-14/evidence_candidates/cand-<sha256>/analysis.md",
    "source_sha256": "<sha256>",
    "canonical_path": "analysis_best.md",
    "canonical_sha256": "<sha256>"
  }
}
```

The canonical copy hash must equal the corresponding selected source hash, and
the bytes must be identical. A compatibility copy may be omitted only in a
future schema version that removes it from all consumers. Under schema v1 it is
required and verified.

The manifest contains no timestamp, mtime, free-form selection explanation, or
producer-supplied attempt ID. Diagnostics may be written separately but are not
authority.

## 6. Deterministic Selection Policy

### 6.1 Primary metric grammar

The canonical Stage 9 contract supplies one exact primary metric key and one
direction in `{maximize, minimize}`. Config must agree exactly. Selection uses
only:

```text
metric_observations[<exact-primary-metric-key>]
```

from the selected Stage 12 or accepted Stage 13 execution payload. It does not
accept substring, suffix, prefix, case-folded, mean-key, or LLM-proposed aliases.
For example, `detection_f1_mean` cannot satisfy `detection_f1`.

C0-C3 originally implement contract schema v1. The final authority model after
C4-U0 requires contract schema v2, which retains every v1 field and adds the
exact top-level `metric_units` and `metric_display_labels` mappings defined in
C4-U0. Schema v1 remains historical documentation only after that migration
and is rejected by every authority producer, accessor, consumer, and release
reconstructor.

JSON numbers are parsed with `Decimal`, rejecting booleans, NaN, infinity, and
non-number strings. Negative zero is normalized to Decimal zero. The candidate
score is the Decimal arithmetic mean of the nonempty observation array under a
local precision-50, `ROUND_HALF_EVEN` context owned by aggregation policy v1.
The evaluator scaffold serializes Decimal values directly as JSON numbers and
the authority loader uses `parse_float=Decimal`; neither path converts an
authority value or aggregate through binary float. Decimal mathematical equality defines a tie, so
`0.5`, `0.50`, and `5e-1` compare equally without rewriting source bytes.

Stage 14 summary must expose the same value at the exact path:

```text
metrics_summary[<exact-primary-metric-key>].mean
```

That field is checked against the independently computed Decimal value but is
never the selection oracle. Presence of both an exact and substring-like key is
legal; only the exact key participates.

### 6.2 Promotion algorithm

Promotion performs these steps in order:

1. select and validate the canonical Stage 9 contract;
2. replay the upgraded Stage 10 sealed-candidate manifest and exact file set;
3. resolve active config history, validate the producer snapshot, and compare
   full semantic config hashes;
4. replay Stage 12 baseline and, when present, the complete Stage 13 refinement
   manifest;
5. derive the selected experiment-result union without reading Stage 14;
6. enumerate only canonical
   `stage-14*/evidence_candidates/cand-<64-hex>/` directories;
7. reject symlinked, malformed, incomplete, mutable, or
   provenance-mismatched candidates;
8. parse each candidate manifest and recompute its identity payload, digest,
   directory name, and complete owned file set;
9. exclude well-formed candidates bound to a stale selected-result generation
   or stale common provenance; malformed, hash-invalid, symlinked, or colliding
   candidates still invalidate the collection;
10. recompute the current-generation candidate score using Section 6.1;
11. because every eligible candidate binds the same selected-result manifest,
   all eligible scores are equal; select by full `candidate_id` in Unicode
   code-point order;
12. under the publication lock, copy selected summary and analysis bytes to
    temporary compatibility files;
13. construct and strict-parse the expected canonical manifest;
14. invalidate the old canonical pointer, atomically replace compatibility
    copies, and atomically publish the new manifest as the final commit point;
15. immediately replay the published manifest through the same strict loader.

Candidate enumeration sorts normalized paths before parsing and deduplicates by
full candidate ID. The same candidate ID in multiple attempt directories is
allowed only when manifests and every bound byte are identical; otherwise it is
a collision error. Filesystem order, stage-version lexical order, and mtime must
never affect selection.

The canonical-manifest loader repeats this enumeration and selection from disk;
it never starts from `selected_candidate`. It validates every canonical
candidate directory, reconstructs the selected result from Stage 12 plus the
complete Stage 13 manifest when present, recomputes every eligible score, and
requires the stored candidate ID, manifest path, and manifest hash to equal the
independently selected winner. A malformed entry inside an
`evidence_candidates/` collection invalidates the collection rather than being
silently skipped.

If no eligible candidate exists, promotion fails. It must not fall back to an
older root `best` file, an unmanifested Stage 14 directory, direct Stage 12
results, or LLM prose.

## 7. Invalidation, Rollback, And Resume

### 7.1 Invalidation events

The canonical manifest and its root compatibility copies become invalid when
any of the following occurs:

- Stage 9 contract regeneration;
- Stage 12 execution or result-set regeneration;
- Stage 13 refinement that changes inputs to Stage 14;
- Stage 14 analysis regeneration;
- a pivot or rollback to Stage 14 or earlier;
- a new active configuration whose semantic hash differs from the producer
  semantic hash;
- mutation, addition, deletion, or replacement of any bound source.

An invalidated manifest must be removed or replaced by an explicit invalid
state before downstream execution. A stale manifest must not remain usable
while a producer is rebuilding evidence.

### 7.2 Rollback behavior

- Rollback to Stage 12 or earlier invalidates Stage 12/13 result-set manifests,
  every Stage 14 candidate selection, the canonical manifest, compatibility
  copies, and all downstream experiment-fact closure artifacts.
- Rollback to Stage 13 preserves Stage 12 only if the exact result-set manifest
  still replays; it invalidates the Stage 13 manifest, Stage 14 selection, and
  downstream closures.
- Rollback to Stage 14 invalidates the canonical selection and downstream
  closures. Stage 14 must publish a new candidate before promotion.
- Stage 15 decisions do not mutate experiment evidence directly. If a decision
  triggers rollback, the rollback rule above applies before the target stage
  executes.

Versioned directories are archival evidence of prior attempts. Immutable
candidate directories may remain on disk but do not become eligible unless
their complete provenance replays against the current selected result set,
contract, sealed input, and semantic configuration.

### 7.3 Resume preflight

Any resume beginning at Stage 15 or later must call the shared canonical
evidence loader before the first requested stage. Failure stops the pipeline
before any LLM call or output persistence.

Resume must not repair, infer, or regenerate provenance automatically. If the
manifest is absent or invalid, the user must resume from the earliest producer
needed to rebuild it:

- contract or semantic config mismatch: Stage 9;
- sealed producer-input mismatch: Stage 10;
- baseline result mismatch: Stage 12;
- refinement mismatch: Stage 13;
- candidate or analysis mismatch: Stage 14.

A new resume snapshot with an identical semantic config hash does not invalidate
evidence solely because its path changed. Both the original producer snapshot
and current active snapshot/history must validate.

The mixed-source validation run named in Section 2 cannot be made authoritative
by writing a manifest after the fact. It must rerun the producer path from
current canonical inputs.

## 8. Shared Consumer Accessor

One module owns loading and replay, for example:

```python
load_canonical_experiment_evidence(run_dir) -> CanonicalExperimentEvidence
```

The returned object contains immutable in-memory bytes or parsed values for the
validated contract/config, selected result payload, summary, analysis, and
candidate provenance. It may expose paths for diagnostics, but consumers must
not reopen them or rescan the filesystem after validation.

The loader acquires the same run-local evidence lock used by publication while
reading and validating the complete bundle. Immutable candidate directories
remain unchanged after publication. Schema v1 promises fail-closed behavior for
process interruption, not power-loss durability. A future claim of power-loss
durability requires file and parent-directory `fsync` with platform-specific
tests.

After migration, authoritative consumers must not directly use:

- `glob("stage-12*/runs/*.json")`;
- `glob("stage-14*/experiment_summary.json")`;
- `_read_prior_artifact()` for experiment summary/analysis selection;
- root `experiment_summary_best.json` or `analysis_best.md` without manifest
  replay;
- local "latest", "best", or modification-time logic.

A repository guard test must scan the authoritative consumer modules for these
patterns. Exceptions are limited to the canonical producer/replay module and
tests that intentionally construct adversarial fixtures.

## 9. Consumer Migration Inventory

This checked-in inventory is exhaustive for the baseline reviewed by this
specification. C0 begins by rerunning `rg` and updating the table for any code
drift before schemas are written.

| Module/function | Role | Required post-migration behavior |
|---|---|---|
| `_helpers._collect_experiment_results` | Stage 14 producer input | Replace cross-stage `stage-*/runs` scan with selected result-set replay. |
| `_helpers._read_best_analysis` | Generic helper | Remove authority selection; callers receive accessor data. |
| `_helpers._build_context_preamble` | Shared prompt context | Receive accessor-selected experiment context; never call generic prior-artifact selection for experiment evidence. |
| `_execution._execute_experiment_run` | Stage 12 producer | Publish only through the strict result-set manifest and isolated evidence namespace. |
| `_execution._execute_iterative_refine` | Stage 13 producer | Publish every iteration and decision through the complete refinement manifest. |
| `_code_generation._execute_code_generation` | Stage 10 producer | Bind the sealed candidate to the canonical contract and exact/full-semantic producer config snapshot. |
| `runner.execute_pipeline` | Pipeline control/persistence boundary | Enforce migration capability and canonical preflight before Stage 12+ or any Stage 15+ consumer side effect; route lesson, trajectory, memory, and package hooks through classified inputs. |
| `runner._version_rollback_stages` | Rollback invalidation | Invalidate the canonical root manifest before rename/copy and prevent archived evidence from remaining selected implicitly. |
| `runner._promote_best_stage14` | Producer/promotion | Replace glob/copy policy with Section 6 reconstruction and publication. |
| `runner._run_experiment_diagnosis` | Repair/diagnostic input | Diagnose only accessor-selected evidence and bind its manifest hash. |
| `runner._run_experiment_repair` | Repair producer | Publish repaired execution through Stage 12/13/14 manifests; never overwrite direct summaries. |
| `runner._consecutive_empty_metrics` | Decision gate | Derive emptiness from canonical selected observations, not current/versioned summaries. |
| `runner._check_experiment_quality` | Decision gate | Remove root/direct/version fallback and use accessor data. |
| `runner` experiment-memory hook | Persistent memory producer | Record only accessor-selected metrics plus canonical manifest hash; never read root `results.json`. |
| `evolution.extract_lessons` | Persistent lesson producer | Bind experiment-derived lessons to accessor-selected evidence and canonical manifest hash. |
| `evolution._extract_runtime_lessons` | Runtime lesson producer | Inspect only selected invocation diagnostics; emit no unbound numeric/result claims. |
| `_helpers._get_evolution_overlay` | Prompt input | Exclude experiment-derived lessons unless their canonical binding replays and matches the current generation. |
| `runner._metaclaw_post_pipeline` | Durable skill producer | Under schema v1, never convert experiment-derived numeric/result lessons into cross-run skills. |
| `evolution.record_refine_trajectory` | Diagnostic-only trajectory producer | Derive from the strict Stage 13 manifest, bind its hash, and never parse raw `refinement_log.json` as authority. |
| `evolution.get_trajectory_signal` | Diagnostic-only trajectory report | May write a diagnostic report but cannot affect decisions, prompts, checkpoints, promotion, packaging, or release. |
| `experiment_repair.select_best_results` | Producer repair selector | Select only replay-valid repair candidates; do not scan direct/versioned summaries. |
| `experiment_repair.run_repair_loop` | Repair producer | Publish repaired execution through Stage 12/13/14 manifests; never write root `experiment_summary_best.json` as authority. |
| `experiment_repair._load_experiment_summary` | Repair input | Replace latest/versioned scan with accessor-selected summary bytes. |
| `experiment_repair._collect_experiment_output` | Repair diagnostic input | Collect only accessor-bound execution output and record its manifest identity. |
| `experiment_repair._build_experiment_summary_from_run` | Repair candidate producer | Build an immutable candidate payload; it cannot publish or select canonical evidence directly. |
| `_analysis._execute_result_analysis` | Stage 14 producer | Consume selected result union and publish immutable candidate. |
| `_analysis._read_experiment_summary` | Stage 15 authoritative helper | Return accessor-selected summary bytes only. |
| `_analysis._read_agent_results_canonical` | Stage 15 authoritative helper | Bind agent result interpretation to the selected result-set manifest. |
| `_analysis._agent_requirements_decision` | Stage 15 decision gate | Evaluate requirements against accessor-selected evidence only. |
| `_analysis._execute_research_decision` | Stage 15 authoritative stage | Bind decision inputs and output to the canonical evidence manifest hash. |
| `experiment_diagnosis.assess_experiment_quality` | Stage 14/15 diagnostic gate | Receive only accessor-selected summary and bound refinement data. |
| `experiment_diagnosis.diagnose_experiment` | Repair decision input | Diagnose only the accessor-selected result set and summary. |
| `requirements_judge.judge_requirements` | Stage 15 decision input | Judge requirements against accessor-selected summary and agent evidence only. |
| `agents.figure_agent.orchestrator.FigureOrchestrator.orchestrate` | Bound in-memory subconsumer root | Receive one immutable accessor payload and manifest identity; pass that same generation to every subagent. |
| `agents.figure_agent.decision.FigureDecisionAgent.execute` | Bound in-memory subconsumer | Use only orchestrator-provided selected experiment context. |
| `agents.figure_agent.planner.PlannerAgent.execute` | Bound in-memory subconsumer | Plan only from the orchestrator-provided selected result payload. |
| `agents.figure_agent.codegen.CodeGenAgent.execute` | Bound in-memory subconsumer | Generate scripts only from the same selected payload and candidate generation. |
| `agents.figure_agent.renderer.RendererAgent.execute` | Bound in-memory subconsumer | Render only candidate-owned scripts into the staging candidate namespace. |
| `agents.figure_agent.critic.CriticAgent.execute` | Bound in-memory subconsumer | Compare figures only with the same accessor-selected observations. |
| `agents.figure_agent.integrator.IntegratorAgent.execute` | Bound in-memory subconsumer | Publish only files captured by the same Stage 14 candidate closure. |
| `templates.results_table_builder.build_results_tables` | Table producer | Build from a registry derived exclusively from accessor-selected evidence. |
| `templates.results_table_builder.build_condition_whitelist` | Writer context producer | Derive reportable conditions only from the accessor-bound registry. |
| `_paper_writing._execute_paper_outline` | Authoritative consumer | Build outline context from accessor bytes only. |
| `_paper_writing._collect_raw_experiment_metrics` | Authoritative helper | Delete broad source scans; serialize accessor-selected observations only. |
| `_paper_writing._collect_grounded_metric_whitelist` | Authoritative validator input | Derive whitelist from accessor-selected result set only. |
| `_paper_writing._write_paper_sections` | Authoritative writer input | Receive only selected evidence and manifest identity. |
| `_paper_writing._check_ablation_effectiveness` | Authoritative quality input | Use selected structured results only. |
| `_paper_writing._detect_result_contradictions` | Authoritative validator | Compare manuscript against accessor-selected evidence only. |
| `_paper_writing._execute_paper_draft` | Authoritative stage | Preflight and bind canonical manifest hash to fact closure. |
| `experiment_fact_closure.build_experiment_fact_closure_report` | Authoritative validator | Delete existence-first source selection; bind canonical manifest hash. |
| `experiment_fact_closure.validate_experiment_fact_closure_report` | Closure replay | Recompute from accessor bytes and require the bound canonical manifest hash. |
| `sectional_execution.build_validation_context` | Authoritative validator input | Stop merging Stage 12/14/root sources; use selected bundle only. |
| `sectional_execution.execute_sectional_revision` | Authoritative Stage 19 consumer | Bind every validation context and manifest to the accessor-selected bundle. |
| `_review_publish._collect_experiment_evidence` | Authoritative helper | Render accessor-selected evidence only. |
| `_review_publish._execute_quality_gate` | Authoritative gate | Use selected evidence and bound fact closure. |
| `_review_publish._execute_knowledge_archive` | Archive producer | Archive selected analysis bytes and canonical identity only. |
| `_review_publish._sanitize_fabricated_data` | Authoritative sanitizer | Compare against selected evidence only; no root/version fallback. |
| `_review_publish._execute_export_publish` | Package producer | Export only selected candidate charts/tables and manifest-bound evidence. |
| `stage_impls._release_audit._execute_truth_audit` | Release-stage consumer | Recompute fact provenance from selected bundle. |
| `stage_impls._release_audit._execute_deai_audit` | Final audit consumer | Consume only manifest-bound truth/citation/fact audit artifacts. |
| `verified_registry.VerifiedRegistry.from_run_dir` | Registry producer | Register only accessor-validated evidence and manifest hash. |
| `release_artifacts.collect_evidence_index` | Package/evidence producer | Remove broad evidence globs; index manifest-bound files only. |
| `release_artifacts.match_value_to_evidence` | Claim evidence matcher | Match values only against the accessor-bound evidence index. |
| `citation_release_audit.audit_citation_evidence` | Release audit | Cross-check citation/fact closures against canonical evidence hash. |
| `report._experiment_section` | User-facing report | Derive run count, metrics, summary, and analysis entirely from accessor bytes; label invalid evidence explicitly. |
| `collaboration.publisher._extract_experiments` | External publisher | Publish only accessor-validated summary bytes and manifest identity. |
| `experiment.visualize.generate_all_charts` | Figure producer | Receive explicit selected result/candidate input; no Stage 12/14 version scan. |
| `hitl.claim_verifier.ClaimVerifier._verify_numerical` | Authoritative HITL verifier | Replace substring search in direct Stage 14 summary with accessor-grounded exact numeric validation. |
| `copilot.branching.BranchManager._read_experiment_summary` | Branch decision input | Load each branch's canonical accessor bundle; invalid branch evidence cannot participate. |
| `hitl.summarizer.generate_pause_summary` | HITL decision input | Summarize accessor-selected metrics and manifest validity only. |
| `hitl.tui.monitor.ExperimentMonitor.get_experiment_status` | Diagnostic-only UI | Use accessor when valid; otherwise display unavailable/invalid and never feed pipeline state. |
| `hitl.quality_predictor._assess_results` | HITL decision input | Assess accessor-selected analysis bytes and validity, not direct Stage 14 presence. |
| `mcp.server.ResearchClawMCPServer._handle_get_results` | External result API | Return accessor-selected results and canonical manifest identity; when the canonical capability is unavailable, return an explicit unsupported/error response. |
| `scripts/release_check.py` claim evidence authorization | Independent release consumer | Remove separate Stage 12/13/14/root-best authorization and call the pure reconstruction oracle. |

Any report, visualization, dashboard, agent, or publisher not using the
accessor must be classified `diagnostic-only` and tested to prove that its
output cannot flow into manuscript prompts, closure, registry, package,
promotion, or release findings. Generic helpers named `best`, `latest`,
`prior`, `summary`, or `analysis` are included in the guard even when they do
not contain a literal known glob.

The HITL TUI and trajectory signal are the only diagnostic-only exceptions
listed above. Tests prove neither has a write or return-value path into claim
state, guidance, stage prompts, decisions, checkpoint, registry, package,
promotion, or release artifacts. If either call graph changes, it becomes a
mandatory accessor consumer before the change may merge.

Experiment-derived lessons are persistent prompt inputs, not diagnostics.
Their schema records the canonical manifest path/hash and a categorical lesson
type. Runtime warning text may be retained only from the selected invocation;
raw metric values and comparative scientific conclusions are forbidden in
schema-v1 overlays. `_get_evolution_overlay` replays the binding against the
current generation before rendering a lesson. MetaClaw conversion rejects all
experiment-derived lessons in schema v1 so an old run cannot become durable
numeric authority in a later run. Experiment memory follows the same binding
rule and cannot be retrieved into a prompt as scientific evidence.

The repository guard combines AST import/call inspection with a narrow textual
scan. A simple blacklist of known glob strings is insufficient.

The inventory scan also classifies known non-production paths explicitly:
tests and probe scripts may construct invalid layouts only inside fixtures;
unsupported `collider_agent` publication paths remain capability-gated and may
not emit schema-v1 evidence. A new production caller of a summary, analysis,
result, refinement, or candidate selector fails the repository guard until it
is added to this table with an accessor or diagnostic-only classification.

## 10. Release Audit

Release check calls one pure reconstruction function:

```python
reconstruct_expected_canonical_evidence(run_dir) -> CanonicalExperimentEvidence
```

This function receives only `run_dir`. It may use canonical selectors and
strict source artifacts. It must not use the stored canonical manifest's
selected candidate, metric value, valid/status fields, counts, or selection
reason as an oracle. It first reconstructs the complete expected manifest and
validated in-memory bundle, then the checker loads the producer manifest only
to require exact structural equality.

The checker follows this loading order exactly:

1. verify only that `canonical_experiment_evidence.json` exists as a safe
   regular file; do not parse or pass its bytes to reconstruction;
2. call `reconstruct_expected_canonical_evidence(run_dir)`, which reselects the
   canonical Stage 9 contract and run-local configuration;
3. replay the Stage 10 sealed input and Stage 12 baseline result set;
4. replay the complete Stage 13 manifest and independently select baseline or
   refinement;
5. rederive claim scope, dataset origin, config semantic identity, exact metric,
   aggregation, and direction;
6. enumerate and replay all immutable Stage 14 candidates;
7. rerun deterministic selection without consulting the stored winner and
   finish the complete expected manifest/in-memory bundle;
8. only now strict-load the stored producer manifest with duplicate-key and
   exact-schema validation;
9. require exact structural equality between stored and expected manifests;
10. verify source/canonical summary and analysis byte identity using the
    reconstructed bundle;
11. verify all downstream fact-closure and release artifacts bind the same
    canonical manifest hash;
12. reject any legacy consumer artifact that records a different experiment
    source;
13. emit a non-degraded-compatible error for every mismatch.

Suggested error-code family:

- `canonical_experiment_evidence_missing`;
- `canonical_experiment_evidence_invalid`;
- `canonical_evidence_migration_incomplete`;
- `canonical_experiment_mode_unsupported`;
- `experiment_result_set_mismatch`;
- `experiment_contract_binding_mismatch`;
- `experiment_config_binding_mismatch`;
- `experiment_refinement_provenance_mismatch`;
- `experiment_candidate_provenance_mismatch`;
- `experiment_selection_replay_mismatch`;
- `experiment_summary_copy_mismatch`;
- `experiment_analysis_copy_mismatch`;
- `experiment_consumer_binding_mismatch`.

No code in this family may be added to the degraded-compatible exit-code
allowlist. A pipeline-validation run may remain useful for engineering
diagnosis, but canonical-evidence errors still produce failure.

## 11. Failure Artifact Lifecycle

Producer stages must clean or invalidate their owned canonical outputs before
starting work. On failure:

- Stage 12 may retain explicitly named diagnostics or partial results, but must
  not retain a validly named result-set manifest from an earlier attempt;
- Stage 13 may retain rejected immutable iteration evidence, but must not retain
  a valid refinement manifest after an interrupted rebuild;
- Stage 14 may retain immutable completed candidates and explicitly named
  diagnostics, but a failed staging directory is never renamed into the
  candidate namespace and cannot participate in promotion;
- promotion must not leave a new compatibility copy paired with an old
  canonical manifest, or vice versa;
- downstream stages must remove stale success artifacts before canonical
  evidence preflight;
- partial files and `*.tmp` files are rejected by default-deny enumeration.

Atomic writes and the evidence lock reduce torn state but do not replace replay.
The old canonical pointer is invalidated before compatibility replacement. The
new canonical manifest is the final publication point only after all selected
copies exist and validate. Consumers hold the lock and consume validated bytes,
so they cannot observe or reopen a half-published bundle.

## 12. Implementation Sequence

The migration must be split into narrow, independently reviewed commits but
released as one compatibility window.

### 12.1 Mandatory migration capability gate

C0 introduces a central capability registry with one required schema version
for each component:

```text
stage10_sealed_input
stage12_result_set
stage13_refinement_set
stage14_candidate_and_promotion
metric_contract_authority
shared_accessor
stage15_17_consumers
stage19_22_consumers
stage24_release_consumers
external_and_persistent_consumers
independent_release_reconstruction
```

Each component module declares its implemented version. Both the CLI and
`execute_pipeline()` evaluate the complete registry before any stage executes
whenever the requested range reaches Stage 12 or later. Missing, zero,
different, or unsupported versions produce
`canonical_evidence_migration_incomplete` and hard failure. This is not a config
flag and has no user override.

C0 also installs one deny-only function, for example
`require_canonical_evidence_capabilities(entrypoint)`, at every authoritative
external or persistent entrypoint that can be called without
`execute_pipeline()`. It evaluates the same complete registry as its first
executable action, before reading or writing any run, lesson, memory, skill, or
published artifact. At minimum this applies independently to:

- MCP `_handle_get_results` and any server/dashboard result API;
- the report CLI plus `generate_report()`;
- `ArtifactPublisher.publish_from_run_dir()` and experiment extraction;
- evolution lesson extraction/persistence and `_get_evolution_overlay`;
- MetaClaw lesson-to-skill conversion;
- experiment-memory record/retrieval adapters.

Python entrypoints raise a typed migration-incomplete error. External protocols
return an explicit structured error with the same code and no legacy payload.
The guard is defense in depth at public and directly callable lower-level
functions; callers cannot bypass it by importing a helper. C0 does not migrate
their authority selectors, but it must install these deny-only shims. C0-C4
therefore make legacy reads mechanically unreachable through pipeline, CLI,
Python, MCP, report, publisher, or persistent-hook entrypoints.

C0-C4 keep at least one required component incomplete. The C5 activation
commit sets the last component to v1 only after every consumer and release
oracle is present. Tests simulate every partial capability map through both CLI
and Python pipeline entrypoints and every external/persistent entrypoint above.
They require failure before Stage 1 side effects, LLM calls, artifact reads, or
persistent writes. Runs explicitly ending before Stage 12 remain available for
pipeline development, but external experiment-result access remains blocked
until C5.

### C0: schemas and strict loaders

- add the mandatory blocked capability registry;
- upgrade Stage 10 canonical contract/schema/loader semantics, including exact
  producer config snapshot and full semantic identity;
- add strict Stage 12 baseline, Stage 13 refinement, Stage 14 candidate, and
  canonical-manifest models;
- add duplicate-key JSON loading, path/hash validators, and exact file-set
  checks;
- add the invocation-journal schema/controller interface and direct-sandbox-call
  repository guard while keeping execution blocked;
- freeze the full config semantic-hash policy and exact Decimal metric grammar;
- install deny-only capability guards at every external/persistent entrypoint;
- add producer/replay round-trip tests;
- do not switch consumer authority selection yet.

### C1: Stage 12-14 producers and deterministic promotion

- publish the isolated single-invocation Stage 12 evidence namespace and
  baseline manifest; failed/partial execution publishes no manifest;
- route sandbox/docker execution through the controller-owned one-acquire lease,
  append-only invocation journal, and result-set binding;
- publish complete Stage 13 iteration/refinement provenance;
- publish immutable Stage 14 candidates with deterministic candidate IDs;
- enforce same-filesystem staging, strict pre-rename replay, and `EXDEV` hard
  failure;
- replace current root promotion with eligible-candidate selection;
- publish canonical manifest and compatibility copies under the evidence lock;
- implement invalidation on producer rerun;
- keep the runtime capability gate blocked.

### C2: central accessor and early consumers

- add the single shared accessor;
- migrate Stage 15, Stage 16, Stage 17 writing, and Stage 17 fact closure;
- migrate evolution overlays, experiment memory, MCP results, reports, FigureAgent
  context propagation, and runner rollback/preflight control paths;
- add repository guard tests for direct and indirect source selection;
- keep the runtime capability gate blocked.

### C3: peer review, revision, quality, archive, and export consumers

- C3-A0: migrate Stage 18 peer-review context. It loads one immutable accessor
  snapshot before any LLM call; the review evidence renderer receives only that
  snapshot, and `review_structure_report.json` binds its manifest path and hash.
- C3-A: migrate both Stage 19 revision paths and the independent sectional
  replay. `validation_context.json` uses its own schema v2 numeric policy:
  `grounded_numeric_values` are unique canonical Decimal strings under
  `stage19_decimal_v1`, never JSON numbers. The Stage 19 validator, producer,
  and independent replay parse these tokens as finite `Decimal` values and use
  the local exact/round-half-even/relative-tolerance matcher; they do not
  convert canonical numeric authority through binary float. Before either
  revision path or the independent audit accepts Stage 17 citation closure,
  it captures and replays one immutable citation authority bundle: Stage 04
  candidates/registry/bibliography, Stage 05 shortlist/screening report,
  Stage 06 cards manifest plus its exact card JSON/Markdown closure and
  allowlist, Stage 16 effective policy/final plan, and the active config
  pointer/history/checkpoint/snapshot. The capture also binds optional-file
  presence, the exact cards namespace, and the exact set of resumed config
  snapshots. Plan and allowlist hashes alone are never authority. The active
  config may use a different resume path only when its complete semantic hash
  equals the canonical experiment-evidence config generation. A final full
  rediscovery compares namespace, selected path, and bytes without consuming
  any reread bytes as producer or audit input.
- C3-B: migrate Stage 20 quality consumers. Stage 20 captures the exact
  `stage-19/paper_revised.md` plus exactly one legacy or sectional publication
  binding, and rejects shadow/versioned papers or ambiguous bindings. Citation
  minimums are evaluated from the source-replayed Stage 04-06/16 authority;
  experiment context, `VerifiedRegistry`, and fabrication values are derived
  only from one immutable canonical experiment snapshot. Stage 14 glob scans,
  root `experiment_summary_best.json`, and `VerifiedRegistry.from_run_dir()`
  are not authority. `quality_report.json` and `fabrication_flags.json` bind
  the canonical manifest, revised-paper hash, and Stage 19 publication binding;
  metric values are canonical Decimal strings derived under a local
  precision-50, round-half-even context. An empty canonical registry is always
  fatal, regardless of stored condition names. LLM quality output uses one
  exact JSON schema that rejects duplicate keys and nonfinite values; malformed
  output cannot enter graceful degradation. One final Stage 04-19 rediscovery
  plus canonical-snapshot fixpoint runs before either report is published.
  The reports remain diagnostic until `quality_gate_manifest.json` is published
  last. That strict manifest exists only for a replayed `passed` result or a
  policy-authorized `degraded` result; every FAILED result leaves it absent.
  It binds both report hashes, canonical evidence, the Stage 19 publication,
  threshold, graceful-degradation policy, score, and derived outcome.
- C3-C: migrate Stage 21 archive consumers. Stage 21 loads the exact Stage 19
  publication plus strict `quality_report.json`, `fabrication_flags.json`, and
  final `quality_gate_manifest.json` bindings before any LLM call. It
  independently reconstructs metric values, conditions, counts, and failure
  state from canonical evidence and re-derives the passed/degraded outcome.
  Its retrospective receives only the captured
  revised paper, canonical Stage 14 analysis, canonical runtime topic, and a
  fixed upstream-proceed statement; it does not reopen Stage 15 text, goal
  files, evolution overlays, best-analysis selectors, or versioned stage
  directories. `bundle_index.json` is a deterministic path/SHA-256 inventory
  of the captured authority set and the generated archive, not a recursive
  `stage-*` filesystem listing. Stage 21 rejects a symlink or non-directory
  output namespace and invalidates `bundle_index.json` before touching the
  archive. A final Stage 04-20 fixpoint runs before each success-named output
  is published, and failure removes the index commit point before the archive.
  Stage 20 and Stage 21 each hold the canonical output directory open with
  `O_DIRECTORY | O_NOFOLLOW`; every owned-file create, readback, replace, and
  unlink is relative to that same directory fd. Final publication also requires
  the live run/stage paths to retain the captured device/inode identities, so a
  parent replacement cannot redirect cleanup or authorize detached outputs.
- C3-D: migrate Stage 22 export consumers. The shared accessor captures the
  replay-selected flat Stage 10/13 project files as immutable bytes while the
  canonical publication lock is held. Stage 22 consumes the exact Stage 19
  publication and Stage 20 quality-gate commit point, replays Stage 4-18
  citation/fact provenance, and packages code only from those captured project
  bytes. It must not reopen `experiment_final*`, scan `stage-14*`, read the
  root degradation signal, use an evolution overlay, or call an LLM to rewrite
  the final paper. Candidate policy v1 has an empty figure plan, so Stage 22 v1
  publishes no experiment charts and deterministically removes references to
  unavailable `charts/*` files. Direct files and the flat `code/` directory
  are built under directory-fd-bound staging. Production and disk replay share
  one pure semantic reconstruction from the captured bundle: the transformed
  Markdown, final LaTeX, bibliography, verification and sanitization reports,
  canonical-source binding, template bytes, and code package must match that
  reconstruction exactly. The paper verifier evaluates the final LaTeX bytes,
  not the pre-render Markdown. A strict `stage22_export_manifest.json` binds
  every mandatory output plus the canonical evidence, selected-result, Stage
  19, Stage 20, bibliography, template-source, and project-source hashes.
  The complete Stage 04-20 input graph is replayed immediately before that
  manifest is written last. Any failure removes the manifest and all
  success-named Stage 22 outputs. A missing LaTeX toolchain is represented by a
  strict bound `compile_status.json`, never inferred as success. Compile success
  requires a nonempty PDF with a PDF header; failed or unavailable compilation
  forbids a PDF. A paper-verifier `REJECT` fails Stage 22 rather than
  heuristically rewriting numeric claims. The legacy Stage 22 producer is a
  mechanically disabled entrypoint and cannot be invoked as alternate authority.
- C3-E: migrate Stage 23 to an immutable Stage 22 publication snapshot and a
  success-only, manifest-last verification publication. Failed or fatal
  verification leaves no success-named Stage 23 outputs. Stage 23 relevance
  scores are serialized from their full finite decimal text and compared using
  that same stored value; `references_verified.bib` contains only entries whose
  verification status is `verified`. The publisher invalidates its prior
  manifest and owned namespace before validating any proposed output mapping.
  `SKIP_FORBIDDEN_STAGES` also governs every HITL input path, including the
  independent CostGuard pause: pre-stage SKIP/ABORT and every post-stage action
  other than an unedited APPROVE fail the stage after the controller clears its
  authority namespace. Relevance JSON numbers are parsed directly as finite
  `Decimal` values, without an intermediate binary float conversion. Add the complete
  Stage 18-23 call-site and forbidden-scan guard, then set the historically
  named `stage19_22_consumers` capability to v1 only after independent review.
  This migration binds existing verification results; it does not change DOI
  provider aggregation, title-only relevance policy, or release thresholds.
- C3-E activation: after independent approval, set only
  `stage19_22_consumers` to v1. The Stage 24, external/persistent-consumer, and
  independent-release-reconstruction capabilities remain incomplete, so this
  component activation does not unblock a Stage 12+ production run.
- ensure each migrated manifest or closure report records the canonical-evidence
  manifest hash;
- keep the runtime capability gate blocked.

### C4: Stage 24 and release replay

Stage 24 is not permitted to treat an LLM-produced claim list as a complete
inventory. C4 is split into the following separately reviewable commits and
must land in order. `stage24_release_consumers` remains zero through C4-U0,
C4-P0, C4-A0, C4-A1, and C4-A2. It is raised only by a separate activation
commit after both Stage 24 and Stage 25 publications have independent approval.

#### C4-U0: metric-authority schema migration

- add a new required capability component `metric_contract_authority`, initially
  zero, so every public pipeline/external entrypoint remains mechanically
  blocked while this upstream schema changes;
- publish Stage 9 experiment-contract schema v2 with exact `metric_units` and
  `metric_display_labels` mappings owned by the selected domain evaluator;
- require every evaluator-emittable metric key to appear exactly once in both
  mappings, reject extra/missing/conflicting keys or labels shared by different
  metrics, and forbid summary/analysis/LLM-derived units or labels;
- upgrade the Stage 10 seal, Stage 12 result-set producer/replay, Stage 13
  refinement producer/replay, Stage 14 candidates/promotion, shared accessor,
  and a pure independent Stage 9-14 expected-reconstruction helper to require
  contract schema v2 and preserve those mappings unchanged;
- reject every mixed v1/v2 chain and every resume that attempts to reuse v1
  Stage 9-14 authority under a v2 consumer. Such a run must rerun Stage 9-14;
- raise only `metric_contract_authority` after producer, loader, replay,
  rollback, resume, and independent tests pass. All later C4 capabilities stay
  zero.

This is an upstream authority migration, not an implicit Stage 24 parser
feature. C4-P0 cannot start until C4-U0 is independently approved.

Contract schema v2 has exactly the v1 top-level fields plus
`metric_authority`, `metric_units`, and `metric_display_labels`; duplicate,
missing, or unknown YAML keys reject. `metric_authority` has exactly `{path,
sha256, policy_version, domain_id, evaluator_id, domain_profile_path,
domain_profile_sha256, selector_index_path, selector_index_sha256,
domain_selector_package_path, domain_selector_package_sha256,
domain_selector_snapshot_path, domain_selector_snapshot_sha256,
selector_input_sha256, selector_policy_version, experiment_mode,
evaluator_kind}`. Its authority path is fixed to
`stage-09/metric_authority.json`, the profile path to
`stage-09/domain_profile.json`, and the index path to
`stage-09/metric_authority_index.json`; the domain selector snapshot path is
fixed to `stage-09/domain_selector_policy.json`, and the package path is fixed
to the trusted package path defined below. `metric_units` is an object from
metric key to one closed unit string. `metric_display_labels` is an object over
the identical key set whose values are nonempty ordered arrays of 1-8 nonempty
strings.
Metric keys match `[a-z][a-z0-9_]{0,63}`. Label identity uses NFKC, Unicode
casefold, trim, and Unicode-whitespace collapse to one ASCII space. Raw labels
must already equal their trimmed form; normalized labels are unique within and
across metrics, and array order is authority.

The mappings do not originate in the contract. Code-owned domain registries
live at the fixed package namespace
`researchclaw/experiment_runtime/metric_authority/<evaluator_id>-v1.json`. Each
strict registry has exactly `{schema_version, policy_version, domain_id,
evaluator_id, owner, metrics}`, where owner is exactly `scaffold` and metrics is
an ordered metric-key-sorted array of exact `{key, unit, display_labels}`
objects using the rules above. The code-owned selector index is fixed at
`researchclaw/experiment_runtime/metric_authority/index-v1.json` and has exactly
`{schema_version, selector_policy_version, entries}`. Entries are an ordered,
lexicographically sorted array of exact `{domain_id, experiment_mode,
evaluator_kind, evaluator_id}` objects; the first three fields are globally
unique.

Canonical metric-domain profiles are separate from mutable prompt-adapter
profiles and live at
`researchclaw/experiment_runtime/metric_authority/profiles/<domain_id>-v1.json`.
Each has exactly `{schema_version, selector_policy_version, domain_id, owner,
supported_experiment_modes}`, with owner `scaffold`, a nonempty ordered closed
mode array, and a filename stem matching the domain ID. Prompt profile text,
display names, hypotheses, and Stage 9 prose are not selector inputs.

The first-level code-owned policy is fixed at
`researchclaw/experiment_runtime/metric_authority/domain-selector-v1.json` and
has exactly `{schema_version, selector_policy_version, normalization,
no_match, rules}`. Schema version is 1, selector policy is
`domain_selector_v1`, normalization is `nfkc_casefold_space_v1`, and no-match
policy is `error`. `rules` is a nonempty ordered array of exact `{rule_id,
pattern_kind, normalized_pattern, domain_id, priority}` objects. Rule IDs are
unique nonempty ASCII identifiers; pattern kind is `whole_word` or `substring`;
priority is a JSON integer from 0 through 1000 with booleans rejected. Every
domain ID names exactly one versioned canonical metric-domain profile.

Topic normalization strict-decodes the active-config string, applies Unicode
NFKC, Unicode casefold, trims, and collapses every Unicode whitespace run to
one ASCII space. A policy pattern must already equal that normalized form and
be nonempty. `substring` is literal normalized-string containment.
`whole_word` is literal containment with both edges at string boundaries or
adjacent to a non-word code point; word code points are Unicode Letter, Unicode
Number, or underscore. Regex, locale, stemming, tokenization, and fuzzy matching
are forbidden. All matching rules are collected. The maximum priority wins;
all maximum-priority matches must name the same domain or selection fails as
ambiguous. Zero matches fails. Array order cannot break a tie and rules are
canonically sorted by descending priority then `rule_id`.

Package policy files are immutable validator inputs for their declared policy
version. Changing a selector mapping, profile semantics, registry content, or
metric labels requires a new selector/policy version and, where registry
content changes, a new evaluator ID; v1 files are never overwritten in place.
Replay loads these trusted package policy bytes first and requires every
run-local snapshot to be byte-identical to the independently selected package
source.

Stage 9 runs two pure selectors before reading plan-derived evaluator fields:

```text
select_canonical_domain_profile(
  active_config.research.topic,
  code_owned_domain_selector_policy,
  domain_selector_policy_v1
) -> {domain_id, package_profile_path, package_profile_sha256}

select_metric_authority(
  canonical_domain_profile_identity,
  active_config.experiment.mode,
  evaluator_kind_for_mode,
  metric_selector_policy_v1,
  code_owned_selector_index
) -> evaluator_id
```

The domain selector is the exact topic-only policy above; process-global forced
profiles, environment overrides, hypotheses, literature, plan, Stage 9 output,
and an LLM fallback cannot alter it. It must select exactly one code-owned
profile or fail. Its identity is canonical JSON over `{domain_id,
package_profile_path, package_profile_sha256, topic_raw_sha256,
topic_normalized_sha256, domain_selector_package_path,
domain_selector_package_sha256, selector_policy_version}`. Experiment mode
comes from the active config semantic projection. Evaluator kind is the
closed deterministic mode mapping: `sandbox` and `docker` map to `scaffold`;
every other mode is unsupported by canonical metric policy v1. The metric
selector performs an exact unique tuple lookup in the code-owned index and
never accepts a producer-supplied evaluator ID.

Stage 9 copies the domain selector policy, selected profile, selector index, and
selected registry to the four fixed run-local paths above.
`domain_selector_package_sha256` is computed from the trusted fixed package
path; `domain_selector_snapshot_sha256` is computed independently from the
run-local snapshot, and the two hashes and bytes must match. The contract may
record these derived identities but cannot choose either source.
`selector_input_sha256` hashes exactly
`{canonical_domain_profile_identity, experiment_mode, evaluator_kind,
selector_policy_version, domain_selector_package_sha256,
selector_index_sha256}`. The contract records the
independently derived result, and its mappings must be exact projections of the
separate registry snapshot. Config, LLM output, plan, contract fields, summary,
and analysis cannot add, override, or select registry content.

Stage 10 binds contract v2 and all four run-local policy/profile/index/registry
snapshots and requires its
deterministic scaffold to declare the same registry hash. Stage 12/13 result
sets record that hash and may emit exactly the registry metric-key set permitted
by evaluator policy. Stage 10-14 replay and the U0 expected helper rerun both
selectors from active config plus the versioned trusted package domain policy,
profile, index, and registry, then compare the independently selected package
bytes with the fixed run-local snapshots and the derived domain/evaluator IDs
with every stored copy; contract, run-local domain policy/index, or snapshot
domain/evaluator ID is never the selector oracle. Stage
14 candidates, promotion, and accessor preserve the same selector-input and
owner/path/hash/key/unit/label closure. The helper imports no Stage 24 or
release producer and does not inspect stored winner/status fields. C4-B later
extends this already approved helper through Stage 15/23/24/25; C4-U0 does not
set or depend on `independent_release_reconstruction`.

#### C4-P0: bound Stage 15 critique publication

- replace the mutable `stage-15/critique.json` workflow with critique schema v2
  and a manifest-last `stage15_critique_manifest.json` publication;
- bind the critique generation to the canonical experiment-evidence manifest,
  raw `decision.md` bytes, strict `decision_structured.json` bytes, critic
  source, critic model, writer model, policy version, and any external-review
  bytes;
- require exact finding fields, nonempty unique finding IDs, closed severity
  and category vocabularies, and no unknown or duplicate JSON fields. Critique
  policy v2 severities are exactly `P0`, `P1`, and `P2`; categories are exactly
  `methodology`, `evidence`, `statistics`, `reproducibility`, `validity`,
  `scope`, and `reporting`;
- define four generation states: `model_final`, `none_final`,
  `external_pending`, and `external_final`. A `none_final` publication is a
  byte-bound record that no isolated critic was available and cannot satisfy a
  release reviewer-isolation gate. `external_pending` is not critique authority
  and publishes no `stage15_critique_manifest.json`;
- retire append-in-place external findings. External review uses a strict
  structured artifact plus an optional prose artifact. Both paths and raw-byte
  hashes are fixed before `external_final` is published. The finalizer copies
  the exact structured findings into critique v2 and never asks an LLM to
  interpret the external review;
- Stage 15 rerun invalidates the old critique manifest and owned critique
  namespace before reading decision or experiment evidence. Any failure leaves
  no finalized critique authority;
- Stage 24 binds and replays the critique bytes and critique-manifest bytes, not
  only a `critique_path`. Every resolution record binds the finding ID,
  finding-content hash, critique hash, resolution-assessment input hash, model,
  and policy version. An LLM resolution cannot delete a P0/P1 finding or turn
  an unresolved finding into release authority by omission.

The external structured review schema is the sole external findings source.
Its identity is the tuple of normalized run-relative path, raw SHA-256, schema
version, reviewer identity declaration, and ordered finding closure. A prose
review is evidence for a human but is not parsed as a second findings source.

Critique policy v2 uses this exact state matrix:

| State | Authority name/manifest | Required fields | Forbidden fields |
| --- | --- | --- | --- |
| `model_final` | `critique.json` plus manifest | nonempty isolated `critic_model`, `writer_model`, canonical/decision bindings, ordered findings | every external path/hash |
| `none_final` | `critique.json` plus manifest | `writer_model`, canonical/decision bindings, empty findings, categorical unavailability reason | critic model and every external path/hash |
| `external_pending` | `critique-pending/external_review_request.json`; no manifest | target canonical/decision bindings, safe structured/prose target paths, policy version | success-named `critique.json`, findings, critic model |
| `external_final` | `critique.json` plus manifest | external reviewer declaration, structured path/hash, target bindings, ordered findings; optional distinct prose path/hash | critic model and pending request as authority |

`model_final` requires critic and writer models to be nonempty and unequal.
`none_final` and `external_pending` cannot satisfy Stage 24 success. Every
state uses an exact field set; fields forbidden by the matrix are absent, not
empty compatibility placeholders.

The literal top-level field sets are:

- `model_final`: `{schema_version, policy_version, state, recommend_only,
  canonical_evidence, decision, writer_model, critic_model, shared_context,
  findings}`;
- `none_final`: `{schema_version, policy_version, state, recommend_only,
  canonical_evidence, decision, writer_model, unavailability_reason,
  findings}`;
- `external_pending`: `{schema_version, policy_version, state,
  canonical_evidence, decision, writer_model, structured_target_path,
  prose_target_path}`;
- `external_final`: `{schema_version, policy_version, state, recommend_only,
  canonical_evidence, decision, writer_model, reviewer,
  external_structured, external_prose, findings}`.

`recommend_only` is exactly true and `shared_context` exactly false where those
fields occur. `none_final.findings` is exactly empty and
`unavailability_reason` is exactly `critic_not_configured`,
`critic_not_isolated`, or `critic_call_failed`. `external_prose` and
`prose_target_path` are JSON null when absent; no other nullable compatibility
fields exist. `reviewer` has exactly `{reviewer_id, reviewer_kind,
organization}`, where every string is nonempty and reviewer kind is `human` or
`independent_agent`. `canonical_evidence` has exactly `{path, sha256}` and
`decision` exactly `{text_path, text_sha256, structured_path,
structured_sha256}`. Each finding has exactly `{id, severity, category,
question, finding, falsification_criterion}`. Nested objects reject missing,
unknown, or duplicate fields.
`external_structured` is exactly `{path, sha256}`;
`external_prose` is exactly `{path, sha256}` or JSON null.
`structured_target_path` is exactly
`stage-15/external-review/structured.json`; `prose_target_path` is exactly
`stage-15/external-review/review.md` or JSON null. A bare path string cannot
substitute for a path/hash object in a finalized record.

Canonical discovery does not consult config-selected arbitrary paths. The
namespaces are fixed:

- final critique: `stage-15/critique.json` and
  `stage-15/stage15_critique_manifest.json`;
- pending request: `stage-15/critique-pending/external_review_request.json`;
- external structured input: `stage-15/external-review/structured.json`;
- optional external prose: `stage-15/external-review/review.md`.

Each directory has exact closure for its state and rejects symlinks,
subdirectories, temporary files, renamed/shadow records, or any extra entry.
`external-review/` is a user-supplied input namespace, not a Stage 15 owned
output namespace: rerun invalidation never deletes or rewrites it. Stage 15 owns
only final critique/manifest and `critique-pending/`; stale external bytes are
harmless unless their embedded target generation matches the current inputs.
The finalized critique manifest has exactly `{schema_version, policy_version,
state, critique_path, critique_sha256, canonical_evidence, decision,
external_inputs, findings_sha256, finding_count, output_namespace}`.
`external_inputs` is an empty array for model/none and the ordered structured
then optional prose `{path, sha256}` closure for external final. The output
namespace is independently enumerated and must equal the state-specific names;
the manifest cannot declare an alternate source path.

The strict external structured review records its target canonical-evidence
manifest path/hash, `decision.md` path/hash, `decision_structured.json`
path/hash, critique/review policy versions, reviewer identity declaration, and
ordered findings. The finalizer requires these target fields to equal the
current captured generation before copying findings. Structured and optional
prose paths are normalized run-relative regular files: absolute paths, `..`,
backslashes, percent-encoded path syntax, parent or leaf symlinks, non-files,
and a structured/prose path collision are rejected. The finalizer compares
both external files' raw bytes before and after publication. An old external
review cannot be rebound to a new decision or canonical generation.

`external-review/structured.json` has exactly `{schema_version,
policy_version, target_canonical_evidence, target_decision, reviewer,
findings}` and reuses the exact nested schemas above. Its target objects are
part of the external review bytes and cannot be supplied later by the
finalizer.

All identity hashes introduced by C4 use canonical identity JSON bytes:
`json.dumps(payload, sort_keys=True, separators=(",", ":"),
ensure_ascii=False, allow_nan=False).encode("utf-8")`. Identity payloads use
only JSON null/boolean/string/array/object values; Decimal values appear as the
canonical Decimal strings defined by their policy. Concatenated ad hoc byte
hashes are forbidden. This rule applies to finding-content,
resolution-assessment-input, citation-assessment-input, obligation, bundle,
and publication identities.

`finding_content_sha256` hashes exactly `{finding_id, severity, category,
question, finding, falsification_criterion}`. A resolution-assessment input
hashes exactly `{schema_version, policy_version, critique_sha256,
finding_content_sha256, raw_paper_sha256, critic_model}`. Its raw source record
has exactly `{schema_version, assessment_id, assessment_input_sha256,
critic_model, policy_version, resolution, note}`; assessment ID is derived from
the input hash and resolution is limited to `fixed`, `rebutted`, or
`unresolved`. `accepted-risk` is not a v1 LLM resolution and no LLM output can
authorize release risk. A future human risk-acceptance workflow requires a new
policy and a separate permission-checked artifact; it is not a compatibility
field in v1. Independent replay reads this raw source
record only after reconstructing its complete bound input graph, then derives
resolution counts and release status without consulting a producer-manifest
copy of those values.

#### C4-A0: immutable Stage 24 input graph

- add an immutable `Stage23PublicationSnapshot` whose paper identity is the
  SHA-256 of exact Stage 23 paper bytes. Whitespace-normalized paper hashes may
  remain diagnostic fields but never participate in identity, replay, or a
  fixpoint;
- add one `Stage24InputBundle` that captures the Stage 23 publication, its
  nested Stage 22/19/canonical-experiment input graph, the finalized Stage 15
  critique publication, citation plan and effective policy, evidence-card JSON
  and deterministic Markdown bytes, Stage 23 verification bytes, Stage 9
  contract bytes, and the active run-config binding. Its strict model projection
  records `writer_model=config.llm.primary_model`,
  `citation_assessment_model=config.paper_revision.critic_model`,
  `generic_support_model=config.paper_revision.critic_model`, and
  `resolution_assessment_model=config.llm.critic_model`;
- load the bundle once before any Stage 24 LLM call. Consumers receive frozen
  mappings and bytes only and must not reopen diagnostic paths. During the
  C4-A0 implementation window the Stage 24 entrypoint performs this preflight,
  then fails explicitly with `canonical_stage24_mode_not_activated`; it makes
  no LLM call and writes no success-named Stage 24 output;
- define the final citation assessment input and source-record schemas in
  C4-A0, but do not connect a transitional ordinal-, character-offset-, or
  normalized-text identity to the producer. C4-A1 atomically adds the unified
  UTF-8-byte obligation inventory and replaces
  `build_citation_support_closure(run_dir, ...)` with the pure
  `replay_citation_support_closure(inputs, assessment_records)`. Its input
  object contains only bytes and strict parsed values captured by the bundle;
  it performs no run-directory read, selector, glob, or config load;
- each citation-support assessment binds the citation instance, claim span,
  cited key, exact card/excerpt hashes, Stage 23 verification record, critic
  model, policy version, and `assessment_input_sha256`. Assessment identity and
  isolation are checked independently; critic prose alone cannot establish
  support;
- immediately before publication, rediscover and replay the complete Stage
  04-23 source graph and compare selected paths, namespace, raw bytes, and
  semantic objects without consuming the fresh bytes as producer input.

C4-A0 and C4-A1 are intentionally not runtime-compatible intermediate modes.
C4-A0 leaves `stage24_release_consumers=0` and the producer fail-closed after
preflight. C4-A1 must remove that explicit stop in the same reviewed change
that installs obligation IDs, assessment input hashes, source-record namespace
closure, staged semantic replay, final source fixpoints, and manifest-last
publication. Tests must not reactivate the legacy producer through a fixture or
private compatibility switch.

`Stage24InputBundle` identity is the canonical identity JSON hash of an exact
ordered array of `{role, normalized_run_relative_path, raw_sha256}` entries plus
the canonical experiment manifest path/hash, Stage 23 publication path/hash,
critique publication path/hash, active-config semantic hash, and bundle policy
version. It also includes the four exact model-projection fields above. The
projection is derived from the already captured active config before any
assessment source record is discovered; all three assessment models must be
nonempty and unequal to the writer model. Source-record `critic_model` is only
an equality check against that projection and cannot select or change the
expected assessment ID. Roles and their order are fixed by the strict bundle
schema; filesystem enumeration order never enters identity.

Citation `assessment_input_sha256` is the canonical identity JSON hash of
exactly `{schema_version, policy_version, canonical_manifest_sha256,
paper_sha256, obligation_id, byte_start, byte_end, source_sha256, instance_id,
cite_key, stage23_verification_record_sha256, evidence_records, critic_model}`.
`evidence_records` is the ordered exact closure of `{card_id, card_sha256,
excerpt_id, excerpt_sha256, byte_start, byte_end}`. The raw assessment source
record has the exact fields `{schema_version, assessment_id,
assessment_input_sha256, critic_model, policy_version, verdict, reason}`;
`assessment_id` is independently derived from the input hash. Duplicate keys,
unknown fields, identity mismatch, a writer-equal critic, or a verdict outside
`supported`/`unsupported` rejects the record.

Non-deterministic Stage 24 source records use fixed, default-deny namespaces:

- citation assessments:
  `stage-24/citation-assessments/<full-assessment_id>.json`;
- generic-claim assessments:
  `stage-24/generic-support-assessments/<full-assessment_id>.json`;
- critique resolutions:
  `stage-24/resolution-assessments/<full-assessment_id>.json`.

The full lowercase assessment ID is the filename stem; truncation, aliases,
producer-declared paths, and nested directories are forbidden. Expected IDs
are derived from the independently reconstructed input graph before any source
record is opened. Each directory must equal the expected filename set exactly,
in lexicographic ID order, and rejects missing, extra, duplicate-ID,
misnamed, symlink, non-file, temporary, or shadow records. These source-record
directories are inputs to staged semantic replay but are not consulted through
the stored Stage 24 manifest to discover records.
All three assessment directories are success-publication content: the producer
builds them under same-filesystem staging, the Stage 24 manifest binds their
exact file closure and hashes, and attempt-start invalidation removes any prior
live assessment directories before making a new LLM call.

#### C4-A1: deterministic obligations and Stage 24 publication

Add a pure `build_claim_obligation_inventory(paper_bytes)` before any claim
classification. It strict-decodes UTF-8 and returns exactly the ordered union
of these four policy-v1 kinds, with no additional inferred obligations:

1. `numeric_token`: every numeric-token occurrence;
2. `citation_instance`: every citation instance;
3. `comparative_sentence`: every sentence matching the versioned explicit
   comparison grammar; and
4. `declarative_sentence`: every declarative sentence in Results, Discussion,
   and Contributions
   sections, including normalized heading aliases fixed by the policy version.

Inventory positions are half-open offsets into the original UTF-8 byte string,
not offsets into normalized text. Every obligation records policy version,
kind, section identity, byte start/end, exact source bytes SHA-256, sentence or
token bytes SHA-256, and an ID derived only from those fields. Parsing may use a
strict UTF-8 text view, but producer and replay map every span back to raw bytes
and require exact round-trip equality. Multiple obligation kinds may point to
the same sentence; they are not silently deduplicated across kinds.

The ordered union sorts by `(byte_start, byte_end, kind_rank, occurrence_rank)`,
where kind rank is the order above and occurrence rank is the zero-based source
order within that kind. A repeated candidate record within the same kind and
span is an error; overlap across kinds is preserved. Section identity is
exactly `{ordinal, level, normalized_path, heading_sha256}`. Preamble identity
is the string `preamble`. Normalized paths use NFKC, Unicode casefold, trim,
and collapse each Unicode whitespace run to one ASCII space; no semantic alias
replacement occurs inside identity.

Each obligation identity payload has exactly:

```json
{
  "schema_version": 1,
  "policy_version": "claim_obligation_v1",
  "paper_sha256": "<raw-paper-sha256>",
  "kind": "numeric_token",
  "section_identity": {
    "ordinal": 6,
    "level": 2,
    "normalized_path": ["results"],
    "heading_sha256": "<raw-heading-sha256>"
  },
  "byte_start": 123,
  "byte_end": 127,
  "source_sha256": "<paper-bytes[start:end]-sha256>",
  "occurrence_rank": 0,
  "kind_payload": {
    "numeric_role": "claim_numeric",
    "number_lexeme": "0.95",
    "unit_lexeme": null
  }
}
```

`obligation_id` is `obl-` plus the full SHA-256 of the canonical identity JSON
bytes. Producer, loader, and reconstruction reject missing, unknown, duplicate,
or reordered identity fields and independently derive every ID.
`kind_payload` is exact per kind: numeric uses `{numeric_role, number_lexeme,
unit_lexeme}`; citation uses `{cite_key, marker_byte_start, marker_byte_end,
marker_sha256, key_rank}`; comparative uses `{matched_terms}` in source order;
declarative uses `{selected_section_class}` with exactly `results`,
`discussion`, or `contributions`.

Claim-obligation policy v1 fixes the sentence and comparison grammar rather
than delegating it to an NLP model. It requires `markdown-it-py==4.2.0`, the
`commonmark` preset, and no plugins; a runtime version/profile mismatch fails
before inventory construction. It uses `parse_manuscript(strict=True)` for the
heading/section sequence and scans CommonMark paragraph and list-item source
blocks. Core CommonMark has no table or caption extension, so v1 does not
invent table-cell or caption blocks. Pipe-table syntax remains paragraph text;
an extension-specific table/caption interpretation requires a new policy
version. Fenced/indented code, inline code, link destinations, raw HTML, and
bibliography entry bodies are not declarative-sentence blocks. A sentence
ends at `.`, `?`, or `!` followed by whitespace or block end, after protecting
the policy's fixed abbreviation set (`e.g.`, `i.e.`, `et al.`, `Fig.`, `Eq.`,
`Sec.`, `Dr.`, `Mr.`, `Ms.`, `vs.`). The final nonempty block fragment is also
an obligation. A declarative sentence is every such nonempty sentence in the
selected sections; the producer does not ask an LLM whether it is declarative.
Abbreviation matching is ASCII-case-insensitive after collapsing internal ASCII
space and does not cross a line ending. The comparison grammar uses
ASCII-case-insensitive whole-word matching for exactly `better`, `worse`,
`higher`, `lower`, `greater`, `less`, `outperform`, `outperforms`,
`outperformed`, `improve`, `improves`, `improved`, `reduce`, `reduces`,
`reduced`, `increase`, `increases`, `increased`, `decrease`, `decreases`,
`decreased`, `versus`, `vs.`, `compared with`, and `compared to`; punctuation
cannot substitute for a word boundary. The selected heading aliases, after the
identity normalization above, are exactly: Results = `results`, `experimental
results`, `evaluation results`, `findings`; Discussion = `discussion`, `results
and discussion`, `discussion and implications`; Contributions =
`contributions`, `main contributions`, `contributions and impact`. Changing
blocks, aliases, abbreviations, headings, or comparison tokens requires a new
claim-obligation policy version.

Raw offsets are constructed without source-text search. The shared offset
builder rejects a UTF-8 BOM and bare CR, strict-decodes once, preserves CRLF as
two source bytes, accepts LF and a missing final newline, and builds (a) a
monotonic line-start byte table by scanning raw bytes and (b) a
codepoint-to-byte boundary table by cumulatively encoding each decoded code
point. The `parse_manuscript` line ranges are converted only through those
tables. `bytes.find()`, `str.find()`, normalized-text search, and first-match
recovery are forbidden for span identity. Repeated sections and repeated
sentences therefore retain distinct offsets.

Citation-instance policy v1 recognizes only bracket markers whose complete
content is one or more canonical cite keys separated by comma or semicolon, and
LaTeX `\cite{...}` markers with comma-separated canonical keys; surrounding
ASCII space is permitted. Each key produces one `citation_instance` obligation.
Its source span is the exact key substring excluding marker punctuation and
space, while separate `marker_byte_start`/`marker_byte_end` fields bind the
whole marker. A multi-key marker therefore yields one independently ordered
obligation per key. Repeated keys at different source positions remain distinct
instances. A malformed marker, author-year prose, or unknown key is not guessed
into a citation and is rejected by the existing citation-closure policy when it
occupies citation syntax.

An LLM may return only classification and bounded explanation for the exact
ordered obligation-ID closure. It cannot add, omit, merge, split, reorder, or
rewrite an obligation, source span, text, numeric token, citation key, or type.
The deterministic `obligation_kind` is immutable. The LLM-provided
`claim_class` uses exactly `quantitative`, `comparative`, `result`, or
`citation` and cannot confer support; unknown classes are rejected rather than
rewritten to `result`. The strict
claims ledger contains exactly one row per obligation and disk replay rebuilds
the inventory from Stage 23 paper bytes before accepting the ledger.

Claim support policy v1 is conservative:

- summary, analysis, attempt-log, or artifact existence is never claim support;
- every quantitative or comparative obligation closes each deterministically
  parsed numeric token to a specific canonical metric/condition/observation
  identity under the exact unit grammar below. Equality with an otherwise
  unrelated canonical value is not support;
- a comparative obligation with no `claim_numeric` token is always unsupported
  in policy v1. An empty token collection never establishes support through
  `all(empty)` or an equivalent vacuous predicate. A numeric comparison is
  supported only when its exact bound observations and comparison direction
  deterministically establish the stated relation; value closure alone is not
  relation closure;
- every citation obligation requires both the Stage 23 `VERIFIED` identity
  record and the matching citation-instance support closure;
- generic result, discussion, or contribution obligations default to
  `unsupported`;
- only a valid `GenericClaimSupportRecord` bound to the same obligation ID,
  source span/hash, paper generation, evidence closure, critic identity, and
  policy may make a generic obligation supported;
- an LLM verdict cannot independently change unsupported to supported.

For every `declarative_sentence` obligation, the generic-support input builder
constructs an exact ordered candidate-evidence set only from canonical
structured result observations and already replayed citation/numeric/comparison
support records whose source spans overlap that sentence. Summary/analysis
prose, attempt logs, and artifact existence are excluded. Numeric, citation,
or comparative closure for the same span does not automatically support the
remaining prose. With no candidate evidence, the obligation remains
unsupported without an LLM call.

Otherwise, `generic_assessment_input_sha256` hashes exactly
`{schema_version, policy_version, canonical_manifest_sha256, paper_sha256,
obligation_id, byte_start, byte_end, source_sha256, evidence_records,
critic_model}`. Each evidence record has exactly `{evidence_kind,
authority_path, authority_sha256, semantic_pointer, semantic_value_sha256}` and
the array is sorted by that tuple. The source record has exactly
`{schema_version, assessment_id, assessment_input_sha256, critic_model,
policy_version, verdict, reason}`, with verdict `supported` or `unsupported`.
It uses the fixed generic-support namespace and the same isolation, identity,
strict-loader, and independent-replay rules as citation assessments. This
record is the only v1 semantic support producer for residual declarative prose.

The numeric grammar is one versioned pure function shared by inventory
production, claim parsing, support matching, strict loaders, and replay. JSON
authority values use `json.loads(..., parse_float=Decimal,
parse_int=Decimal)`. Booleans, strings masquerading as numbers, NaN, infinity,
and nonfinite Decimal values are rejected. Manuscript numeric tokens, including
scientific notation, are parsed directly to finite `Decimal` without binary
float conversion. Canonical numeric authority uses exact Decimal equality and
never tolerance, rounded aliases, or display-rounded fallback. Unit transforms
are explicit and versioned: for example, a `%` token is divided by
`Decimal("100")` only when the bound canonical observation has ratio units.
Unitless and percent forms are not interchangeable without that declared
transform.

Metric units are authority, not inferred presentation metadata. The sealed
Stage 9 experiment contract contains an exact `metric_units` mapping whose keys
close over every metric key the Stage 12/13 evaluator may publish and whose
values are from `ratio`, `percent`, `count`, `seconds`, `milliseconds`,
`microseconds`, `nanoseconds`, `bytes`, and `unitless`. Stage 10 binds that
contract; result-set replay rejects an observed metric with a missing or
unknown unit, an extra unit key, or a unit conflicting with the evaluator's
domain-owned metric schema. Summary, analysis, manuscript, and LLM output are
never unit sources. A percent manuscript token maps to a `ratio` observation by
exact division by 100, maps to a `percent` observation without scaling, and is
invalid for every other unit. Other cross-unit transforms are forbidden in
policy v1. Decimal negative zero is normalized to Decimal zero for mathematical
comparison while the obligation retains the exact original token bytes and
hash.

Numeric-token policy v1 uses the exact lexical form
`(?<![A-Za-z0-9_])(?P<number>[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?)(?P<unit>%|[ \t]?(?:ns|us|\u00b5s|ms|s|bytes))?(?![A-Za-z0-9_])`.
The pre-scan is a byte-defined state machine. At an ASCII boundary whose prior
byte is not `[A-Za-z0-9_]`, a candidate starts with a digit, a sign followed by
a digit or dot-plus-digit, or a dot followed by a digit. It consumes the
maximal contiguous ASCII run from `[0-9,+.eE-]`. It may remove exactly one final
`,` or `.` as prose punctuation only when that byte is followed by EOF or ASCII
whitespace and the remaining run fully matches the numeric-core grammar. The
remaining complete run must match the `number` group above; otherwise the
entire paper is invalid for policy v1 and the scanner does not recover
submatches. A valid core may then consume at most one ASCII space or tab plus
one exact unit lexeme, followed by EOF or a non-`[A-Za-z0-9_]` boundary.
Thousands commas are removed only after this full-run validation. Thus `1,000`
is one value, `1,000,` followed by whitespace is one value plus punctuation,
`1.` is integer one plus sentence punctuation, and `1,,000`, `1,00abc`,
`12,34,567`, `1e`, or `1-2` reject rather than split.
Unit lexemes map exactly to the closed Stage 9 units; `us` and `µs` both map
to `microseconds`. A missing lexeme can match only `ratio`, `count`, or
`unitless` when the same sentence has exactly one deterministic metric display
label and the observation-bound support record fixes which unit applies.
Metric display labels are a second exact Stage 9 contract mapping owned by the
domain evaluator; unknown, duplicate-across-metric, missing, or LLM-supplied
labels are invalid. A numeric sentence with zero or multiple matching metric
labels is unsupported in policy v1.
Every match receives a deterministic lexical role. A token wholly inside a
citation marker/key, link destination, DOI/arXiv identifier, heading ordinal,
equation/figure/table label, or bibliography metadata field is
`identifier_metadata`; all other matches are `claim_numeric`. The protected
contexts are derived from the same CommonMark/source grammar and cannot be
selected by the LLM. An ambiguous token defaults to `claim_numeric`.
`identifier_metadata` remains in the obligation ledger and must replay its
exact context, but it is not matched to an experiment observation.

Stage 24 publication follows this order:

1. open a directory-fd-bound `stage-24` namespace;
2. as the first operation, invalidate `stage24_truth_manifest.json` and clear
   every owned success name;
3. capture one `Stage24InputBundle`;
4. build the deterministic obligation inventory;
5. create all proposed outputs in same-filesystem staging;
6. strict-read and semantically replay every staged output;
7. perform the complete source fixpoint;
8. publish the success files;
9. write `stage24_truth_manifest.json` last;
10. replay the manifest and publication from disk; and
11. perform the complete source fixpoint again.

The manifest binds the raw Stage 23 paper hash, Stage 23 publication,
Stage24InputBundle identity, critique publication, obligation inventory,
claims ledger, citation-support assessments, citation-instance mapping,
critique resolution, truth-audit result, exact output namespace, and every raw
output hash. `truth_audit.json` is a bound result, not the commit point. Any
exception clears the success namespace and manifest. Optional diagnostics live
under a separate `stage-24-diagnostics/` non-authoritative namespace, are
cleared on the next attempt, never enter the manifest, and cannot coexist with
a successful publication.

Stage 24 has `input_files=()` and `max_retries=0`; its strict bundle loader is
the only input selector.

`stage24_success` is a pure derived predicate and is true exactly when:

- critique state is `model_final` or `external_final`;
- every P0/P1 critique finding has exactly one resolution allowed by release
  policy (`fixed` or `rebutted`), with no orphan or duplicate resolution;
- when the paper contains any audited prose block or inventory candidate, the
  deterministic obligation inventory is nonempty;
- every support-required obligation is supported and `unsupported_count == 0`;
- citation-support replay is valid and has no missing, orphan, duplicate, or
  unsupported citation instance;
- `dataset_claim_violations` is exactly empty; and
- every schema, publication replay, namespace closure, source fixpoint, and
  post-manifest check succeeds.

`identifier_metadata` numeric obligations are deterministically marked
non-support-required but remain in the exact ledger. Every other obligation is
support-required. `none_final`, `external_pending`, a zero inventory over
audited source blocks, any unsupported obligation, any unresolved P0/P1,
invalid citation support, or any dataset-claim violation makes
`stage24_success` false. A false predicate returns Stage FAILED, publishes no
`stage24_truth_manifest.json`, removes every success-named Stage 24 output, and
may retain only the separate non-authoritative diagnostics described above.

#### C4-A2: Stage 25 publication

- give Stage 25 `input_files=()` and `max_retries=0`;
- add an immutable `Stage24PublicationSnapshot` as its sole authority input;
- remove direct reads of `truth_audit.json` and all paper reselection;
- publish `deai_audit.json` under a directory-fd-bound namespace and write
  `stage25_deai_manifest.json` last;
- bind the Stage 24 manifest/path/hash, raw paper bytes/hash, exact Stage 24
  output closure, DEAI policy version, and `deai_audit.json` hash;
- invalidate old authority before loading Stage 24, then require staged replay,
  source fixpoint, manifest-last publication, post-manifest replay, and a final
  source fixpoint. Once a Stage 25 attempt acquires its fd-bound namespace, it
  invalidates the previous manifest as its first operation; every later failure
  leaves no Stage 25 authority. A failed attempt never preserves a prior valid
  generation in the live namespace.

After C4-A2 receives independent approval, a narrow activation commit may set
only `stage24_release_consumers` to v1. External/persistent consumers and
independent release reconstruction remain zero, so the complete runtime stays
blocked.

#### C4-B: independent release reconstruction

- add `reconstruct_expected_canonical_evidence(run_dir)` and independent Stage
  15/23/24/25 publication reconstruction using only strict run-local disk
  inputs;
- reconstruct the deterministic claim-obligation inventory and all numeric,
  citation, critique, and publication semantics without importing producer
  objects or accepting stored winner/status/reason fields as authority;
- before expected reconstruction is complete, treat stored manifests only as
  ordinary regular-file bytes. Do not parse or trust a stored winner, metric,
  reason, verdict, resolution, or status to choose reconstruction inputs;
- non-deterministic critic output is a strict, hash-bound source record rather
  than a reproducible computation. Reconstruction never calls an LLM. After it
  independently reconstructs the complete assessment input graph and verifies
  source-record identity/isolation, it may read the raw assessment record's
  verdict/reason or resolution/note under the trusted isolated-critic
  assumption. It then deterministically derives expected support states,
  resolution states, counts, `stage24_success`, and manifest fields. A verdict,
  status, count, reason, or resolution copied into a producer manifest or
  derived report is never an oracle;
- compare the complete expected publications byte-for-byte and field-for-field
  with stored manifests, then bind release packaging and registry artifacts;
- migrate `release_check.py` and external/persistent consumers to this
  reconstruction result and add the complete adversarial release suite;
- raise `external_and_persistent_consumers` and
  `independent_release_reconstruction` only in their own independently approved
  commits.

This chain proves run-local byte binding and deterministic closure under the
trusted producer and isolated-critic assumptions. It does not claim that a
hash proves scientific truth, that an external critic is organizationally
independent, or that natural-language support outside the deterministic
obligation grammar is complete.

### C5: activation

- verify all component versions equal v1;
- run targeted and full suites with the simulated partial-state tests;
- activate the runtime capability registry in one narrow commit;
- do not combine DOI/title verification changes.

### C6: validation runs

1. run targeted and full suites;
2. run a fresh `pipeline_validation` Stage 1-25 directory with no resume;
3. confirm canonical replay passes while release remains nonzero only for the
   expected non-release identity or environmental findings;
4. run a fresh `research_release` candidate only after all scope-specific data,
   evaluator, evidence, and full-text prerequisites are satisfied;
5. archive complete findings and manifest hashes.

Commits C0-C5 may be reviewed separately and must land in order. The runtime
gate, not an operational instruction, makes intermediate checkouts incapable of
running a mixed Stage 12+ authority path.

## 13. Required Adversarial Tests

At minimum:

1. current Stage 12 result differs from stale root `best` copy: stale copy is
   rejected and never selected;
2. Stage 12 baseline is `0.5` and accepted Stage 13 refinement is `0.8`: the
   tagged refined result is selected and replays;
3. selected Stage 13 refined code, sandbox result, runtime-repair result, and
   refinement log are each mutated independently: reject;
4. a rejected Stage 13 iteration is deleted or reclassified accepted: reject;
5. direct Stage 14 summary and versioned Stage 14 analysis are mixed: reject;
6. selected summary hash is changed while manifest is unchanged: reject;
7. manifest and summary hash are changed but selected result source is unchanged:
   deterministic grounding rejects the unsupported metric;
8. one Stage 12 evidence file is deleted: reject;
9. an extra Stage 12 evidence file, temporary file, directory, or symlink appears:
   reject;
10. raw sandbox/workspace diagnostics exist outside the evidence namespace:
    ignored; moving one into the namespace without manifest update rejects;
11. Stage 12 attempts to choose among sandbox outputs by mtime: guard test
    rejects the implementation;
12. canonical Stage 9 selector returns `stage-09_v1` while Stage 10 manifest
    declares direct `stage-09`: reject;
13. Stage 10 manifest has unknown/duplicate fields, unsafe path, symlink,
    unmanifested file, or wrong contract path/hash: reject;
14. `collider_agent` attempts to publish canonical schema v1 evidence: reject in
    both pipeline-validation and research-release scopes;
15. sealed-candidate manifest or one selected candidate file changes: reject;
16. repository config changes after the run: replay still uses the bound
   run-local snapshot;
17. a resume creates a different config path with byte/semantic-identical
    content: existing evidence remains valid;
18. active config changes only metric direction, mode, evaluator, scaffold,
    dataset, or another semantic field: reject and require Stage 9 rebuild;
19. active pointer, history, or checkpoint is independently changed: reject;
20. claim scope or dataset origin is edited only in a manifest: reject;
21. exact metric and substring metric coexist: only exact key participates;
22. missing exact metric with only substring aliases: reject;
23. `-0.0`, high-precision Decimal, exponent notation, and equal values with
    different textual forms follow Section 6.1 exactly;
24. primary metric or optimization direction is edited: reject;
25. two eligible candidates tie: winner is independent of filesystem order;
26. candidate staging runs twice with identical payload: deterministic full
    candidate IDs match and bytes must be identical;
27. candidate ID is influenced by timestamp, mtime, producer field, or directory
    enumeration: guard test rejects the implementation;
28. a stale-provenance candidate advertises any score: exclude it, then select
    among current-generation eligible candidates;
29. producer selects a non-winning same-generation candidate while every stored
    hash is self-consistent: full-ID tie-break reconstruction rejects it;
30. no eligible candidate exists: fail with no fallback;
31. Stage 14 rerun fails after writing new summary but before charts: old chart,
    figure plan, table, or auxiliary file cannot enter the new candidate;
32. canonical summary copy differs by one byte from selected source: reject;
33. canonical analysis copy belongs to another candidate: reject;
34. distinct evidence paths with byte-identical content and duplicate hashes are
    accepted when all other rules pass;
35. rollback to Stage 12 leaves old canonical manifest: resume at Stage 17
    fails before LLM calls;
36. rollback to Stage 13 preserves valid baseline but invalidates refinement and
    candidate selection;
37. Stage 14 rerun leaves old candidate manifest: producer/replay rejects it;
38. resume directly at Stage 19 with missing canonical manifest: fail;
39. versioned Stage 14 archive mutates after selection: replay rejects it;
40. publication is interrupted before and after each compatibility-copy and
    manifest replace: every observed state fails closed or resolves wholly old/new;
41. a source is replaced after loader validation: consumers still use captured
    validated bytes and do not reopen it;
42. malformed JSON, duplicate keys, unknown fields, invalid UTF-8, unsafe path,
    bool-as-int, NaN, and infinity are rejected;
43. modification times are reordered: selection does not change;
44. downstream closure report binds a different canonical manifest hash:
    release fails;
45. canonical-evidence error plus a graceful-degradation signal still exits
    with release failure;
46. the same evidence tamper fails in both pipeline-validation and
    research-release scopes;
47. legacy run without canonical manifest cannot release;
48. producer output is parsed by the same strict loader before Stage DONE;
49. consumer guard detects direct globs, `_read_best_analysis`, registry,
    release-artifact, repair, and generic best/latest/prior helper regressions;
50. each simulated C0, C1, C2, C3, and C4 capability map fails through both CLI
    and Python entrypoints before stage side effects;
51. a coherent full-chain rewrite is documented as outside the hash-only threat
    model and is not misrepresented as cryptographically prevented.
52. an identity-bound summary, figure plan, table, analysis, or chart metadata
    embeds the candidate ID, identity digest, candidate-manifest digest,
    canonical root-manifest path, or final candidate-directory prefix: reject;
53. a complete candidate parent moves from `stage-14` to `stage-14_v1` without
    changing candidate-manifest bytes: candidate-root-relative paths still
    replay when all upstream provenance remains valid;
54. publication stops after identity-bound artifacts are complete but before
    the candidate manifest is written: no directory appears in the formal
    `evidence_candidates/cand-*` namespace;
55. repository guards detect direct experiment-evidence access newly introduced
    in `hitl.claim_verifier`, `copilot.branching`, HITL summarization, or the TUI;
    the TUI passes only when call-graph tests prove diagnostic-only isolation.
56. Stage 10 is sealed under semantic config A, then Stage 12 runs under semantic
    config B while the contract hash remains unchanged: reject before execution;
57. an instrumented sandbox callable fails on its first invocation; a second
    lease acquire raises before the callable count increments, and no result-set
    manifest is published;
58. a successful journal has exactly one started/terminal pair; deleting,
    appending, reordering, retokening, reindexing, or changing either journal
    record or `execution_statuses` causes replay failure;
59. candidate manifest exists and staging replay succeeds, but publication
    stops before rename: the formal candidate namespace remains unchanged;
60. candidate staging and destination are forced onto different filesystems:
    `EXDEV` fails hard and no copy/delete fallback occurs;
61. structured candidate artifacts encode a forbidden candidate identity or
    path with JSON Unicode escapes: decoded recursive validation rejects it;
62. repository guards detect evolution runtime scans, raw trajectory
    persistence, experiment-memory root reads, MCP result reads, runner rollback
    or preflight omissions, and legacy report result selection;
63. an unbound or stale experiment-derived evolution lesson cannot enter a
    later prompt or MetaClaw skill, even when all lesson text is syntactically
    valid;
64. FigureAgent Decision, Planner, CodeGen, Renderer, Critic, and Integrator
    receive one bound in-memory generation and every emitted file enters the
    same immutable candidate closure;
65. MCP `get_experiment_results` returns an explicit error for missing/invalid
    canonical evidence and never falls back to root `experiment_results.json`;
66. rollback invalidates the canonical root manifest before copying or renaming
    any Stage 12-14 directory, including interruption at each boundary.
67. every C0-C4 partial capability map makes MCP `get_experiment_results`
    return migration-incomplete before any run file is read;
68. the report CLI, direct `generate_report()`, and publisher public/helper
    entrypoints reject every partial capability map before reading parseable
    legacy root/direct/versioned artifacts;
69. direct evolution, overlay, MetaClaw, and experiment-memory persistence or
    retrieval calls under an incomplete capability map or missing canonical
    binding perform no lesson, skill, memory, or prompt write;
70. repository guards reject a canonical Stage 12 implementation that calls
    sandbox `run`/`run_project` without the controller-owned lease wrapper;
71. in a black-box fixture where a hidden wrapper would fail once then succeed,
    the controller journal prevents the second execution and release replay
    cannot observe a success-only history.
72. a paper contains two unsupported numeric claims while the classifier returns
    only one obligation: exact inventory closure rejects the ledger before
    support assessment;
73. a comparative sentence or a declarative Results/Discussion/Contributions
    sentence is omitted, merged, retyped, reordered, or rewritten by the LLM:
    strict obligation replay rejects it;
74. the paper contains multibyte UTF-8 before a numeric token or citation:
    producer and replay derive identical raw-byte offsets and IDs; changing one
    byte without changing visible normalized text rejects;
75. an unknown claim type is returned: reject rather than coercing it to
    `result`;
76. a citation-support producer attempts to reopen citation plan, cards,
    verification report, contract, or config after bundle capture: repository
    guard and call-site tests reject it;
77. a citation assessment changes card hash, excerpt hash, citation instance,
    claim span, critic model, policy version, or assessment-input hash: replay
    rejects;
78. Stage 15 decision or canonical evidence changes after critique generation:
    critique v2 and Stage 24 replay reject the stale critique;
79. external review is appended directly to `critique.json`, lacks its strict
    structured artifact, targets the wrong canonical/decision generation, uses
    an unsafe or colliding structured/prose path, changes during finalization,
    or supplies duplicate finding IDs: no finalized critique manifest is
    accepted;
80. a Stage 24 rerun fails during bundle loading, inventory construction, LLM
    assessment, staging replay, or either source fixpoint: an old truth manifest
    and every success-named Stage 24 output are absent; synchronous output plus
    manifest-hash forgery, post-manifest disk corruption, and parent replacement
    by an external symlink also reject without touching the external target;
81. two paper byte strings normalize to the same whitespace-collapsed text but
    differ in raw bytes: Stage 24/25 identity and fixpoint reject substitution;
82. high-precision Decimal, exponent notation, negative zero, percent/ratio
    conversion, wrong/missing/conflicting unit authority, boolean, NaN,
    infinity, and a display-rounded near match follow the single exact
    numeric/unit grammar in producer, parser, and replay;
83. summary, analysis, attempt log, or artifact presence exists without an
    obligation-bound `GenericClaimSupportRecord`: generic result/contribution
    remains unsupported;
84. a citation marker is verified but its claim-instance assessment is missing,
    mismatched, or unsupported: the citation obligation cannot be supported;
85. Stage 25 sees a shadow/versioned paper, direct truth-audit replacement,
    missing Stage 24 manifest, or changed Stage 24 output after snapshot:
    reject without publishing a DEAI manifest;
86. Stage 25 fails before input load, during render, before manifest replace,
    after manifest replace, or during final fixpoint: after namespace acquisition
    every failure leaves no Stage 25 authority; synchronized output/manifest
    forgery and fd-bound parent replacement also reject without writing an
    external target;
87. independent reconstruction is presented a self-consistent stored manifest
    with a forged winner, metric, reason, verdict, resolution, or status: it
    selects and derives expected authority without consulting those fields and
    rejects the stored publication;
88. every partial C4 map, including `stage24_release_consumers=v1` with either
    `metric_contract_authority`, external/persistent consumers, or independent
    reconstruction still zero, remains blocked at all public entrypoints before
    run artifacts are read.
89. the ledger is complete but one support-required obligation is unsupported,
    critique is `none_final`, a P0/P1 is unresolved, citation support is invalid,
    or a dataset claim violation exists: `stage24_success` is false and no truth
    manifest or success-named output is published;
90. `Our method outperforms the baseline.` and equivalent numeric-free
    comparison forms are unsupported in policy v1 and cannot pass through an
    empty-token predicate;
91. producer and replay receive duplicate identical sentences, duplicate
    section bodies, multibyte text, CRLF, missing final newline, Setext headings,
    and inline-code numerics: exact obligation offsets, kinds, order, and IDs
    agree or the explicitly unsupported syntax rejects;
92. BOM and bare-CR input, an unpinned markdown-it version/profile, a table or
    caption plugin, unknown heading alias, or changed abbreviation/comparison
    grammar cannot be silently interpreted under policy v1;
93. an LLM returns the exact obligation IDs but changes kind, occurrence rank,
    section identity, span, order, source hash, or adds an implementation-defined
    fifth obligation kind: strict ledger replay rejects;
94. citation and resolution raw assessment records remain unchanged while a
    producer report or manifest changes supported/status/count/reason/resolution:
    independent reconstruction rederives expected values from the bound source
    records and rejects the derived forgery without calling an LLM;
95. the same raw critic record is paired with a different assessment-input
    payload, writer-equal model, policy version, evidence excerpt, finding hash,
    or paper generation: identity/isolation replay rejects;
96. a metric unit is added only by summary, analysis, manuscript, or LLM output,
    or an observed metric lacks an exact Stage 9/domain-evaluator unit mapping:
    the numeric obligation remains unsupported and Stage 24 cannot publish.
97. a resolution LLM returns `accepted-risk` for an unresolved P0/P1 without a
    separately versioned human-authorization policy: strict source parsing
    rejects the enum and no Stage 24 manifest is published;
98. an expected citation/generic/resolution assessment is renamed, shadowed,
    duplicated under another filename, omitted, nested, symlinked, or joined by
    an extra file: independent namespace discovery rejects without consulting
    the stored Stage 24 manifest;
99. numeric and citation obligations for a sentence are supported while its
    overlapping declarative obligation has no valid generic support record:
    the declarative obligation remains unsupported and prevents publication;
100. contract schema v1 evidence is presented to metric policy v2, a v2 Stage 9
    contract is mixed with v1 Stage 10-14 evidence, or an old run resumes at
    Stage 12 after C4-U0: capability/version and replay checks reject before
    execution or claim assessment;
101. `[key1; key2]`, `\cite{key1,key2}`, repeated keys, malformed multi-key
    markers, and key/marker span tampering follow the per-key citation-instance
    closure exactly;
102. `1,000`, malformed `1,00`, sentence-final `1.`, `.5`, exponent notation,
    `10ms`, and `10 ms` produce the exact policy-v1 token/span/Decimal result or
    reject without splitting a malformed token into apparently valid values;
103. critique state records vary each required, forbidden, null, enum, and
    nested field; only the four literal schemas pass, and pending/external
    namespaces retain exact closure;
104. generic support evidence is drawn from summary/analysis prose, an attempt
    log, an unbound value-equal observation, or an overlapping child closure
    without residual-prose assessment: strict input derivation rejects or keeps
    the declarative obligation unsupported.
105. an assessment record changes `critic_model`, recomputes its input hash and
    assessment ID, and renames the file consistently: the pre-captured
    active-config model projection still rejects it before source-record
    semantics are consumed;
106. contract v2 self-declares units/labels without the fixed run-local registry,
    changes registry owner/evaluator/hash, duplicates a normalized label across
    metrics, changes array order, or emits a metric outside the registry: Stage
    9-14 producer/replay and the U0 expected helper reject;
107. the mandatory capability registry omits or zeros
    `metric_contract_authority`, or raises it while any U0 producer/loader/replay
    remains v1: every guarded entrypoint fails before artifact access;
108. C4-B reconstructs Stage 9-14 metric authority by extending the independently
    approved U0 helper; a substitute release-only selector or dependence on
    `independent_release_reconstruction` during U0 is rejected by call-site and
    partial-map tests.
109. two valid metric registries A and B exist; an attacker selects B for an
    A-domain topic and synchronously rewrites the run-local domain-selector
    policy snapshot, profile, index, registry, contract, Stage 10-14 hashes,
    and stored domain/evaluator IDs so that the run-local chain selects B: the
    U0 expected helper first loads the trusted package domain-selector policy,
    independently derives A from the active-config topic, then reruns the tuple
    selector and rejects the coherent B chain. Recomputing every run-local hash
    cannot make the substituted domain-selector policy an oracle.

## 14. Separate Stage 23 DOI/Title Workstream

The observed citation-verification path has two independent weaknesses:

1. a DOI 404 can terminate provider evaluation as `HALLUCINATED`, preventing a
   title-based provider such as OpenAlex from contributing evidence;
2. title-only relevance checking can become a second semantic authority that
   conflicts with Stage 5's abstract-based screening.

These issues must not be mixed into the canonical experiment-evidence commits.
They require a separate narrow specification and implementation:

- provider aggregation continues after a DOI-specific negative result;
- exact title identity with invalid DOI yields at least `SUSPICIOUS`, never
  silently verified;
- all providers reporting no identity match yields `HALLUCINATED`;
- network/provider unavailability yields `SKIPPED`, not a fabricated verdict;
- cache identity binds normalized title, DOI, arXiv ID, and policy version;
- Stage 5 remains semantic-screening authority;
- Stage 23 title-only relevance is diagnostic and cannot override Stage 5 or
  relax citation-support closure;
- existing thresholds, Stage 19, E9, and release gates remain unchanged.

## 15. Review Questions

An independent reviewer should answer:

1. Does the artifact graph contain any circular self-proof?
2. Does Stage 13 refinement provenance bind code, executions, rejected attempts,
   and deterministic selection without trusting `refinement_log.json`?
3. Can summary, analysis, tables, or charts from different Stage 14 attempts be
   made to pass?
4. Can a stale or partial Stage 12/13 generation remain eligible after rollback?
5. Is canonical evidence correctly unavailable for unsupported experiment modes?
6. Does semantic config identity handle same-content resume snapshots safely?
7. Is candidate ID derived only from the canonical path-independent payload?
8. Are metric path, Decimal parsing, aggregation, direction, and ties exact?
9. Is immutable publication safe under interruption and in-process TOCTOU?
10. Does every authoritative and release consumer migrate to one accessor?
11. Can a direct resume bypass canonical preflight?
12. Does release reconstruction independently reselect the winner without using
    producer selection fields as an oracle?
13. Are intermediate migration commits mechanically prevented from running
    incompatible mixed consumer paths?
14. Does the document state the SHA-only threat-model limit accurately?
15. Is the Stage 23 DOI/title workstream correctly isolated?
16. Does Stage 10 bind the exact producer config and share one semantic identity
    with Stage 12-14 even when the contract is unchanged?
17. Does Stage 12 policy v1 enforce exactly one invocation and refuse to publish
    after failed, partial, timed-out, or retried execution?
18. Can evolution, trajectory, experiment memory, MCP, report, or FigureAgent
    paths reintroduce unbound experiment evidence into prompts or external output?
19. Do all external/persistent entrypoints enforce the complete capability map
    locally before any artifact read or durable write?
20. Does the controller-owned invocation journal plus direct-call guard make a
    hidden failed-then-successful Stage 12 retry observable and invalid?

## 16. Acceptance Rule

Implementation is accepted only when:

- all schemas and replay rules are implemented with strict loaders;
- Stage 10 producer-input hardening and Stage 12/13 isolated evidence namespaces
  are complete;
- one semantic config identity is replayed across Stage 10-14;
- Stage 12 policy v1 proves a single completed invocation with no retry or
  partial-result publication;
- producer artifacts self-validate before Stage DONE;
- all authoritative consumers use the shared accessor;
- persistent evolution/memory and external MCP/report/FigureAgent paths cannot
  reintroduce unbound experiment evidence;
- repository guard tests find no unauthorized direct selection paths;
- controller-owned Stage 12 invocation journals replay exactly and direct
  sandbox calls outside the one-acquire lease are mechanically rejected;
- the runtime capability registry is complete and active only after all
  consumers and release reconstruction reach the same version;
- every external/persistent entrypoint applies the deny-only capability guard
  locally before artifact access during C0-C4;
- rollback and resume tests prove stale evidence is unusable;
- release audit independently reconstructs selection and provenance;
- targeted and full suites pass;
- a fresh, single-version Stage 1-25 validation run completes without canonical
  experiment-evidence findings;
- no frozen Stage 19, E9, citation, quality, or release gate is weakened.

Until those conditions hold, the current mixed-source run remains diagnostic
only and cannot establish scientific support or release readiness.
