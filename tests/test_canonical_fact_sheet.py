"""批次 1（D1+D2）—— Canonical Fact Sheet（CFS）+ 身份化投影。

测试先以最小 API 骨架取得逐项红，再由同 epoch execution-policy capture、
纯内存 CFS builder 和双预算身份化投影实现转绿。

语义来源：docs/STAGE17_GROUNDING_FIX_DESIGN.md v0.5 + Codex 终审（P1×4、P2×3）。
fixture 来源：runs/f0-trojnet-pv-20260721-155806 的只读 canonical artifact 拷贝，
逐文件 sha256 见 fixtures/canonical_fact_sheet/SHA256SUMS.txt（测试强制对账）。

冻结契约（Codex 终审后版本）：
1. builder 为**单参** ``build_canonical_fact_sheet(evidence)``；execution policy
   由 evidence 同 reader epoch 快照提供，禁止公共 caller-supplied policy bytes
   （Codex P1-1：关闭跨 epoch 替换窗口）。D1 须为 CanonicalExperimentEvidence
   新增 execution_policy_artifact 字段（Stage 14 ``_capture_source_files`` 已捕获
   baseline["execution_policy"]，仅需随 evidence 返回）。测试直接使用真实
   ``CanonicalExperimentEvidence``，不保留 caller-supplied bytes 包装 seam。
2. 投影行指标顺序：primary metric 在前，其后按 execution_policy.metric_keys 顺序。
3. 预算双常量（strict Decimal 实测冻结）：162 行正文为 56786 UTF-8
   字节（最长行 484）。MAX_PROJECTION_ROWS = 162，
   MAX_PROJECTION_UTF8_BYTES = 65536。56786 是 fixture 观测值（Codex P2），
   生产输出契约是 **≤ 预算**，不是逐字等于测试侧参考渲染。
4. 投影头格式（单行，budget 字段报告本次调用有效预算）：
   ``projection mode=<mode> shown=<n> total=<n> budget_rows=<r> budget_bytes=<b>``
5. 值格式化直接使用 canonical_experiment_evidence.canonical_decimal，
   禁止 float 中转（Codex P1-4）。
6. CFS 按 Mapping 访问；token 集合为排序 tuple（canonical JSON 可序列化），
   不使用 frozenset（Codex P1-4）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalEvidenceArtifact,
    CanonicalExperimentEvidence,
    _freeze_authority_value,
    _parse_json_value,
    _thaw_authority_value,
    canonical_authority_json_text,
    canonical_decimal,
)
from researchclaw.pipeline.canonical_fact_sheet import (  # noqa: F401
    CFSIntegrityError,
    MAX_PROJECTION_ROWS,
    MAX_PROJECTION_UTF8_BYTES,
    ProjectionBudgetError,
    _benchmark_tokens,
    build_canonical_fact_sheet,
    canonical_fact_sheet_sha256,
    compose_grounding_context,
    render_fact_sheet_text,
    render_observation_projection,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "canonical_fact_sheet"

METRIC_KEYS = (
    "accuracy",
    "auprc",
    "auroc",
    "f1",
    "fpr",
    "precision",
    "recall",
    "top_k_precision",
)
PRIMARY_METRIC_KEY = "auprc"
CONDITIONS = ("raw_cc1", "scoap_isolation_forest", "trojnet_community_graphsage")
PRIMARY_CONDITION = "trojnet_community_graphsage"
SEEDS = (0, 1, 2)
CIRCUIT_FAMILIES = ("c1355", "c1908", "c3540", "c432", "c6288", "c880")
VARIANTS_PER_FAMILY = 3
VARIANT_IDS = tuple(
    f"{family}_ht{variant}"
    for family in CIRCUIT_FAMILIES
    for variant in range(1, VARIANTS_PER_FAMILY + 1)
)
DATASET_NAME = "controlled_synthetic_iscas85_trojan_localization_v1"
EVALUATOR_SCHEMA = "trojnet_iscas85_graphsage_localization_v1"
EVALUATOR_ID = "trojnet_iscas85_graphsage_localization"
CLAIM_SCOPE = "pipeline_validation"
DATASET_ORIGIN = "synthetic"

# 批次 1 红测阶段实测冻结（见模块 docstring 第 3 条）。
FROZEN_MAX_PROJECTION_ROWS = 162
FROZEN_MAX_PROJECTION_UTF8_BYTES = 65536
REFERENCE_FULL_PROJECTION_UTF8_BYTES = 56786  # fixture 观测值，非生产逐字契约


# ---------------------------------------------------------------------------
# fixture 装载与 evidence 工厂
# ---------------------------------------------------------------------------


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def _real_observations_payload() -> dict[str, Any]:
    value = _parse_json_value(
        _fixture_bytes("observations-v2.json").decode("utf-8"),
        "canonical fact sheet observations fixture",
    )
    assert isinstance(value, dict)
    return value


def _real_results_payload() -> dict[str, Any]:
    return json.loads(_fixture_bytes("results-v2.json"))


def _real_rows() -> list[dict[str, Any]]:
    payload = _parse_json_value(
        _fixture_bytes("observations-v2.json").decode("utf-8"),
        "canonical fact sheet observations fixture",
    )
    assert isinstance(payload, dict)
    return list(payload["observations"])


def _real_primary_metric() -> dict[str, Any]:
    payload = _parse_json_value(
        _fixture_bytes("observations-v2.json").decode("utf-8"),
        "canonical fact sheet observations fixture",
    )
    assert isinstance(payload, dict)
    return dict(payload["primary_metric"])


def _project_flat_metric_observations() -> dict[str, list[Any]]:
    """与 stage14_domain_evaluator._project_metric_observations 同形的扁平投影。"""

    payload = _parse_json_value(
        _fixture_bytes("observations-v2.json").decode("utf-8"),
        "canonical fact sheet observations fixture",
    )
    assert isinstance(payload, dict)
    projected: dict[str, list[Any]] = {key: [] for key in METRIC_KEYS}
    for row in payload["observations"]:
        for key in METRIC_KEYS:
            projected[key].append(row["metrics"][key])
    return projected


def _make_manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "selection_policy_version": 2,
        "generation_kind": "domain_evaluator",
        "bindings": {
            "claim_scope": CLAIM_SCOPE,
            "dataset_origin": DATASET_ORIGIN,
            "dataset_name": DATASET_NAME,
            "evaluator_schema": EVALUATOR_SCHEMA,
        },
        "observation_authority": {"observation_policy_version": 1},
        "primary_metric": _real_primary_metric(),
        "selected_candidate": {"candidate_id": "cand-" + "0" * 64},
        "selected_result": {"type": "baseline", "iteration_id": None},
    }
    manifest.update(overrides)
    return manifest


def _make_candidate(**overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "schema_version": 2,
        "candidate_policy_version": 2,
        "candidate_id": "cand-" + "0" * 64,
        "bindings": {},
        "observation_authority": {"observation_policy_version": 1},
        "primary_metric": _real_primary_metric(),
        "selected_result": {"type": "baseline", "iteration_id": None},
        "artifacts": [],
    }
    candidate.update(overrides)
    return candidate


def _make_selected_result(**overrides: Any) -> dict[str, Any]:
    selected: dict[str, Any] = {
        "schema_version": 2,
        "refinement_policy_version": 2,  # 真实 run 实测值（Codex P2）
        "result_set_type": "stage13_refinement",
        "selected_result": {"type": "baseline", "iteration_id": None},
    }
    selected.update(overrides)
    return selected


def _policy_artifact(content: bytes) -> CanonicalEvidenceArtifact:
    return CanonicalEvidenceArtifact(
        role="execution_policy",
        path="stage-09/domain_evaluator_execution_policy.json",
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _make_evidence(**overrides: Any) -> CanonicalExperimentEvidence:
    policy_artifact = overrides.pop(
        "execution_policy_artifact",
        _policy_artifact(_fixture_bytes("execution_policy.json")),
    )
    observations_bytes = _fixture_bytes("observations-v2.json")
    contract_bytes = _fixture_bytes("experiment_contract.yaml")
    fields: dict[str, Any] = {
        "manifest_path": "canonical_experiment_evidence.json",
        "manifest_sha256": "0" * 64,
        "manifest": _make_manifest(),
        "candidate_manifest_path": (
            "stage-14/evidence_candidates/cand-" + "0" * 64
            + "/experiment_evidence_candidate.json"
        ),
        "candidate_manifest_sha256": "0" * 64,
        "candidate": _make_candidate(),
        "selected_result_manifest_path": "stage-13/refinement_result_set.json",
        "selected_result_manifest_sha256": "0" * 64,
        "selected_result": _make_selected_result(),
        "selected_execution_artifact": CanonicalEvidenceArtifact(
            role="observations",
            path="stage-12/evidence-v2/observations.json",
            sha256=hashlib.sha256(observations_bytes).hexdigest(),
            content=observations_bytes,
        ),
        "experiment_contract_path": "stage-09/experiment_contract.yaml",
        "experiment_contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "experiment_contract_bytes": contract_bytes,
        "run_config_path": "config.yaml",
        "run_config_sha256": "0" * 64,
        "run_config_bytes": b"{}",
        "summary_bytes": b"{}",
        "summary": {},
        "analysis_bytes": b"",
        "analysis_text": "",
        "metric_observations": _project_flat_metric_observations(),
        "structured_results": _parse_json_value(
            _fixture_bytes("results-v2.json").decode("utf-8"),
            "canonical fact sheet results fixture",
        ),
        "artifacts": (),
        "project_artifacts": (),
        "execution_policy_artifact": policy_artifact,
    }
    fields.update(overrides)
    return CanonicalExperimentEvidence(**fields)


def _build_cfs(evidence: Any = None) -> Mapping[str, Any]:
    cfs = build_canonical_fact_sheet(
        evidence if evidence is not None else _make_evidence()
    )
    assert cfs is not None, "有效 domain-evaluator 证据必须生成 CFS"
    return cfs


# ---------------------------------------------------------------------------
# 冻结投影格式的测试侧参考渲染器（实测依据；生产实现须满足 ≤ 预算契约）
# ---------------------------------------------------------------------------


def _format_metric_value(value: Any) -> str:
    """Codex P1-4：直接使用仓库既有 canonical_decimal，禁止 float 中转。"""

    return canonical_decimal(value)


def _projection_metric_order() -> tuple[str, ...]:
    return (PRIMARY_METRIC_KEY,) + tuple(
        key for key in METRIC_KEYS if key != PRIMARY_METRIC_KEY
    )


def _reference_projection_row(row: Mapping[str, Any]) -> str:
    identity = f"{row['condition']}/seed={row['seed']}/{row['circuit_variant']}"
    metrics = " ".join(
        f"{key}={_format_metric_value(row['metrics'][key])}"
        for key in _projection_metric_order()
    )
    return f"obs {identity}: {metrics}"


def _reference_projection_header(mode: str, shown: int, total: int) -> str:
    return (
        f"projection mode={mode} shown={shown} total={total} "
        f"budget_rows={FROZEN_MAX_PROJECTION_ROWS} "
        f"budget_bytes={FROZEN_MAX_PROJECTION_UTF8_BYTES}"
    )


def _reference_full_projection_text() -> str:
    rows = _real_rows()
    lines = [_reference_projection_header("full", len(rows), len(rows))]
    lines.extend(_reference_projection_row(row) for row in rows)
    return "\n".join(lines)


def _normalize_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _expected_circuit_tokens() -> tuple[str, ...]:
    return tuple(
        sorted(
            [_normalize_token(family) for family in CIRCUIT_FAMILIES]
            + [_normalize_token(variant) for variant in VARIANT_IDS]
        )
    )


# ---------------------------------------------------------------------------
# 篡改/解析辅助
# ---------------------------------------------------------------------------


def _evidence_with_observations_payload(
    payload: dict[str, Any],
) -> CanonicalExperimentEvidence:
    raw = canonical_authority_json_text(payload).encode("utf-8")
    return _evidence_with_observations_bytes(raw)


def _evidence_with_observations_bytes(raw: bytes) -> CanonicalExperimentEvidence:
    return _make_evidence(
        selected_execution_artifact=CanonicalEvidenceArtifact(
            role="observations",
            path="stage-12/evidence-v2/observations.json",
            sha256=hashlib.sha256(raw).hexdigest(),
            content=raw,
        )
    )


def _evidence_with_synchronized_observation_results(
    observations: dict[str, Any],
    *,
    primary_metric: dict[str, Any] | None = None,
) -> CanonicalExperimentEvidence:
    evidence = _evidence_with_observations_payload(observations)
    results = copy.deepcopy(
        _parse_json_value(
            _fixture_bytes("results-v2.json").decode("utf-8"),
            "canonical fact sheet results fixture",
        )
    )
    results["structured_results"] = copy.deepcopy(observations["aggregate"])
    results["observations"] = {
        "path": evidence.selected_execution_artifact.path,
        "sha256": evidence.selected_execution_artifact.sha256,
        "size": len(evidence.selected_execution_artifact.content),
    }
    overrides: dict[str, Any] = {
        "selected_execution_artifact": evidence.selected_execution_artifact,
        "structured_results": results,
    }
    if primary_metric is not None:
        results["primary_metric"] = copy.deepcopy(primary_metric)
        overrides.update(
            manifest=_make_manifest(primary_metric=copy.deepcopy(primary_metric)),
            candidate=_make_candidate(primary_metric=copy.deepcopy(primary_metric)),
        )
    return _make_evidence(**overrides)


def _evidence_with_dataset_name(name: str) -> CanonicalExperimentEvidence:
    contract_bytes = (
        _fixture_bytes("experiment_contract.yaml")
        .decode("utf-8")
        .replace(DATASET_NAME, name)
        .encode("utf-8")
    )
    manifest = _make_manifest()
    manifest["bindings"] = {**manifest["bindings"], "dataset_name": name}
    return _make_evidence(
        manifest=manifest,
        experiment_contract_sha256=hashlib.sha256(contract_bytes).hexdigest(),
        experiment_contract_bytes=contract_bytes,
    )


def _tampered_policy_bytes(**mutations: Any) -> bytes:
    policy = json.loads(_fixture_bytes("execution_policy.json"))
    for dotted_key, value in mutations.items():
        target = policy
        parts = dotted_key.split(".")
        for part in parts[:-1]:
            target = target[part]
        if value is None:
            del target[parts[-1]]
        else:
            target[parts[-1]] = value
    return json.dumps(policy, sort_keys=True).encode("utf-8")


def _evidence_with_policy_payload(
    policy: dict[str, Any],
) -> CanonicalExperimentEvidence:
    policy_bytes = canonical_authority_json_text(policy).encode("utf-8")
    contract = yaml.safe_load(_fixture_bytes("experiment_contract.yaml"))
    contract["evaluator_authority"]["execution_policy_snapshot_sha256"] = (
        hashlib.sha256(policy_bytes).hexdigest()
    )
    contract_bytes = yaml.safe_dump(contract, sort_keys=False).encode("utf-8")
    return _make_evidence(
        execution_policy_artifact=_policy_artifact(policy_bytes),
        experiment_contract_bytes=contract_bytes,
        experiment_contract_sha256=hashlib.sha256(contract_bytes).hexdigest(),
    )


def _synthetic_rows(count_per_group: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for condition in CONDITIONS:
        for seed in SEEDS:
            for index in range(count_per_group):
                rows.append(
                    {
                        "condition": condition,
                        "seed": seed,
                        "circuit_variant": f"c{index:04d}_ht1",
                        "metrics": {key: Decimal("0.5") for key in METRIC_KEYS},
                    }
                )
    return rows


def _render_projection(
    rows: list[dict[str, Any]],
    *,
    max_rows: int = FROZEN_MAX_PROJECTION_ROWS,
    max_bytes: int = FROZEN_MAX_PROJECTION_UTF8_BYTES,
) -> str:
    """冻结的生产 API 调用形：render_observation_projection(rows, *, ...)."""
    return render_observation_projection(
        rows,
        primary_metric_key=PRIMARY_METRIC_KEY,
        metric_keys=METRIC_KEYS,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )


def _parse_projection(text: str) -> tuple[dict[str, str], list[str]]:
    lines = text.split("\n")
    header = lines[0]
    assert header.startswith("projection "), "投影头必须以 projection 起始"
    fields = dict(item.split("=", 1) for item in header.split()[1:])
    return fields, lines[1:]


def _obs_identity(line: str) -> str:
    assert line.startswith("obs "), f"非法投影行: {line!r}"
    return line[len("obs ") :].split(": ", 1)[0]


_OBS_ROW_RE = re.compile(r"^obs \S+: [a-z0-9_]+=\S+( [a-z0-9_]+=\S+){7}$")


# 覆盖 1：exact activation predicate 正反分支。
class TestActivationPredicate:
    def test_domain_evaluator_v2_activates(self) -> None:
        cfs = _build_cfs()
        assert cfs["schema_version"] == 1

    @pytest.mark.parametrize(
        "evidence",
        [
            _make_evidence(manifest=_make_manifest(schema_version=1)),
            _make_evidence(manifest=_make_manifest(generation_kind="generic")),
            _make_evidence(candidate=_make_candidate(schema_version=1)),
            _make_evidence(selected_result=_make_selected_result(schema_version=1)),
            _make_evidence(
                selected_result=_make_selected_result(
                    result_set_type="stage12_baseline"
                )
            ),
        ],
        ids=[
            "manifest_schema_v1",
            "generation_kind_generic",
            "candidate_schema_v1",
            "selected_result_schema_v1",
            "result_set_type_baseline",
        ],
    )
    def test_non_domain_evaluator_returns_none(
        self, evidence: CanonicalExperimentEvidence
    ) -> None:
        # 不抛错、不启发式降级：精确不满足即无 CFS。
        assert build_canonical_fact_sheet(evidence) is None


# 覆盖 2：同一 snapshot 重复派生 hash 一致（纯内存投影、零实时读取）。
class TestDerivationDeterminism:
    def test_build_does_not_reopen_trusted_selector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_selector(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("CFS contract replay reopened trusted selector")

        monkeypatch.setattr(
            "researchclaw.experiment_runtime.contract.select_metric_authority",
            fail_selector,
        )
        assert _build_cfs()["schema_version"] == 1

    def test_sha256_stable_across_builds(self) -> None:
        first = _build_cfs()
        second = _build_cfs()
        assert canonical_fact_sheet_sha256(first) == canonical_fact_sheet_sha256(
            second
        )
        assert first == second

    @pytest.mark.parametrize(
        "view", ["introduction", "method", "results", "limitations"]
    )
    def test_view_render_deterministic(self, view: str) -> None:
        cfs = _build_cfs()
        assert render_fact_sheet_text(cfs, view=view) == render_fact_sheet_text(
            cfs, view=view
        )


# 覆盖 3：canonical 字段篡改、runtime 不一致均拒绝（CFSIntegrityError）。
class TestCanonicalTamperRejected:
    def test_execution_policy_unknown_field_rejected_with_bound_hash(self) -> None:
        policy = _parse_json_value(
            _fixture_bytes("execution_policy.json").decode("utf-8"),
            "canonical fact sheet execution policy fixture",
        )
        assert isinstance(policy, dict)
        policy["unknown_authority"] = "poison"
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_evidence_with_policy_payload(policy))

    def test_duplicate_contract_key_rejected(self) -> None:
        contract_bytes = _fixture_bytes("experiment_contract.yaml") + (
            b"\nclaim_scope: pipeline_validation\n"
        )
        with pytest.raises(CFSIntegrityError):
            _build_cfs(
                _make_evidence(
                    experiment_contract_bytes=contract_bytes,
                    experiment_contract_sha256=hashlib.sha256(
                        contract_bytes
                    ).hexdigest(),
                )
            )

    def test_contract_nested_unknown_field_rejected(self) -> None:
        contract = yaml.safe_load(_fixture_bytes("experiment_contract.yaml"))
        contract["metric_authority"]["unknown_authority"] = "poison"
        contract_bytes = yaml.safe_dump(contract, sort_keys=False).encode("utf-8")
        with pytest.raises(CFSIntegrityError):
            _build_cfs(
                _make_evidence(
                    experiment_contract_bytes=contract_bytes,
                    experiment_contract_sha256=hashlib.sha256(
                        contract_bytes
                    ).hexdigest(),
                )
            )

    @pytest.mark.parametrize("location", ["top", "row"])
    def test_observation_unknown_field_rejected(self, location: str) -> None:
        payload = copy.deepcopy(_real_observations_payload())
        if location == "top":
            payload["unknown_authority"] = "poison"
        else:
            payload["observations"][0]["unknown_authority"] = "poison"
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_evidence_with_observations_payload(payload))

    def test_results_unknown_field_rejected(self) -> None:
        payload = copy.deepcopy(
            _parse_json_value(
                _fixture_bytes("results-v2.json").decode("utf-8"),
                "canonical fact sheet results fixture",
            )
        )
        payload["unknown_authority"] = "poison"
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_make_evidence(structured_results=payload))

    def test_observation_metric_tamper_rejected(self) -> None:
        payload = _real_observations_payload()
        payload["observations"][0]["metrics"]["auprc"] = Decimal("0.001")
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_evidence_with_observations_payload(payload))

    def test_structured_results_aggregate_tamper_rejected(self) -> None:
        payload = copy.deepcopy(
            _parse_json_value(
                _fixture_bytes("results-v2.json").decode("utf-8"),
                "canonical fact sheet results fixture",
            )
        )
        payload["structured_results"][0]["metrics"]["auprc"]["mean"] += Decimal(
            "0.01"
        )
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_make_evidence(structured_results=payload))

    def test_observation_identity_tamper_rejected(self) -> None:
        payload = _real_observations_payload()
        payload["observations"][0]["seed"] = 7  # 身份闭包外 seed
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_evidence_with_observations_payload(payload))

    def test_bool_metric_value_rejected(self) -> None:
        payload = _real_observations_payload()
        payload["observations"][0]["metrics"]["auprc"] = True
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_evidence_with_observations_payload(payload))

    def test_nonfinite_metric_value_rejected(self) -> None:
        raw = re.sub(
            rb'"auprc":(?:-?[0-9]+(?:\.[0-9]+)?)',
            b'"auprc":NaN',
            _fixture_bytes("observations-v2.json"),
            count=1,
        )
        assert raw != _fixture_bytes("observations-v2.json")
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_evidence_with_observations_bytes(raw))

    def test_per_seed_metric_tamper_rejected(self) -> None:
        payload = copy.deepcopy(_real_observations_payload())
        payload["per_seed"][0]["metrics"]["auprc"] += Decimal("0.01")
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_evidence_with_observations_payload(payload))

    def test_duplicate_per_seed_identity_rejected(self) -> None:
        payload = copy.deepcopy(_real_observations_payload())
        payload["per_seed"][1] = copy.deepcopy(payload["per_seed"][0])
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_evidence_with_observations_payload(payload))

    def test_synchronized_condition_aggregate_tamper_rejected(self) -> None:
        observations = copy.deepcopy(_real_observations_payload())
        observations["aggregate"][0]["metrics"]["auprc"]["mean"] += Decimal(
            "0.01"
        )
        results = copy.deepcopy(
            _parse_json_value(
                _fixture_bytes("results-v2.json").decode("utf-8"),
                "canonical fact sheet results fixture",
            )
        )
        results["structured_results"] = copy.deepcopy(observations["aggregate"])
        evidence = _evidence_with_observations_payload(observations)
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_make_evidence(
                selected_execution_artifact=evidence.selected_execution_artifact,
                structured_results=results,
            ))

    def test_sub_tolerance_aggregate_cannot_become_cfs_authority(self) -> None:
        baseline = _build_cfs()
        observations = copy.deepcopy(_real_observations_payload())
        with localcontext() as context:
            context.prec = 100
            observations["aggregate"][0]["metrics"]["accuracy"]["mean"] += (
                Decimal("5e-50")
            )
        tampered_value = observations["aggregate"][0]["metrics"]["accuracy"][
            "mean"
        ]

        replayed = _build_cfs(
            _evidence_with_synchronized_observation_results(observations)
        )

        assert replayed["condition_aggregates"] == baseline["condition_aggregates"]
        assert canonical_fact_sheet_sha256(replayed) == canonical_fact_sheet_sha256(
            baseline
        )
        assert (
            replayed["condition_aggregates"][0]["metrics"]["accuracy"]["mean"]
            != tampered_value
        )

    def test_sub_tolerance_primary_cannot_become_cfs_authority(self) -> None:
        baseline = _build_cfs()
        observations = copy.deepcopy(_real_observations_payload())
        primary = copy.deepcopy(observations["primary_metric"])
        with localcontext() as context:
            context.prec = 100
            primary["value"] += Decimal("5e-50")
        observations["primary_metric"] = copy.deepcopy(primary)

        replayed = _build_cfs(
            _evidence_with_synchronized_observation_results(
                observations,
                primary_metric=primary,
            )
        )

        assert replayed["primary_metric"] == baseline["primary_metric"]
        assert replayed["primary_metric"]["value"] != primary["value"]

    def test_policy_runtime_tamper_rejected(self) -> None:
        # sha256(policy) 与 contract evaluator_authority 绑定不符即拒绝；
        # policy 只能来自 evidence 同 epoch 快照（Codex P1-1）。
        with pytest.raises(CFSIntegrityError):
            build_canonical_fact_sheet(
                _make_evidence(
                    execution_policy_artifact=_policy_artifact(
                        _tampered_policy_bytes(
                            **{"runtime_projection.packages.torch": "9.9.9"}
                        )
                    )
                )
            )

    def test_policy_missing_runtime_projection_rejected(self) -> None:
        with pytest.raises(CFSIntegrityError):
            build_canonical_fact_sheet(
                _make_evidence(
                    execution_policy_artifact=_policy_artifact(
                        _tampered_policy_bytes(runtime_projection=None)
                    )
                )
            )


# 覆盖 4：benchmark/circuit token 规范化（v0.5 §3.1-B + Codex P1-4 排序 tuple）。
class TestBoundLabelTokens:
    def test_benchmark_tokens_exact(self) -> None:
        labels = _build_cfs()["bound_labels"]
        assert labels["benchmark_tokens"] == ("iscas85",)
        assert labels["circuit_tokens"] == _expected_circuit_tokens()

    def test_version_and_circuit_ids_not_benchmark_tokens(self) -> None:
        labels = _build_cfs()["bound_labels"]
        assert "v1" not in labels["benchmark_tokens"]
        assert "c1355" not in labels["benchmark_tokens"]
        assert "c1355" in labels["circuit_tokens"]

    def test_non_source_segments_excluded(self) -> None:
        labels = _build_cfs()["bound_labels"]
        for token in (
            "trojan",
            "graphsage",
            "community",
            "localization",
            "controlled",
            "synthetic",
        ):
            assert token not in labels["benchmark_tokens"]
            assert token not in labels["circuit_tokens"]

    def test_bound_label_parse_casefolded(self) -> None:
        assert _benchmark_tokens(("ISCAS85",)) == ("iscas85",)


# 覆盖 5：162 行投影预算实测并冻结双常量。
class TestProjectionBudgetFrozen:
    def test_production_constants_match_frozen_values(self) -> None:
        assert MAX_PROJECTION_ROWS == FROZEN_MAX_PROJECTION_ROWS == 162
        assert (
            MAX_PROJECTION_UTF8_BYTES == FROZEN_MAX_PROJECTION_UTF8_BYTES == 65536
        )

    def test_reference_full_projection_fixture_observation(self) -> None:
        """56786 是 strict fixture 观测值，只约束参考渲染器漂移。"""
        text = _reference_full_projection_text()
        lines = text.split("\n")
        assert len(lines) - 1 == FROZEN_MAX_PROJECTION_ROWS  # 162 行正文
        assert len(text.encode("utf-8")) == REFERENCE_FULL_PROJECTION_UTF8_BYTES
        assert len(text.encode("utf-8")) <= FROZEN_MAX_PROJECTION_UTF8_BYTES


# 覆盖 6：full / sampled / aggregate_only 及 shown/total/mode + 硬预算契约。
class TestProjectionModes:
    def test_full_mode_covers_all_identities(self) -> None:
        text = _render_projection(_real_rows())
        fields, rows = _parse_projection(text)
        assert fields["mode"] == "full"
        assert fields["shown"] == str(len(_real_rows())) == "162"
        assert fields["total"] == str(len(_real_rows()))
        expected = {
            f"{row['condition']}/seed={row['seed']}/{row['circuit_variant']}"
            for row in _real_rows()
        }
        assert {_obs_identity(line) for line in rows} == expected
        # Codex P1-3：full 输出同时满足行/字节双预算。
        assert len(rows) <= FROZEN_MAX_PROJECTION_ROWS
        assert len(text.encode("utf-8")) <= FROZEN_MAX_PROJECTION_UTF8_BYTES

    def test_full_mode_primary_metric_first(self) -> None:
        _, rows = _parse_projection(_render_projection(_real_rows()))
        for line in rows:
            assert line.split(": ", 1)[1].startswith("auprc=")

    def test_no_half_line_truncation(self) -> None:
        # Codex P1-3：每行必须是完整 8 指标行，不得截断半行。
        _, rows = _parse_projection(_render_projection(_real_rows()))
        assert len(rows) == len(_real_rows())
        for line in rows:
            assert _OBS_ROW_RE.fullmatch(line), f"半行或指标缺失: {line!r}"

    def test_sampled_mode_deterministic_proportional_unique(self) -> None:
        rows = _synthetic_rows(60)  # 3 条件 × 3 seeds × 60 = 540 行
        first = _render_projection(rows)
        second = _render_projection(rows)
        assert first == second, "sampled 渲染必须确定性一致"
        fields, lines = _parse_projection(first)
        assert fields["mode"] == "sampled"
        assert int(fields["shown"]) <= FROZEN_MAX_PROJECTION_ROWS
        assert fields["total"] == "540"
        assert int(fields["shown"]) == len(lines), "shown 不得无声截断"
        # Codex P1-3：字节预算硬契约 + 身份唯一。
        assert len(first.encode("utf-8")) <= FROZEN_MAX_PROJECTION_UTF8_BYTES
        identities = [_obs_identity(line) for line in lines]
        assert len(set(identities)) == len(identities), "sampled 身份必须唯一"
        groups: dict[str, int] = {}
        for identity in identities:
            group = "/".join(identity.split("/")[0:2])
            groups[group] = groups.get(group, 0) + 1
        assert len(set(groups.values())) == 1, "须按 condition×seed 等比例采样"
        assert len(groups) == len(CONDITIONS) * len(SEEDS)

    def test_aggregate_only_under_tight_byte_budget(self) -> None:
        text = _render_projection(_real_rows(), max_bytes=100)
        fields, rows = _parse_projection(text)
        assert fields["mode"] == "aggregate_only"
        assert fields["shown"] == "0"
        assert fields["total"] == str(len(_real_rows()))
        assert not any(
            line.startswith("obs ") for line in rows
        ), "aggregate_only 不得含逐行观察"
        assert len(text.encode("utf-8")) <= 100

    def test_header_over_budget_fails_deterministically(self) -> None:
        # Codex P1-3：header 自身即超预算 → 确定性报错，不得输出残缺文本。
        with pytest.raises(ProjectionBudgetError):
            _render_projection(_real_rows(), max_bytes=10)


# 覆盖 6b：指标值域对抗（Codex P1-4）。
class TestMetricValueDomain:
    def _row_with_value(self, value: Any) -> dict[str, Any]:
        return {
            "condition": "raw_cc1",
            "seed": 0,
            "circuit_variant": "c1355_ht1",
            "metrics": {
                **{key: Decimal("0.5") for key in METRIC_KEYS},
                "auprc": value,
            },
        }

    def test_high_precision_decimal_preserved(self) -> None:
        precise = Decimal("0.123456789012345678901234567890")
        text = _render_projection([self._row_with_value(precise)])
        assert f"auprc={canonical_decimal(precise)}" in text, (
            "Decimal 有效精度不得经 float 中转丢失"
        )

    @pytest.mark.parametrize(
        "bad",
        [True, False, float("nan"), float("inf"), float("-inf")],
        ids=["bool_true", "bool_false", "nan", "pos_inf", "neg_inf"],
    )
    def test_invalid_metric_values_rejected(self, bad: Any) -> None:
        with pytest.raises(CFSIntegrityError):
            _render_projection([self._row_with_value(bad)])

    @pytest.mark.parametrize("bad", [0.1, 0.5])
    def test_finite_binary_float_rejected(self, bad: float) -> None:
        with pytest.raises(CFSIntegrityError):
            _render_projection([self._row_with_value(bad)])


# 覆盖 7：section-scoped prompt 视图隔离。
class TestSectionScopedViews:
    def test_introduction_view_scope_without_metrics_or_runtime(self) -> None:
        text = render_fact_sheet_text(_build_cfs(), view="introduction")
        for condition in CONDITIONS:
            assert condition in text
        assert CLAIM_SCOPE in text
        assert "iscas85" in text.casefold()
        assert repr(_real_primary_metric()["value"])[:6] not in text
        assert "auprc=" not in text
        assert "torch" not in text.casefold()

    def test_method_view_runtime_and_roles_without_aggregates(self) -> None:
        text = render_fact_sheet_text(_build_cfs(), view="method")
        assert "torch" in text.casefold()
        assert "2.12.1" in text
        assert "cpu" in text.casefold()
        assert "primary" in text
        assert "comparator" in text
        assert "162" in text
        assert "invocation" in text.casefold()
        assert repr(_real_primary_metric()["value"])[:6] not in text

    def test_results_view_aggregates_and_projection(self) -> None:
        cfs = _build_cfs()
        text = render_fact_sheet_text(cfs, view="results")
        assert canonical_decimal(cfs["primary_metric"]["value"]) in text
        raw_cc1 = next(
            item
            for item in cfs["condition_aggregates"]
            if item["condition"] == "raw_cc1"
        )
        assert canonical_decimal(raw_cc1["metrics"]["auprc"]["mean"]) in text
        assert f"obs {PRIMARY_CONDITION}/seed=0/" in text

    def test_limitations_view_scope_boundary_and_primary(self) -> None:
        cfs = _build_cfs()
        text = render_fact_sheet_text(cfs, view="limitations")
        assert CLAIM_SCOPE in text
        assert canonical_decimal(cfs["primary_metric"]["value"]) in text

    @pytest.mark.parametrize(
        "view", ["introduction", "method", "results", "limitations"]
    )
    def test_views_use_data_only_fenced_block(self, view: str) -> None:
        assert "```" in render_fact_sheet_text(_build_cfs(), view=view)

    def test_compose_orders_cfs_before_projection(self) -> None:
        context = compose_grounding_context(_build_cfs(), view="results")
        # 冻结标记：CFS 文本块含 canonical_fact_sheet 标识且先于投影块。
        assert context.index("canonical_fact_sheet") < context.index(
            "projection mode="
        )

    def test_results_projection_is_inside_data_only_fence(self) -> None:
        text = render_fact_sheet_text(_build_cfs(), view="results")
        projection_index = text.index("projection mode=")
        fence_ranges: list[tuple[int, int]] = []
        cursor = 0
        while True:
            start = text.find("```", cursor)
            if start < 0:
                break
            content_start = text.find("\n", start) + 1
            end = text.find("```", content_start)
            assert content_start > 0 and end >= 0
            fence_ranges.append((content_start, end))
            cursor = end + 3
        assert any(start <= projection_index < end for start, end in fence_ranges)


class TestPromptFacingIdentityGrammar:
    @pytest.mark.parametrize(
        "dataset_name",
        ["iscas85\npoison", "iscas85`poison", "x" * 129],
        ids=["newline", "backtick", "overlong"],
    )
    def test_dataset_identity_injection_rejected(self, dataset_name: str) -> None:
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_evidence_with_dataset_name(dataset_name))

    @pytest.mark.parametrize(
        "evaluator_schema",
        ["schema\npoison", "schema`poison", "x" * 129],
        ids=["newline", "backtick", "overlong"],
    )
    def test_root_evaluator_schema_injection_rejected(
        self, evaluator_schema: str
    ) -> None:
        manifest = _make_manifest()
        manifest["bindings"] = {
            **manifest["bindings"],
            "evaluator_schema": evaluator_schema,
        }
        with pytest.raises(CFSIntegrityError):
            _build_cfs(_make_evidence(manifest=manifest))

    def test_ml_hep_grounding_instruction_parity(self) -> None:
        from researchclaw.prompts import hep, ml

        assert isinstance(ml.CFS_GROUNDING_INSTRUCTION, str)
        assert ml.CFS_GROUNDING_INSTRUCTION
        assert ml.CFS_GROUNDING_INSTRUCTION == hep.CFS_GROUNDING_INSTRUCTION


# 覆盖 8：generic v1 零 CFS 介入。
class TestGenericV1NoCFS:
    @pytest.mark.parametrize(
        "evidence",
        [
            _make_evidence(manifest=_make_manifest(generation_kind="generic")),
            _make_evidence(manifest=_make_manifest(schema_version=1)),
        ],
        ids=["generation_kind_generic", "manifest_schema_v1"],
    )
    def test_generic_evidence_yields_no_cfs(
        self, evidence: CanonicalExperimentEvidence
    ) -> None:
        assert build_canonical_fact_sheet(evidence) is None

    def test_compose_rejects_missing_cfs(self) -> None:
        # 调用方契约：generic 路径不得调用 compose；误调用必须显式失败。
        with pytest.raises(ValueError):
            compose_grounding_context(None, view="method")


# 覆盖 9（Codex P2）：fixture 来源 exact closure + 逐文件 sha256 对账。
class TestFixtureIntegrity:
    def test_fixture_exact_closure_and_hashes(self) -> None:
        manifest_text = (FIXTURE_DIR / "SHA256SUMS.txt").read_text("utf-8")
        entries: dict[str, str] = {}
        for line in manifest_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            digest, name = line.split(None, 1)
            entries[name.strip()] = digest
        expected = {
            "observations-v2.json",
            "results-v2.json",
            "experiment_contract.yaml",
            "execution_policy.json",
        }
        assert set(entries) == expected, "SHA manifest 必须精确覆盖四个 fixture"
        on_disk = {p.name for p in FIXTURE_DIR.iterdir() if p.is_file()}
        assert on_disk == expected | {"SHA256SUMS.txt"}, "fixture 目录不得有额外文件"
        for name, digest in entries.items():
            actual = hashlib.sha256((FIXTURE_DIR / name).read_bytes()).hexdigest()
            assert actual == digest, f"fixture {name} 内容与来源对账失败"


# 覆盖 10（Codex 终审 R2 P1-2）：CFS 不可变性与 canonical 序列化对抗。
class TestCFSImmutabilityAndSerialization:
    def test_top_level_mapping_immutable(self) -> None:
        cfs = _build_cfs()
        with pytest.raises(TypeError):
            cfs["counts"] = {}  # type: ignore[index]

    def test_nested_mapping_immutable(self) -> None:
        cfs = _build_cfs()
        with pytest.raises(TypeError):
            cfs["bound_labels"]["benchmark_tokens"] = ()  # type: ignore[index]

    def test_nested_tuple_not_replaceable(self) -> None:
        cfs = _build_cfs()
        tokens = cfs["bound_labels"]["benchmark_tokens"]
        with pytest.raises(TypeError):
            tokens[0] = "forged"  # type: ignore[index]

    def test_canonical_serializer_encodes_complete_cfs(self) -> None:
        # 复用仓库 freeze/thaw 语义：CFS 必须可被 canonical serializer 完整编码。
        cfs = _build_cfs()
        text = canonical_authority_json_text(_thaw_authority_value(cfs))
        assert json.loads(text), "canonical serializer 必须完整编码 CFS"

    def test_freeze_thaw_round_trip_hash_stable(self) -> None:
        cfs = _build_cfs()
        serialized = canonical_authority_json_text(_thaw_authority_value(cfs))
        refrozen = _freeze_authority_value(
            _parse_json_value(serialized, "canonical fact sheet round trip")
        )
        assert canonical_fact_sheet_sha256(refrozen) == canonical_fact_sheet_sha256(
            cfs
        )
        assert canonical_fact_sheet_sha256(cfs) == canonical_fact_sheet_sha256(cfs)
