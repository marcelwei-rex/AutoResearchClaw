# F0 Post-Activation Acceptance Criteria

Status: active

Applies to the first fresh Stage 1-25 runs after canonical evidence capability
activation. These runs validate the live provider and pipeline; checked-in
production-chain tests remain the deterministic regression proof.

## Required sequence

1. Run a fresh `pipeline_validation + synthetic` validation with no resume.
2. Diagnose and fix any canonical, provenance, citation, quality, or stage
   failure. Do not weaken a gate to make the run pass.
3. Prepare a separate public-data `research_release` config.
4. Run a second fresh Stage 1-25 validation. This is the release proof and must
   pass `scripts/release_check.py` with exit code 0.

The existing `config.deepseek.sectional-dry-run.yaml` proves only
`pipeline_validation`. Its `claim_scope: pipeline_validation` and
`dataset_origin: synthetic` cannot establish release readiness.

## Fresh-run rules

- Pin and record the current Git commit before starting.
- Use a new output directory. Do not use `--from-stage`, resume, or copy
  artifacts from an earlier run.
- Use one config snapshot for the entire run.
- Do not edit generated authority artifacts.
- Keep Stage 19, Stage 24/25, independent reconstruction, and
  `release_check` policies unchanged while diagnosing a run.
- Preserve the failed run directory as evidence if any stage fails.

## Preflight

```bash
git status --short --branch
git rev-parse HEAD
.venv/bin/python -m researchclaw doctor \
  -c config.deepseek.sectional-dry-run.yaml
```

Expected:

- branch and commit are recorded;
- only known, intentionally excluded untracked directories are present;
- doctor reports `PASS`;
- the configured model and sandbox runtime are available.

## F0-PV: pipeline-validation run

Choose a new directory name for every attempt:

```bash
RUN="runs/f0-pipeline-validation-$(date +%Y%m%d-%H%M%S)"
test ! -e "$RUN"
.venv/bin/python -m researchclaw run \
  -c config.deepseek.sectional-dry-run.yaml \
  -o "$RUN"
```

### Pipeline completion

```bash
.venv/bin/python - "$RUN" <<'PY'
import json
import sys
from pathlib import Path

run = Path(sys.argv[1])
summary = json.loads((run / "pipeline_summary.json").read_text(encoding="utf-8"))
assert summary["final_stage"] == 25, summary
assert summary["final_status"] == "done", summary
assert summary["stages_failed"] == 0, summary
assert summary["degraded"] is False, summary
print("PASS: all 25 stages completed without degradation")
PY
```

### Independent authority reconstruction

This is the primary artifact-integrity check. Do not replace it with direct
reads from legacy result paths.

```bash
.venv/bin/python - "$RUN" <<'PY'
import sys
from pathlib import Path

from researchclaw.pipeline.independent_release_reconstruction import (
    reconstruct_expected_release_publications,
)

run = Path(sys.argv[1])
snapshot = reconstruct_expected_release_publications(run)
paths = [artifact.path for artifact in snapshot.authority_artifacts]
assert len(paths) == len(set(paths)), "duplicate authority paths"
assert snapshot.stage23.manifest.path == "stage-23/stage23_verification_manifest.json"
assert snapshot.stage24.manifest.path == "stage-24/stage24_truth_manifest.json"
assert snapshot.stage25.manifest.path == "stage-25/stage25_deai_manifest.json"
assert snapshot.stage24.require_output("truth_audit.json").content
assert snapshot.stage25.audit.content
print(f"PASS: reconstructed {len(paths)} exact authority artifacts")
PY
```

### Stage 24/25 semantic success

```bash
.venv/bin/python - "$RUN" <<'PY'
import json
import sys
from pathlib import Path

from researchclaw.pipeline.independent_release_reconstruction import (
    reconstruct_expected_release_publications,
)

run = Path(sys.argv[1])
snapshot = reconstruct_expected_release_publications(run)
claims = json.loads(snapshot.stage24.require_output("claims.json").content)
truth = json.loads(snapshot.stage24.require_output("truth_audit.json").content)
deai = json.loads(snapshot.stage25.audit.content)
assert claims["counts"]["total"] > 0, claims["counts"]
assert truth["stage24_success"] is True, truth
assert truth["unsupported_count"] == 0, truth
assert deai["paper_sha256"] == truth["paper_sha256"], (deai, truth)
assert deai["recommend_only"] is True and deai["applied"] is False, deai
print("PASS: Stage 24 truth closure and Stage 25 paper binding are valid")
PY
```

### Release check

The pipeline-validation run must fail only for its non-release identity. A
missing local TeX toolchain may also be reported, but must be resolved before
the research-release run.

```bash
set +e
.venv/bin/python scripts/release_check.py "$RUN" \
  --quality-threshold 6.0 --json > /tmp/researchclaw-f0-release-check.json
RC=$?
set -e
.venv/bin/python - "$RC" <<'PY'
import json
import sys

exit_code = int(sys.argv[1])
report = json.load(open("/tmp/researchclaw-f0-release-check.json", encoding="utf-8"))
errors = {
    finding["code"]
    for finding in report["findings"]
    if finding["severity"] == "error"
}
allowed = {"non_release_claim_scope", "compile_toolchain_missing"}
unexpected = errors - allowed
assert exit_code != 0, "pipeline_validation must not be release-ready"
assert "non_release_claim_scope" in errors, errors
assert not unexpected, sorted(unexpected)
print(f"PASS: expected non-release result only: {sorted(errors)}")
PY
```

## Findings that always fail F0-PV

Any error outside the identity/toolchain allowlist fails the run, including:

- incomplete or failed stages;
- degraded quality output;
- independent reconstruction failure;
- canonical source, generation, path, or hash mismatch;
- empty, unsupported, orphaned, or numerically unclosed claims;
- invalid citation support or citation replay;
- unresolved P0/P1 critique findings;
- missing sandbox/environment provenance;
- paper hash or Stage 24/25 binding mismatch;
- stale or malformed deliverables.

Do not maintain a second hand-written allowlist of canonical/provenance error
codes here. `scripts/release_check.py` is authoritative and new error codes are
unexpected by default.

## F0-RR: research-release run

Do not start this run until all of the following exist:

- a reviewed config with `claim_scope: research_release`;
- a supported non-synthetic `dataset_origin` and canonical dataset contract;
- the required literature evidence and any PDF/full-text HITL work;
- a working LaTeX compilation path or CI-produced canonical compile result.

Define the reviewed config and a fresh output directory, then run Stage 1-25:

```bash
RELEASE_CONFIG=config.research-release.yaml
RELEASE_RUN="runs/f0-research-release-$(date +%Y%m%d-%H%M%S)"
test -f "$RELEASE_CONFIG"
test ! -e "$RELEASE_RUN"
.venv/bin/python -m researchclaw run \
  -c "$RELEASE_CONFIG" \
  -o "$RELEASE_RUN"
```

Validate completion and Stage 23-25 authority in one immutable reconstruction.
This is independent of the PV expected-failure check:

```bash
.venv/bin/python - "$RELEASE_RUN" <<'PY'
import json
import sys
from pathlib import Path

from researchclaw.pipeline.independent_release_reconstruction import (
    reconstruct_expected_release_publications,
)

run = Path(sys.argv[1])
summary = json.loads((run / "pipeline_summary.json").read_text(encoding="utf-8"))
assert summary["final_stage"] == 25, summary
assert summary["final_status"] == "done", summary
assert summary["stages_failed"] == 0, summary
assert summary["degraded"] is False, summary

snapshot = reconstruct_expected_release_publications(run)
paths = [artifact.path for artifact in snapshot.authority_artifacts]
assert len(paths) == len(set(paths)), "duplicate authority paths"
assert snapshot.stage23.manifest.path == "stage-23/stage23_verification_manifest.json"
assert snapshot.stage24.manifest.path == "stage-24/stage24_truth_manifest.json"
assert snapshot.stage25.manifest.path == "stage-25/stage25_deai_manifest.json"
claims = json.loads(snapshot.stage24.require_output("claims.json").content)
truth = json.loads(snapshot.stage24.require_output("truth_audit.json").content)
deai = json.loads(snapshot.stage25.audit.content)
assert claims["counts"]["total"] > 0, claims["counts"]
assert truth["stage24_success"] is True, truth
assert truth["unsupported_count"] == 0, truth
assert deai["paper_sha256"] == truth["paper_sha256"], (deai, truth)
assert deai["recommend_only"] is True and deai["applied"] is False, deai
print(f"PASS: research-release reconstructed {len(paths)} authority artifacts")
PY
```

Finally, require release-check exit 0 and an empty error set:

```bash
set +e
.venv/bin/python scripts/release_check.py "$RELEASE_RUN" \
  --quality-threshold 6.0 --json > /tmp/researchclaw-f0-release-check-rr.json
RC=$?
set -e
.venv/bin/python - "$RC" <<'PY'
import json
import sys

exit_code = int(sys.argv[1])
report = json.load(
    open("/tmp/researchclaw-f0-release-check-rr.json", encoding="utf-8")
)
errors = [
    finding
    for finding in report["findings"]
    if finding["severity"] == "error"
]
assert exit_code == 0, report
assert not errors, errors
assert report["status"] == "pass", report
print("PASS: research-release release_check exited 0 with no errors")
PY
```

Acceptance requires exit code 0 and no error findings. A completed pipeline,
`release_ready` field, generated PDF, or good prose is not a substitute for
this result.

## After F0

Start research-governance work as a separate workstream. First define and
implement `STOP_INSUFFICIENT_EVIDENCE`, honest negative-result publication,
and tiered release states. Stage 0 `research_brief.yaml` and the Stage 9
data-readiness gate follow. These governance changes must not be mixed into an
F0 run-fix commit.

## Non-negotiable invariants

- Empty or unsupported release claims remain blocking.
- Stage 20 quality threshold remains at least 6.0.
- `pipeline_validation` and synthetic evidence cannot pass release checks.
- Stage 12 consumes only the sealed selected candidate.
- Stage 10 smoke output never becomes experiment authority.
- Plugins cannot self-grade.
- Paper numbers must be grounded in canonical run evidence.
- Graceful degradation never exempts release-check failures.
- PDF/full-text acquisition retains its HITL and provenance boundaries.
