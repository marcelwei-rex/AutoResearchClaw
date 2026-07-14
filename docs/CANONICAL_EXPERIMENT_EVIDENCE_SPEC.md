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
- C3-E: add the complete call-site and forbidden-scan guard, then set
  `stage19_22_consumers` to v1 only after Stage 18 through Stage 22 pass it.
- ensure each migrated manifest or closure report records the canonical-evidence
  manifest hash;
- keep the runtime capability gate blocked.

### C4: Stage 24 and release replay

- migrate Stage 24 provenance;
- add `reconstruct_expected_canonical_evidence(run_dir)` and exact comparison;
- bind release packaging and registry artifacts;
- add all adversarial release tests.

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
