# TrojNet Canonical Evaluator Migration Specification

Status: design draft for independent review
Baseline: `ec3725f49e914044d6520e75a8e1d909d08c8b7f`
Target: Stage 9-14 canonical evaluator support for the frozen TrojNet ISCAS-85
pipeline-validation workload

## 1. Purpose and boundary

The audited fixture proves that the TrojNet workload can produce a complete
matrix of baselines, proposed-method results, three seeds, and eight metrics.
It is not canonical authority. This migration must not copy the fixture's two
remaining diagnostic assumptions into the production evidence graph:

1. path/hash replay is not an immutable input capture; and
2. a self-consistent result object is not proof that observations came from an
   executed evaluator.

The canonical evaluator is restricted to:

```text
claim_scope   = pipeline_validation
dataset_origin = synthetic
dataset_name   = controlled_synthetic_iscas85_trojan_localization_v1
experiment_mode = sandbox
```

It is never `research_release` evidence. Adding public or measured hardware
data requires a new evaluator ID, dataset authority, and policy version.

This document changes no Stage 19, E9, Stage 24/25, release-check, or release
policy semantics.

## 2. Non-negotiable invariants

### 2.1 Selection

- The research topic, trusted package selector policy, experiment mode, and
  versioned selector index are the only evaluator-selection inputs.
- Stage 9 plan text, LLM output, hypotheses, literature, environment variables,
  and stored evaluator IDs cannot select or override the evaluator.
- Hardware-Trojan localization has a distinct canonical domain ID. It must not
  be represented as `security_detection/hpc_anomaly_detection`.
- Every stored domain, evaluator, metric, package, and execution-policy identity
  is independently re-derived during Stage 9-14 replay.

### 2.2 Immutable input capture

- Stage 10 captures evaluator source, vendored source, frozen data, execution
  policy, verifier source, and runtime projection into a run-local immutable
  namespace.
- Capture opens every source-path component relative to held directory fds with
  `O_DIRECTORY | O_NOFOLLOW`, opens the final file with `O_NOFOLLOW`, validates
  regular-file identity with `fstat`, and copies from the opened fd. It never
  performs `is_file()` followed by path-based `read_bytes()`.
- The package manifest defines an exact source namespace. A held-fd walk must
  equal the declared file and directory closure; any undeclared regular file,
  directory, `__pycache__`, symlink, or special entry rejects.
- The captured namespace is exact and manifest-last. Symlinks, devices, FIFOs,
  sockets, missing files, duplicate targets, extra authority files, and hash
  mismatches reject.
- A source path may change after capture without affecting the run. Stage 12-14
  consume only captured bytes and never reopen package or fixture data paths.
- A source mutation during capture either yields the predeclared bytes or fails
  the package-manifest hash. A mutate-then-restore path cannot authorize bytes
  that were not captured and verified.

### 2.3 Trusted execution evidence and independent metric authority

- Evaluator-produced summaries and metrics are not authority.
- Stage 12 executes the captured evaluator twice in separate fresh invocation
  workspaces under a predeclared two-invocation policy. There are no retries.
- In v1, `sandbox` is the existing host subprocess backend. The evaluator is a
  trusted, hash-bound, code-owned package. Two executions establish deterministic
  output consistency under that trust assumption; they do not prove hostile
  process isolation, cache absence, network isolation, or fresh recomputation.
- No release or scientific-support claim may describe these two runs as
  independent execution proof. A future isolated-execution policy requires a
  new evaluator ID and policy version.
- Each invocation publishes raw score evidence only. Labels are independently
  loaded from the captured frozen data by the verifier; evaluator-reported
  labels are forbidden.
- The two raw score evidence payloads must be byte-identical after canonical
  serialization. Runtime diagnostics are outside this comparison.
- A separately captured, code-owned verifier reconstructs all 162 observations
  from captured labels and raw scores. It does not import the evaluator's metric
  or aggregation functions.
- The verifier is applied separately to both invocation payloads. The two
  reconstructed observation payloads and aggregates must be byte-identical.
- `run-1.json`, `run-2.json`, and `results.json` are built by the controller
  from verifier output. Sandbox-provided `results.json`, per-seed summaries, and
  aggregate summaries are ignored and forbidden in the output namespace.

### 2.4 Publication and replay

- Every success manifest is written last and replayed from disk.
- Every failure invalidates the success manifest before diagnostic cleanup.
- Stage 12 journal records exactly two started/terminal pairs, one per
  predeclared ordinal. An incomplete or failed pair cannot publish authority.
- Stage 13 cannot modify a fixed domain evaluator. It publishes an explicit
  no-refinement selection of the Stage 12 baseline.
- Stage 14, canonical promotion, shared accessors, independent reconstruction,
  and release checks preserve the same evaluator-capture and trusted-execution
  consistency identities.

## 3. Versioned authority packages

### 3.1 New domain and evaluator identities

```text
domain_id: hardware_trojan_localization
evaluator_id: trojnet_iscas85_graphsage_localization
evaluator_schema: trojnet_iscas85_graphsage_localization_v1
evaluator_kind: domain_evaluator
```

The trusted selector policy adds higher-priority normalized rules for exact
Hardware-Trojan/TrojNet/ISCAS-85 localization topics. Existing transient-
execution topics continue selecting `security_detection`.

Versioned package files are additive; v1 files are not rewritten:

```text
researchclaw/experiment_runtime/metric_authority/domain-selector-v2.json
researchclaw/experiment_runtime/metric_authority/index-v2.json
researchclaw/experiment_runtime/metric_authority/profiles/
  hardware_trojan_localization-v2.json
researchclaw/experiment_runtime/metric_authority/
  trojnet_iscas85_graphsage_localization-v2.json
researchclaw/experiment_runtime/domain_evaluators/
  trojnet_iscas85_v1/
    package-manifest-v1.json
    evaluator_main.py
    verifier_main.py
    execution-policy-v1.json
```

The package manifest declares separate trusted roots for package code, frozen
vendor source, and frozen data. It may reference the already committed fixture
vendor/data roots, but every root has its own fixed package path, hash-bound
file inventory, and exact namespace closure. Canonical execution does not
import or call the diagnostic fixture runner.

### 3.2 Metric registry

The metric key set is exact and key-sorted:

```text
accuracy
auprc
auroc
f1
fpr
precision
recall
top_k_precision
```

All eight units are `ratio`. Runtime is diagnostic and is not a metric key.
The registry owner is `domain_evaluator`, requiring metric-authority policy v2.
The primary metric is `auprc`, direction `maximize`.

`MetricAuthorityIdentityV2` has exactly:

```text
schema_version=2, policy_version=2,
domain_id, evaluator_id, experiment_mode, evaluator_kind,
selector_policy_version=2, selector_input_sha256,
domain_selector_package, domain_selector_snapshot,
domain_profile_snapshot, selector_index_package, selector_index_snapshot,
registry_snapshot
```

Each artifact field is a fixed-path `{path,sha256}` pair. Package references
point to code-allowlisted trusted paths; snapshot references point to exact
Stage 9 paths and must have byte-identical hashes. The identity is independently
derived from topic, active config, trusted selector policy, and trusted index;
stored fields are comparison-only.

### 3.3 Trusted selector index v2

`index-v2.json` is loaded only from its fixed package path. It has exactly:

```json
{
  "schema_version": 2,
  "selector_policy_version": 2,
  "entries": [
    {
      "domain_id": "hardware_trojan_localization",
      "experiment_mode": "sandbox",
      "evaluator_kind": "domain_evaluator",
      "evaluator_id": "trojnet_iscas85_graphsage_localization",
      "package_policy_version": 1,
      "package_manifest_path": "researchclaw/experiment_runtime/domain_evaluators/trojnet_iscas85_v1/package-manifest-v1.json",
      "package_manifest_sha256": "<sha256>",
      "metric_registry_path": "researchclaw/experiment_runtime/metric_authority/trojnet_iscas85_graphsage_localization-v2.json",
      "metric_registry_sha256": "<sha256>"
    }
  ]
}
```

Entries are nonempty and sorted by the tuple `(domain_id, experiment_mode,
evaluator_kind, evaluator_id)`. The first three fields are globally unique.
Every version is a true integer. Every path is an exact, code-allowlisted
package path and every hash is lowercase SHA-256. The loader verifies the
trusted index entry and package-manifest hash before parsing any path or source
root supplied by that manifest. Contract fields and run-local snapshots are
equality projections only; they cannot select or re-anchor package bytes.

### 3.4 Package manifest

`package-manifest-v1.json` has exactly:

```json
{
  "schema_version": 1,
  "package_policy_version": 1,
  "domain_id": "hardware_trojan_localization",
  "evaluator_id": "trojnet_iscas85_graphsage_localization",
  "evaluator_schema": "trojnet_iscas85_graphsage_localization_v1",
  "execution_policy_path": "execution-policy-v1.json",
  "execution_policy_sha256": "<sha256>",
  "source_roots": [
    {
      "root_id": "package",
      "trusted_path": "researchclaw/experiment_runtime/domain_evaluators/trojnet_iscas85_v1",
      "reserved_control_files": ["package-manifest-v1.json"]
    },
    {
      "root_id": "vendor",
      "trusted_path": "researchclaw/experiment_runtime/validation_fixtures/trojnet_iscas85_v1/vendor/trojnet",
      "reserved_control_files": []
    },
    {
      "root_id": "data",
      "trusted_path": "researchclaw/experiment_runtime/validation_fixtures/trojnet_iscas85_v1/data/iscas85",
      "reserved_control_files": []
    }
  ],
  "files": [
    {
      "source_root": "package",
      "source_path": "<fixed root-relative path>",
      "capture_path": "<fixed capture-relative path>",
      "role": "evaluator|verifier|vendor|data|policy",
      "sha256": "<sha256>",
      "size": 1
    }
  ]
}
```

`source_roots` is nonempty, sorted by `root_id`, and has exact entries for
`package`, `vendor`, and `data`. `trusted_path` values are fixed by the trusted
selector index, not selected by the manifest. The package root's only reserved
control file is `package-manifest-v1.json`; its bytes are authorized by the
selector-index hash and it is not captured, avoiding a self-hash cycle. Vendor
and data roots have no reserved control files.

`files` is nonempty and sorted by `capture_path`. Every `source_root` must exist
in `source_roots`. Source and capture paths are normalized relative POSIX paths
with no empty, dot, dot-dot, or symlink segments. Capture paths are globally
unique. Size is a positive true integer. Every file is a regular file and is
read from a held source fd. For each source root, declared files plus the fixed
reserved control files derive the exact allowed directory closure; the source
walker may not silently ignore an undeclared entry.

Root IDs, trusted paths, and held `(st_dev, st_ino)` identities are globally
unique. Trusted root paths may not equal, contain, or be contained by another
root. Every `(source_root, source_path)` pair is unique. After opening all
files, their `(st_dev, st_ino)` identities must also be globally unique, so a
hardlink cannot satisfy two logical files or roles. The manifest contains
exactly one `evaluator`, one `verifier`, and one `policy` role; `vendor` and
`data` are both nonempty and their complete path sets are fixed by the trusted
manifest hash.

### 3.5 Execution policy

`execution-policy-v1.json` has exactly:

```json
{
  "schema_version": 1,
  "execution_policy_version": 1,
  "invocation_count": 2,
  "seeds": [0, 1, 2],
  "circuit_families": ["c1355", "c1908", "c3540", "c432", "c6288", "c880"],
  "variants_per_family": 3,
  "conditions": [
    "raw_cc1",
    "scoap_isolation_forest",
    "trojnet_community_graphsage"
  ],
  "metric_keys": [
    "accuracy", "auprc", "auroc", "f1", "fpr", "precision", "recall",
    "top_k_precision"
  ],
  "primary_condition": "trojnet_community_graphsage",
  "primary_metric_key": "auprc",
  "primary_observation_set": "exact_18_variants_per_seed",
  "primary_aggregation": "mean_variants_then_mean_seeds_v1",
  "execution_backend_policy": "host_subprocess_trusted_consistency_v1",
  "raw_evidence_policy": "node_score_evidence_v1",
  "observation_policy": "trojnet_localization_metrics_v1",
  "aggregation_policy": "condition_seed_variant_mean_v1",
  "runtime_projection": {
    "python_major_minor": "3.11",
    "packages": {
      "networkx": "3.6.1",
      "numpy": "2.4.6",
      "scikit-learn": "1.9.0",
      "scipy": "1.17.1",
      "torch": "2.12.1",
      "torch-geometric": "2.8.0"
    },
    "device": "cpu",
    "torch_deterministic_algorithms": true,
    "torch_num_threads": 1
  }
}
```

All arrays are authority order. Booleans cannot satisfy integer fields.

The unique Stage 12-14 scalar is derived, never selected from stored output:

```text
for each seed:
  mean AUPRC over the exact 18 trojnet_community_graphsage variants
primary_metric_value:
  arithmetic mean of those exact three per-seed means
```

The scalar remains a `Fraction` until canonical Decimal serialization. Stage
12, Stage 13, Stage 14, the shared accessor, and independent reconstruction all
recompute it from observation rows. A stored primary value is comparison-only
and cannot participate in candidate selection.

## 4. Stage 9 contract v3

A new mandatory capability `domain_evaluator_authority` is added at version
zero before any production parser accepts contract v3. Complete Stage 12+
entrypoints remain blocked until the migration is activated separately.

Contract v2 remains valid only for the existing scaffold evaluator. Contract
v3 adds one exact top-level `evaluator_authority` object:

```json
{
  "kind": "domain_evaluator",
  "domain_id": "hardware_trojan_localization",
  "evaluator_id": "trojnet_iscas85_graphsage_localization",
  "evaluator_schema": "trojnet_iscas85_graphsage_localization_v1",
  "package_manifest_package_path": "<fixed trusted package path>",
  "package_manifest_package_sha256": "<sha256>",
  "package_manifest_snapshot_path": "stage-09/domain_evaluator_package_manifest.json",
  "package_manifest_snapshot_sha256": "<sha256>",
  "execution_policy_package_path": "<fixed trusted package path>",
  "execution_policy_package_sha256": "<sha256>",
  "execution_policy_snapshot_path": "stage-09/domain_evaluator_execution_policy.json",
  "execution_policy_snapshot_sha256": "<sha256>",
  "input_capture_policy_version": 1,
  "result_set_policy_version": 2,
  "observation_replay_policy_version": 1
}
```

Package and snapshot hashes must be pairwise equal. The object is a projection
of trusted selector/package policy. The Stage 9 LLM
cannot emit or modify it. For this evaluator, contract validation additionally
requires the fixed scope, origin, dataset, primary metric, mode, metric key set,
and empty model-owned input surface.

Stage 9 publishes run-local snapshots of selector, profile, registry, package
manifest, and execution policy through one held Stage 9 namespace. Contract is
written after snapshots and replayed before its sidecar is committed.

### 4.1 Mechanical schema compatibility matrix

Only these complete tuples are valid:

```text
scaffold generation:
  contract v2 + Stage 10 seal v2 + journal v1 + result-set v1
  + refinement v1 + Stage 14 candidate v1 + canonical root v1

domain-evaluator generation:
  contract v3 + Stage 10 seal v3 + capture manifest v1 + journal v2
  + result-set v2 + refinement v2 + Stage 14 candidate v2
  + canonical root v2
```

After the bytes-level numeric scan, every parser performs a duplicate-key-
rejecting dispatch parse with `parse_int=int` and `parse_float=Decimal`, reads
only `schema_version`, requires `type(version) is int`, and selects one exact
schema. The selected authority parser then reparses the same immutable bytes
with both numeric hooks set to Decimal as specified in Section 6.3. A payload
field such as `candidate_kind`, `result_set_type`, or `evaluator_schema` never
chooses the grammar. Any cross-row combination rejects before artifact
selection. Shared
accessors and reconstruction expose a tagged immutable union only after the
entire selected row has replayed; they do not merge fields from both rows.

## 5. Stage 10 immutable evaluator capture

### 5.1 Namespace

Stage 10 adds:

```text
stage-10/evaluator-capture-v1/
  capture-manifest.json
  evaluator/evaluator_main.py
  verifier/verifier_main.py
  vendor/...
  data/...
  policy/execution-policy-v1.json
```

The capture manifest is the directory commit point and is written last. It has
exactly:

```text
schema_version=1, capture_policy_version=1,
package_manifest, execution_policy, evaluator_schema,
source_namespace_sha256, files
```

The two manifest fields are `FileRefV2`. `files` is a nonempty array sorted by
capture path; each entry has exactly `role,path,sha256,size,
package_entry_sha256`. Its path set equals the complete capture namespace except
the manifest itself. `source_namespace_sha256` hashes the ordered trusted-root
IDs, source paths, roles, package hashes, and sizes; host inode values are
capture-time checks and are not portable stored authority.

The Stage 10 selected-candidate manifest v3 has exactly:

```text
schema_version=3, seal_policy_version=2,
candidate_kind="domain_evaluator", experiment_contract, run_config,
config_semantic_policy_version, config_semantic_sha256,
metric_authority, package_manifest, execution_policy,
capture_manifest, evaluator_schema
```

All artifact fields are `FileRefV2`; `metric_authority` is the exact contract-v3
identity. Seal replay validates the capture's complete namespace rather than a
model project file list.

No model-generated plugin or project file is selected for this candidate kind.
The existing scaffold seal remains schema v2 and behaviorally unchanged. A
loader dispatches first on a true-integer schema version and then applies one
exact grammar; `candidate_kind` may not choose a grammar.

### 5.2 Capture algorithm

1. Acquire the existing release-graph writer epoch and Stage 10 held namespace.
2. Load the trusted package manifest from its fixed package path.
3. Open every declared source root with `O_DIRECTORY | O_NOFOLLOW`.
4. Walk and open every intermediate directory relative to its parent fd using
   `O_DIRECTORY | O_NOFOLLOW`; open each final file using `O_NOFOLLOW`.
5. Require a regular file, capture identity with `fstat`, read from the fd,
   re-`fstat`, and require stable device/inode/size/mtime/ctime.
6. Require the fd-relative source namespace to equal the manifest-derived file
   and directory closure, then require captured size/hash to equal the package
   manifest.
7. After all copies, re-`fstat` every held root and file, re-enumerate every root
   namespace, and require identities, entries, sizes, and hashes to equal the
   initially captured plan.
8. Create a hidden staging sibling under the held Stage 10 directory fd, on the
   same filesystem as the final capture.
9. Replay the staging tree against the package manifest.
10. Publish the complete tree by fd-relative rename into Stage 10. `EXDEV`
   rejects; copy-delete fallback is forbidden.
11. Replay from the held Stage 10 fd, write `capture-manifest.json` last, and
    bind it into `selected_candidate_manifest.json`.

Failure removes or quarantines the newly published tree relative to the held fd
and leaves no selected-candidate manifest. A package-local `__pycache__` is an
undeclared source entry and rejects capture. Captured `.py` files run as direct
scripts in fresh invocation workspaces with controller-selected external
`-X pycache_prefix` directories.

## 6. Stage 12 trusted execution evidence v2

### 6.1 Invocation journal

Result-set policy v2 predeclares exactly two invocations. The controller owns
four journal records:

```text
started ordinal 1
terminal completed ordinal 1
started ordinal 2
terminal completed ordinal 2
```

Each started record binds contract v3, Stage 10 seal, capture manifest, config
semantic hash, evaluator schema, execution policy hash, and ordinal. A failure,
timeout, missing output, backend fallback, or incomplete journal prevents the
result-set manifest.

Journal v2 is canonical JSONL with exactly four LF-terminated records. Started
records have exactly:

```text
schema_version=2, event="started", ordinal, invocation_token,
generation_binding_sha256, experiment_contract_sha256,
sealed_candidate_manifest_sha256, capture_manifest_sha256,
execution_policy_sha256, runtime_attestation_sha256,
config_semantic_sha256, evaluator_schema
```

Completed terminal records have exactly:

```text
schema_version=2, event="terminal", ordinal, invocation_token,
status="completed", score_evidence_path, score_evidence_sha256,
execution_meta_path, execution_meta_sha256, failure_code=null
```

Ordinals are exactly `1,2`; each token is controller-generated, unique, and
bound to its matching pair. Any other status can exist only in an unpublished
diagnostic journal generation and has null authority paths/hashes.

### 6.2 Invocation output namespace

Each fresh invocation may publish exactly:

```text
score_evidence.jsonl
execution_meta.json
diagnostics.json
```

`diagnostics.json` is copied only to `stage-12/diagnostics/ordinal-N.json`, which
is outside the evidence namespace and all authority manifests. It cannot affect
retry, selection, promotion, or failure classification. `execution_meta.json`
is controller-owned and records actual ordinal/token, evaluator command hash,
backend policy, Python major/minor, imported package versions, thread/device
settings, capture hash, and execution-policy hash. It is created from the
controller launcher and child bootstrap, not evaluator output. Any
evaluator-produced metric, summary, label, aggregate, runtime attestation, or
generic `results.json` is forbidden.

Before each evaluator launch, a controller-owned bootstrap under the selected
Python executable imports the exact runtime package set, records their actual
versions, activates deterministic torch settings, and exits. The controller
strictly compares that output with the trusted execution policy and hashes the
canonical attestation into the started-record generation binding. A missing,
extra, mismatched, or noncanonical runtime field rejects before evaluator code
runs. Repeating policy text from evaluator output is never runtime evidence.

Each score-evidence row has exactly:

```json
{
  "schema_version": 1,
  "condition": "raw_cc1",
  "seed": 0,
  "circuit_family": "c432",
  "circuit_variant": "c432_ht1",
  "node_ids": ["<ordered unique node id>"],
  "scores": ["<canonical finite decimal string>"]
}
```

There are exactly 162 rows in policy order. `node_ids` and `scores` are equal-
length nonempty arrays. Node IDs are unique and must equal the independently
parsed captured circuit node set. Scores use a closed canonical decimal-string
grammar; NaN, infinity, booleans, JSON numbers, and exponent aliases reject.
The JSONL file is UTF-8 without BOM or CR, contains one canonical key-sorted
compact JSON object per line, ends in exactly one LF, contains no blank line,
and is at most 128 MiB.

### 6.3 Independent verifier

The verifier is a trusted, direct captured script launched with `python -I -S`
in a fresh workspace containing copies of only verifier source, captured data,
and one captured score-evidence file. The controller supplies exact argv, an
allowlisted environment, and `-X pycache_prefix=<controller temp>`. It does not
rely on `PYTHONPYCACHEPREFIX`, because isolated mode ignores `PYTHON*` variables.
Evaluator/vendor roots are not added to `sys.path`. Because the current host
subprocess backend is not an OS sandbox, these conditions are structural input
discipline, not a claim that malicious code cannot open absolute host paths.
Trust rests on the fixed verifier source hash and independent implementation.

Before packaging, an AST/import-closure test requires the verifier to import
only `json`, `decimal`, `fractions`, `hashlib`, `pathlib`, `re`, `sys`, and
`unicodedata`; dynamic import primitives are forbidden. The verifier source
may open only its three controller-provided argv roots. This static rule is a
review guard over trusted code, not a replacement for process isolation.

It uses only Python standard-library `json`, `decimal`, and `fractions` for
metric reconstruction. It independently parses frozen Trojan-label files and
applies the following `trojnet_localization_metrics_v1` rules:

1. Scores are parsed as exact `Decimal` values. Labels are true integers in
   `{0, 1}`. Each observation must contain at least one positive and one
   negative label.
2. AUROC is the exact pairwise rank statistic
   `(positive-greater-negative + ties / 2) / (positive_count * negative_count)`.
3. AUPRC is grouped average precision. Distinct score groups are processed in
   descending order; after consuming a complete tie group, add
   `(recall_current - recall_previous) * precision_current`. No ordering inside
   a tie group can affect the result.
4. Youden candidates are an initial predict-none point followed by each
   distinct score in descending order. At score `s`, scores greater than or
   equal to `s` are positive. Select the first candidate with maximal
   `TPR - FPR`; this is the highest-threshold tie rule.
5. Accuracy, precision, recall, F1, and FPR are exact count ratios at the
   selected Youden point. A zero denominator yields zero only for precision or
   F1; positive and negative label counts themselves may not be zero.
6. For top-k precision, `k` equals the positive-label count. Nodes above the
   kth score are selected completely. Remaining slots at the boundary score
   receive the exact expected positive fraction of the whole tie group. If
   `A={score>cutoff}`, `T={score=cutoff}`, and `r=k-|A|`, then
   `top_k_precision=(positives(A)+r*positives(T)/|T|)/k`.

All ratios, means, and variances remain `Fraction` values until serialization.
A local Decimal context of precision 50 with `ROUND_HALF_EVEN` converts a
nonterminating ratio or square root exactly once. Canonical JSON number lexemes
use no exponent, no leading plus, no redundant leading or trailing zero, and
normalize negative zero to `0`. Conversion applies unary plus in that local
context, then `format(value, "f")`, then strips fractional trailing zeroes and
the decimal point. No per-seed value is rounded before aggregate variance. No
global Decimal context, NumPy, SciPy, sklearn, torch metric helper, or evaluator
code may participate in verification.

Authority JSON uses duplicate-key rejection, `parse_float=Decimal`,
`parse_int=Decimal`, and constant rejection. Schema-aware field validators then
require structural integer fields to be nonnegative integral Decimals. Before
JSON parsing, a bytes-level token scanner requires every number lexeme to match
`-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?`, rejects `-0`, and rejects any lexeme
whose canonical Decimal formatting differs from its source bytes. Structural
integer fields additionally require source grammar `0|[1-9][0-9]*`; metric
fields remain Decimal. This makes canonical metric zero `0` unambiguous without
accepting bool or binary float authority.

It does not import `vendor/trojnet/eval.py`, the fixture runner, or evaluator
aggregation helpers. Its output is canonical JSON with no runtime field.

#### 6.3.1 Frozen-data byte grammar

Bench and label inputs are bytes-first UTF-8 without BOM, CR, NUL, invalid
UTF-8, or missing final LF. A bench semantic line, after removing an optional
`#` comment and ASCII surrounding space, is exactly one of:

```text
INPUT(<node_id>)
OUTPUT(<node_id>)
<node_id> = <gate>(<node_id>[, <node_id>]*)
```

`node_id` matches `[A-Za-z_][A-Za-z0-9_]*` and is case-sensitive. `gate` is one
of `AND,NAND,OR,NOR,XOR,XNOR,NOT,BUF,DFF`. Keywords are uppercase. Empty
semantic lines are allowed only for blank or comment-only source lines. Each
node has one defining INPUT or gate statement; duplicate definitions reject.
Every gate input and OUTPUT must resolve to a defined node after the complete
parse. Node order is first semantic appearance while scanning lines, including
gate input references. The parser rejects unknown statements, empty fan-in,
duplicate fan-in, and trailing noncomment text.

A label file contains one `node_id` per LF-terminated line, with no blank line,
comment, surrounding whitespace, duplicate, or unknown node. It is nonempty
and a strict subset of the parsed circuit nodes. The score-evidence `node_ids`
must exactly equal the bench-derived ordered node sequence; labels are derived
only by membership in the captured label file.

### 6.4 Observation and aggregation order

Observation order is the Cartesian product in this exact nesting order:

```text
execution_policy.seeds
  -> execution_policy.circuit_families
    -> variant number 1, 2, 3
      -> execution_policy.conditions
```

For every condition and seed, per-seed metrics are the arithmetic mean of the
18 circuit-variant observations. For every condition, aggregate `mean`, `min`,
and `max` operate on the three per-seed values. Aggregate variance is the exact
Fraction `sum((x-mean)^2)/(n-1)` over unrounded per-seed Fractions. Only its
square root enters the precision-50 Decimal context with `ROUND_HALF_EVEN`.
Output key and row order are fixed by the
execution policy. The verifier emits metric values as canonical JSON number
lexemes, which authority loaders parse with both integer and float hooks set to
`Decimal` and reject if the source lexeme is not canonical.

The controller independently runs the verifier against invocation 1 and 2,
requires byte-identical observation, per-seed, and aggregate output, and only
then creates the Stage 12 result set.

### 6.5 Result-set publication

The controller creates:

```text
stage-12/evidence-v2/invocation-1/score_evidence.jsonl
stage-12/evidence-v2/invocation-1/execution_meta.json
stage-12/evidence-v2/invocation-2/score_evidence.jsonl
stage-12/evidence-v2/invocation-2/execution_meta.json
stage-12/evidence-v2/verification-1.json
stage-12/evidence-v2/verification-2.json
stage-12/evidence-v2/observations.json
stage-12/evidence-v2/run-1.json
stage-12/evidence-v2/run-2.json
stage-12/evidence-v2/results.json
stage-12/experiment_result_set.json
```

`verification-1.json` and `verification-2.json` are byte-identical verifier
outputs because the input score bytes must be identical. `observations.json` is
a controller-published byte copy of that output. `run-1.json` and `run-2.json`
bind their invocation raw evidence and the same observations. `results.json` is
controller-derived from those observations. The result-set manifest is written
last and binds the exact evidence namespace and complete journal.

### 6.6 Exact Stage 12 v2 schemas

All file references in v2 have exactly `{path, sha256, size}`. Paths are fixed
run-relative POSIX paths, hashes are lowercase SHA-256, and size is a positive
structural integer. `PrimaryMetricV2` has exactly:

```text
condition, key, observation_set, aggregation, value
```

with the four identity fields equal to execution policy and `value` equal to
the independently recomputed canonical Decimal scalar.

`observations.json` and each verification document have exactly:

```text
schema_version=2, observation_policy_version=1,
dataset_capture_sha256, score_evidence_sha256,
metric_keys, observations, per_seed, aggregate, primary_metric
```

The verification copies omit ordinal/token and therefore must be byte-identical
when score bytes match. Observation rows have exactly `condition, seed,
circuit_family, circuit_variant, n_total, n_trojan, metrics`. Per-seed rows have
exactly `condition, seed, n_variants=18, metrics`. Aggregate rows have exactly
`condition, n_seeds=3, metrics`; each aggregate metric has exactly
`mean,std,min,max`. Ordering and numeric grammar are fixed by Sections 6.3-6.4.

Each `run-N.json` has exactly:

```text
schema_version=2, run_policy_version=2, ordinal, invocation_token,
generation_binding_sha256, capture_manifest, score_evidence,
execution_meta, observations, primary_metric
```

`results.json` has exactly:

```text
schema_version=2, results_policy_version=2,
runs=[run-1 ref, run-2 ref], observations ref,
structured_results, primary_metric
```

`structured_results` is exactly the ordered `aggregate` array from
`observations.json`; it is not an independently supplied summary.

`experiment_result_set.json` v2 has exactly:

```text
schema_version=2, result_set_policy_version=2,
result_set_type="stage12_domain_evaluator",
experiment_mode, experiment_contract, sealed_candidate_manifest,
capture_manifest, package_manifest, execution_policy, run_config,
config_semantic_policy_version, config_semantic_sha256,
claim_scope, dataset_origin, dataset_name, evaluator_schema,
metric_authority, invocation_journal, execution_statuses,
evidence_files, observations, results, primary_metric
```

The common artifact fields are `FileRefV2`; `metric_authority` is the exact
contract-v3 identity object. `execution_statuses` contains exactly two ordered
completed entries with `ordinal, status, run, failure_code=null`.
`evidence_files` is the exact key-sorted list of every file under
`evidence-v2/`; no diagnostic path is present. The manifest parser recomputes
the entire journal, namespace, verifier outputs, observations, aggregates, and
primary scalar before accepting stored references or values.

## 7. Stage 13 fixed-evaluator semantics

Stage 13 must not ask an LLM to modify evaluator, verifier, vendor, data, or
policy bytes. Refinement manifest v2 has the following exact top-level key set;
the displayed nested identity objects are abbreviated references to their exact
schemas:

```json
{
  "schema_version": 2,
  "refinement_policy_version": 2,
  "result_set_type": "stage13_refinement",
  "baseline_manifest": {"path": "stage-12/experiment_result_set.json", "sha256": "<sha256>"},
  "experiment_contract": {"path": "stage-09/experiment_contract.yaml", "sha256": "<sha256>"},
  "sealed_candidate_manifest": {"path": "stage-10/selected_candidate_manifest.json", "sha256": "<sha256>"},
  "capture_manifest": {"path": "stage-10/evaluator-capture-v1/capture-manifest.json", "sha256": "<sha256>"},
  "execution_policy": {"path": "stage-09/domain_evaluator_execution_policy.json", "sha256": "<sha256>"},
  "observations": {"path": "stage-12/evidence-v2/observations.json", "sha256": "<sha256>"},
  "run_config": {"path": "config.yaml", "sha256": "<sha256>"},
  "config_semantic_policy_version": 1,
  "config_semantic_sha256": "<sha256>",
  "claim_scope": "pipeline_validation",
  "dataset_origin": "synthetic",
  "dataset_name": "controlled_synthetic_iscas85_trojan_localization_v1",
  "evaluator_schema": "trojnet_iscas85_graphsage_localization_v1",
  "metric_authority": {"schema_version": 2},
  "refinement_mode": "fixed_evaluator_no_refine",
  "primary_metric": {"condition": "trojnet_community_graphsage"},
  "iterations": [],
  "selected_result": {"type": "baseline", "iteration_id": null}
}
```

The abbreviated nested `metric_authority` and `primary_metric` examples above
must expand to their complete contract-v3 identity and `PrimaryMetricV2` exact
schemas; no additional fields are allowed. The displayed file refs omit `size`
only as an illustration shorthand; every stored reference is a complete
`FileRefV2` and must equal Stage 12. Stage 13 writes no compatibility project
copy. LLM and sandbox call counts are both zero. The v2 parser cannot call the
v1 refinement iteration parser.

## 8. Stage 14 and downstream bindings

Stage 14 receives the selected Stage 12 observation payload through the Stage
13 manifest. Candidate schema v2 additionally binds:

- evaluator capture manifest path/hash;
- execution policy path/hash;
- both invocation raw-evidence hashes;
- verifier source hash;
- observation payload path/hash and replay-policy version; and
- Stage 12 journal and result-set hashes.

Analysis text and figures are not observation authority. Deterministic
promotion, the canonical root manifest, shared accessor, independent Stage
9-14 reconstruction, and final release reconstruction replay the complete
chain. A stored Stage 14 winner cannot choose an evaluator, capture, invocation,
or observation payload.

### 8.1 Candidate v2 and canonical root v2

Stage 14 candidate v2 top-level keys are exactly:

```text
schema_version=2, candidate_policy_version=2, candidate_id,
bindings, selected_result, observation_authority, primary_metric, artifacts
```

Canonical root v2 top-level keys are exactly:

```text
schema_version=2, selection_policy_version=2,
generation_kind="domain_evaluator", bindings, selected_result,
observation_authority, primary_metric, selected_candidate,
selected_summary, selected_analysis
```

For both schemas, `bindings` has exactly:

```text
experiment_contract, sealed_candidate_manifest, capture_manifest,
package_manifest, execution_policy, stage12_result_set,
stage13_refinement, run_config, config_semantic_policy_version,
config_semantic_sha256, claim_scope, dataset_origin, dataset_name,
evaluator_schema, metric_authority
```

Artifact fields are `FileRefV2`. `observation_authority` has exactly
`invocation_journal, score_evidence_1, score_evidence_2, observations, results,
observation_policy_version`. `selected_result` is exactly
`{type:"baseline", iteration_id:null}`. `primary_metric` is
`PrimaryMetricV2`. Candidate `artifacts` entries have exactly
`{role,path,sha256,size}` for analysis, summary, results table, and deterministic
empty figure plan; roles and paths are exact and no chart or evaluator artifact
is allowed. Root `selected_candidate` has exactly `{candidate_id,manifest}`
where `manifest` is `FileRefV2`. Root `selected_summary` and
`selected_analysis` each have exactly `{source,canonical_copy}`, and both nested
values are `FileRefV2`. Summary copy path is fixed to
`experiment_summary_best.json`; analysis copy path is fixed to
`analysis_best.md`. Each source ref must equal the corresponding selected
candidate artifact ref. Source and copy hashes and bytes must be equal.

Candidate identity uses an envelope without a content-hash cycle. The virtual
`identity_payload` has exactly `schema_version=2`,
`candidate_policy_version=2`, complete `bindings`, `selected_result`,
`observation_authority`, `primary_metric`, and the ordered artifact
`role,path,sha256,size` entries. It excludes candidate ID,
candidate digest, manifest digest, final directory path, timestamps, and file
metadata. Then:

```text
candidate_digest = sha256(canonical_json(identity_payload))
candidate_id = "cand-" + candidate_digest
```

The candidate manifest is the identity envelope and is not hashed back into
`identity_payload`. Artifact bytes enter identity only through their verified
hash and size. Loader replay reconstructs the payload, digest, and ID before
directory-name comparison.

Root replay treats `canonical_experiment_evidence.json`,
`experiment_summary_best.json`, and `analysis_best.md` as its exact owned
compatibility namespace. A missing, extra similarly owned/versioned, symlink,
or nonregular compatibility entry rejects. Promotion first replays Stage 12
observations and derives `primary_metric.value`; it never reads candidate
summary to obtain the score.
The stored candidate/root value and summary mirror must equal that derived
Decimal. The accessor dispatches root v1 or v2 by true-integer schema version
and returns the matching immutable union only after full-row replay.

## 9. Failure, resume, and rollback

- A Stage 9 policy or contract v3 failure leaves no contract commit point.
- A Stage 10 capture failure leaves no selected candidate manifest.
- Before a Stage 9 generation attempt reads producer input, the held
  release-graph writer fd invalidates root canonical authority, the Stage 10
  selected-candidate seal, the Stage 10 capture commit point, and Stage 12/13
  commit points. Before a Stage 10 attempt, it invalidates its own seal/capture
  commit points plus Stage 12/13/root. Before a Stage 12 attempt, it invalidates
  Stage 12/13/root before acquiring an invocation lease.
- A Stage 12 failure leaves no Stage 12 result-set, Stage 13 refinement, or root
  canonical manifest before diagnostics are retained.
- Existing immutable Stage 14 candidate directories, including `stage-14_vN`,
  are retained as history. Promotion fully validates every candidate. A valid
  candidate whose Stage 12/13/capture bindings differ is ineligible; a malformed
  candidate, duplicate-ID byte collision, or unsafe namespace is a hard
  failure, not a skipped entry.
- A Stage 13/14 rerun preserves Stage 10/12 only after complete replay of their
  exact generation and capture identities.
- Resume cannot combine contract v2, Stage 10 seal v3, result-set v2, or Stage
  14 candidate v1, nor any other cross-row tuple from Section 4.1. Mixed schema
  generations reject and require rerun from the earliest changed authority
  stage.
- Restoring a detached old run directory cannot resurrect a manifest whose
  generation was invalidated. All commit-point removal above is fd-relative to
  the pre-replacement run inode; live replacement paths are never opened for
  cleanup.

## 10. Implementation milestones

### M0: capability and selector design

- Add `domain_evaluator_authority=0` to the mandatory capability map.
- Add selector/profile/index/metric-registry v2 files and strict loaders.
- Add contract v3 parser/producer/replay, but keep Stage 12+ blocked.

### M1: immutable Stage 10 capture

- Add package/execution manifest strict loaders.
- Add fd-bound source capture and Stage 10 domain-evaluator
  selected-candidate seal v3.
- Prove Stage 12 has no package/fixture path consumer before proceeding.

### M2: Stage 12 raw execution and independent reconstruction

- Before production wiring, run a non-authoritative reproducibility probe that
  captures all 162 raw score rows from two clean processes. Raw canonical bytes
  must match. A mismatch blocks M2; it may not be hidden with a tolerance,
  summary-only comparison, or a changed seed policy.
- Commit golden verifier vectors covering score ties, Youden ties, all-equal
  scores, top-k boundary ties, zero predicted positives, and nonterminating
  rational outputs before accepting the full fixture output.
- Add journal/result-set policy v2.
- Add raw score evidence producer and independent verifier.
- Execute two fresh host-subprocess invocations and publish verifier-derived
  authority only, preserving the explicit trusted-code limitation.

### M3: Stage 13/14 and reconstruction

- Add fixed-evaluator no-refine policy.
- Extend Stage 14 candidate, promotion, accessor, U0 helper, and independent
  release reconstruction.

### M4: activation and validation

- Run focused adversarial suites.
- Run the complete 162-observation evaluator twice from canonical Stage 12.
- Run one fresh Stage 1-25 `pipeline_validation` chain.
- Raise `domain_evaluator_authority` only in a separate narrow activation
  commit after independent approval.

## 11. Required adversarial tests

At minimum:

1. topic selects the old HPC evaluator after synchronized stored-ID rewrites:
   reject;
2. selector snapshot, profile, index, registry, and package manifest are all
   synchronously replaced: trusted package selector still rejects;
3. package source changes before open, during read, and after capture;
4. mutate-then-restore during capture cannot authorize altered bytes;
5. source/capture symlink, FIFO, socket, device, duplicate target, extra file,
   missing file, and hash/size mismatch reject;
6. package-local malicious pyc cannot execute or enter the capture;
7. package source changes after Stage 10: Stage 12 uses captured bytes only;
8. captured evaluator/data/verifier/policy changes after Stage 10: replay rejects;
9. invocation ordinal 1 or 2 missing, duplicated, failed, timed out, or reordered:
   no result-set manifest;
10. Docker/subprocess fallback or backend mismatch rejects before authority;
11. invocation score evidence differs by one score: reject;
12. invocation output adds self-reported labels, metrics, summaries, or
    `results.json`: reject exact namespace;
13. score row missing/duplicate/extra/wrong-order/wrong family/wrong variant;
14. node ID missing/duplicate/extra or inconsistent with captured circuit;
15. score is bool, JSON number, NaN, infinity, exponent alias, or noncanonical
    decimal string;
16. evaluator self-reports forged perfect metrics: ignored and forbidden;
17. stored observations, per-seed rows, aggregates, and all hashes are
    synchronously forged: independent verifier reconstruction rejects;
18. verifier imports evaluator metric code: repository/static guard rejects;
19. verifier package source or runtime projection differs from Stage 9/10
    authority: reject;
20. two executions produce equal summaries but different raw scores: reject;
21. two raw payloads match but verifier outputs differ: reject;
22. Stage 13 attempts any LLM/sandbox call or nonempty iteration: reject;
23. Stage 14 candidate points to another capture, invocation, verifier, or
    observation generation: reject;
24. contract/seal/result/candidate schema v1/v2/v3 mixing: reject;
25. rollback and resume at every Stage 9-14 boundary leaves no stale authority;
26. parent/stage replacement during capture, execution, verification, and
    publication causes external zero-write and detached-authority invalidation;
27. two complete canonical executions produce identical raw score evidence,
    observations, per-seed summaries, aggregates, and semantic identity;
28. default capability map remains incomplete until the separate activation
    commit;
29. Stage 19, E9, Stage 24/25, and release-check production code remain
    unchanged before downstream binding review; and
30. `research_release + synthetic` is rejected before Stage 10 capture;
31. Stage 10 seal v2 with domain fields, or v3 with scaffold fields, rejects
    before `candidate_kind` dispatch;
32. package manifest, roots, policy, and run-local hashes are synchronously
    rewritten while trusted index v2 is unchanged: reject before manifest parse;
33. duplicate source pair, overlapping roots, duplicate root inode, cross-role
    hardlink, wrong role cardinality, and cross-filesystem rename reject;
34. metric zero encoded as `0`, plus structural integer/metric type confusion,
    replays identically through the Decimal authority loader;
35. grouped AUPRC, Youden, top-k, exact mean, and sample-std golden vectors
    reject every alternative tie, rounding, or pre-rounding policy;
36. alternate primary condition, max-condition score, or direct stored scalar
    cannot become the Stage 14 candidate score;
37. BOM, CRLF, invalid UTF-8, duplicate gate/node/label, unknown label, malformed
    bench statement, and node-order mismatch reject;
38. stale valid Stage 14 history is ineligible, while malformed history and
    candidate-ID collision hard-fail without restoring root authority; and
39. synchronously rewriting artifact bytes, hashes, manifest, and directory
    while putting candidate ID/path/digest into the identity payload rejects the
    canonical envelope reconstruction.

## 12. Planned production landing map

Implementation is expected to remain narrowly distributed across these
ownership boundaries:

- `metric_authority.py` and additive package JSON: trusted two-level selector,
  domain profile, evaluator registry, package manifest, and execution policy;
- `contract.py` and Stage 9 implementation: contract v3 projection and held-fd
  snapshots;
- `canonical_experiment_evidence.py`: strict v2/v3 schemas and complete replay,
  without executing evaluator code;
- `canonical_execution_controller.py`, `release_graph_lock.py`, and Stage 10-14
  implementations: fd-bound capture, two invocation leases, publication, and
  generation invalidation;
- new `domain_evaluators/trojnet_iscas85_v1/` scripts: score producer and
  standard-library-only independent verifier; and
- focused canonical-evidence, controller, stage, and independent-reconstruction
  tests.

Any need to modify Stage 19, E9, Stage 24/25, or `release_check` before the
downstream binding review is a scope break and requires a new design ruling.

## 13. Explicit non-goals

- No claim that this synthetic fixture validates a publishable scientific
  result.
- No GPU execution, adaptive seeds, adaptive retries, or model-selected
  evaluator.
- No Stage 13 mutation of the frozen evaluator.
- No reuse of diagnostic fixture `validate_result()` as canonical replay.
- No reliance on mtime selection, live package paths, evaluator-provided
  summaries, or a single execution's self-report.
