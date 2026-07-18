from __future__ import annotations

import asyncio
import contextvars
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    REQUIRED_CAPABILITIES,
    CanonicalEvidenceMigrationIncomplete,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_experiment_evidence import (
    publish_canonical_experiment_manifest,
    publish_experiment_evidence_candidate,
)
from researchclaw.pipeline.canonical_execution_controller import (
    CanonicalAnalysisController,
    CanonicalExecutionController,
    CanonicalRefinementController,
)
from researchclaw.pipeline.executor import execute_stage
from researchclaw.pipeline.stage15_critique import (
    _prepare_stage15_critique_namespace_under_lock,
)
from researchclaw.pipeline.stage23_verification import (
    _execute_canonical_stage23_under_lock,
)
from researchclaw.pipeline.stage24_publication import (
    Stage24PublicationError,
    _publish_after_invalidation as _publish_stage24_under_lock,
)
from researchclaw.pipeline.stage25_publication import (
    Stage25PublicationError,
    _publish_after_invalidation as _publish_stage25_under_lock,
)
from researchclaw.pipeline.independent_release_reconstruction import (
    IndependentReleaseReconstructionError,
    _build_exact_release_authority_artifacts,
    reconstruct_expected_release_publications,
    validate_release_authority_path,
)
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.stages import Stage


@pytest.mark.parametrize(
    "path",
    (
        r"stage-24\shadow.json",
        "./stage-24/output.json",
        "stage-24//output.json",
        "stage-24/%2e%2e/output.json",
        "stage-24/e\u0301.json",
    ),
)
def test_release_authority_path_rejects_noncanonical_forms(path: str) -> None:
    with pytest.raises(
        IndependentReleaseReconstructionError,
        match="noncanonical release authority path",
    ):
        validate_release_authority_path(path)


def test_release_authority_artifacts_reject_duplicate_path_even_for_same_bytes() -> None:
    with pytest.raises(
        IndependentReleaseReconstructionError,
        match="duplicate release authority path: stage-09/experiment_contract.yaml",
    ):
        _build_exact_release_authority_artifacts(
            [
                ("contract", "stage-09/experiment_contract.yaml", b"same"),
                ("shadow_role", "stage-09/experiment_contract.yaml", b"same"),
            ]
        )


def test_reconstruction_capability_guard_precedes_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_incomplete: None,
) -> None:
    run_dir = tmp_path / "missing-run"
    calls: list[str] = []

    def forbidden(_run_dir: Path) -> object:
        calls.append("capture")
        raise AssertionError("capture occurred before capability guard")

    monkeypatch.setattr(
        "researchclaw.pipeline.independent_release_reconstruction."
        "_capture_expected_release_publications",
        forbidden,
    )

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        reconstruct_expected_release_publications(run_dir)

    assert calls == []
    assert not run_dir.exists()


def test_reconstruction_requires_two_identical_disk_captures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES},
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    captures = iter((object(), object()))
    monkeypatch.setattr(
        "researchclaw.pipeline.independent_release_reconstruction."
        "_capture_expected_release_publications",
        lambda _run_dir: next(captures),
    )

    with pytest.raises(
        IndependentReleaseReconstructionError,
        match="changed during independent reconstruction",
    ):
        reconstruct_expected_release_publications(run_dir)


def test_release_reader_epoch_rejects_writer_reentry(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with ReleaseGraphLock.acquire(run_dir, "reader", mode="read"):
        with pytest.raises(RuntimeError, match="release_graph.*locked"):
            ReleaseGraphLock.acquire(run_dir, "writer", mode="write")

    with ReleaseGraphLock.acquire(run_dir, "writer", mode="write"):
        with ReleaseGraphLock.acquire(run_dir, "nested-writer", mode="write"):
            pass


def test_stage15_has_no_public_caller_authority_reconstruction_api() -> None:
    from researchclaw.pipeline import stage15_critique

    assert not hasattr(
        stage15_critique, "reconstruct_expected_stage15_critique_publication"
    )


def test_copied_context_cannot_borrow_closed_writer_epoch(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with ReleaseGraphLock.acquire(run_dir, "writer", mode="write"):
        copied = contextvars.copy_context()

    def acquire_after_owner_closed() -> None:
        with ReleaseGraphLock.acquire(run_dir, "fresh-writer", mode="write") as lease:
            assert lease._owner is None
            lease.assert_canonical()

    copied.run(acquire_after_owner_closed)


def test_async_child_outliving_parent_gets_fresh_epoch(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    async def scenario() -> bool:
        proceed = asyncio.Event()

        async def child() -> bool:
            await proceed.wait()
            with ReleaseGraphLock.acquire(
                run_dir, "async-child", mode="write"
            ) as lease:
                return lease._owner is None

        with ReleaseGraphLock.acquire(run_dir, "parent", mode="write"):
            task = asyncio.create_task(child())
        proceed.set()
        return await task

    assert asyncio.run(scenario()) is True


def test_writer_epoch_is_reacquirable_after_exception(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(RuntimeError, match="injected writer failure"):
        with ReleaseGraphLock.acquire(run_dir, "failing-writer", mode="write"):
            raise RuntimeError("injected writer failure")

    with ReleaseGraphLock.acquire(run_dir, "recovery-writer", mode="write") as lease:
        lease.assert_canonical()


def test_writer_epoch_is_reacquirable_after_async_cancellation(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def cancellable_writer() -> None:
            with ReleaseGraphLock.acquire(run_dir, "cancelled-writer", mode="write"):
                entered.set()
                await release.wait()

        task = asyncio.create_task(cancellable_writer())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        with ReleaseGraphLock.acquire(
            run_dir, "post-cancellation-writer", mode="write"
        ) as lease:
            lease.assert_canonical()

    asyncio.run(scenario())


def test_subprocess_writer_is_rejected_by_reader_flock(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    code = (
        "from pathlib import Path\n"
        "from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock\n"
        f"run = Path({str(run_dir)!r})\n"
        "try:\n"
        "    ReleaseGraphLock.acquire(run, 'child-writer', mode='write')\n"
        "except RuntimeError as exc:\n"
        "    print(str(exc))\n"
        "else:\n"
        "    raise SystemExit(3)\n"
    )
    with ReleaseGraphLock.acquire(run_dir, "reader", mode="read"):
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
        )
    assert completed.returncode == 0
    assert "release_graph_generation_locked" in completed.stdout


def test_owner_deferred_close_releases_after_last_borrower(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with ReleaseGraphLock.acquire(run_dir, "owner", mode="write"):
        borrower = ReleaseGraphLock.acquire(run_dir, "borrower", mode="write")

    with pytest.raises(RuntimeError, match="lease_inactive"):
        borrower.assert_canonical()

    code = (
        "from pathlib import Path\n"
        "from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock\n"
        f"run = Path({str(run_dir)!r})\n"
        "with ReleaseGraphLock.acquire(run, 'child-writer', mode='write'):\n"
        "    print('acquired')\n"
    )
    blocked = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert blocked.returncode != 0
    assert "release_graph_generation_locked" in blocked.stderr

    borrower.close()
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "acquired"


def test_named_lock_entry_replacement_cannot_split_directory_flock(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    named_lock = run_dir / ".canonical_release_graph.lock"
    named_lock.write_text("legacy", encoding="utf-8")
    code = (
        "from pathlib import Path\n"
        "from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock\n"
        f"run = Path({str(run_dir)!r})\n"
        "try:\n"
        "    ReleaseGraphLock.acquire(run, 'child-writer', mode='write')\n"
        "except RuntimeError as exc:\n"
        "    print(str(exc))\n"
        "else:\n"
        "    raise SystemExit(3)\n"
    )
    with ReleaseGraphLock.acquire(run_dir, "reader", mode="read") as reader:
        named_lock.unlink()
        named_lock.write_text("replacement", encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
        )
        reader.assert_canonical()
    assert completed.returncode == 0
    assert "release_graph_generation_locked" in completed.stdout


@pytest.mark.parametrize(
    "stage",
    (
        Stage.LITERATURE_COLLECT,
        Stage.LITERATURE_SCREEN,
        Stage.KNOWLEDGE_EXTRACT,
        Stage.SYNTHESIS,
        Stage.HYPOTHESIS_GEN,
        Stage.EXPERIMENT_DESIGN,
        Stage.CODE_GENERATION,
        Stage.RESOURCE_PLANNING,
    ),
)
def test_reader_epoch_rejects_stage4_through_stage11_before_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: Stage,
) -> None:
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES},
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = RCConfig.load(
        Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False
    )
    with ReleaseGraphLock.acquire(run_dir, "reader", mode="read"):
        with pytest.raises(RuntimeError, match="release_graph.*locked"):
            execute_stage(
                stage,
                run_dir=run_dir,
                run_id="locked-run",
                config=config,
                adapters=AdapterBundle(),
            )
    assert not (run_dir / f"stage-{int(stage):02d}").exists()


def test_reader_epoch_rejects_direct_stage14_publishers(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-14"
    staging = stage_dir / ".candidate-staging-test"
    staging.mkdir(parents=True)

    with ReleaseGraphLock.acquire(run_dir, "reader", mode="read"):
        before = tuple(run_dir.rglob("*"))
        with pytest.raises(RuntimeError, match="release_graph.*locked"):
            publish_experiment_evidence_candidate(
                run_dir,
                stage_dir,
                staging,
                None,  # type: ignore[arg-type]
            )
        with pytest.raises(RuntimeError, match="release_graph.*locked"):
            publish_canonical_experiment_manifest(
                run_dir,
                None,  # type: ignore[arg-type]
            )
        assert tuple(run_dir.rglob("*")) == before


def test_private_writer_helpers_reject_forged_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES},
    )

    class NamespaceSpy:
        run_dir = tmp_path / "run"

        def __getattr__(self, _name: str) -> object:
            raise AssertionError("forged lease reached namespace access")

    namespace = NamespaceSpy()
    namespace.run_dir.mkdir()
    _assert_private_writer_helpers_reject(namespace, object())


def test_private_writer_helpers_reject_inactive_reader_and_wrong_run_leases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES},
    )

    class NamespaceSpy:
        run_dir = tmp_path / "run"

        def __getattr__(self, _name: str) -> object:
            raise AssertionError("invalid lease reached namespace access")

    namespace = NamespaceSpy()
    namespace.run_dir.mkdir()
    inactive = ReleaseGraphLock.acquire(
        namespace.run_dir, "inactive", mode="write"
    )
    inactive.close()
    _assert_private_writer_helpers_reject(namespace, inactive)

    with ReleaseGraphLock.acquire(
        namespace.run_dir, "reader", mode="read"
    ) as reader:
        _assert_private_writer_helpers_reject(namespace, reader)

    other = tmp_path / "other-run"
    other.mkdir()
    with ReleaseGraphLock.acquire(other, "wrong-run", mode="write") as wrong_run:
        _assert_private_writer_helpers_reject(namespace, wrong_run)


def _assert_private_writer_helpers_reject(namespace: object, lease: object) -> None:
    run_dir = namespace.run_dir  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        _prepare_stage15_critique_namespace_under_lock(
            namespace, writer_lease=lease  # type: ignore[arg-type]
        )
    with pytest.raises(RuntimeError):
        _execute_canonical_stage23_under_lock(
            run_dir,
            run_dir / "stage-23",
            None,  # type: ignore[arg-type]
            relevance_checker=None,
            writer_lease=lease,
        )
    with pytest.raises(Stage24PublicationError, match="active release writer lease"):
        _publish_stage24_under_lock(
            namespace,  # type: ignore[arg-type]
            bundle=None,  # type: ignore[arg-type]
            runtime_config=None,  # type: ignore[arg-type]
            llm=None,
            release_lock=lease,  # type: ignore[arg-type]
        )
    with pytest.raises(Stage25PublicationError, match="active release writer lease"):
        _publish_stage25_under_lock(
            namespace,  # type: ignore[arg-type]
            source=None,  # type: ignore[arg-type]
            runtime_config=None,  # type: ignore[arg-type]
            llm=None,
            release_lock=lease,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("generation", ("stage12", "stage13", "stage14"))
def test_canonical_generation_parent_replacement_invalidates_detached_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generation: str,
) -> None:
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES},
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    if generation == "stage12":
        stage_dir = run_dir / "stage-12"
        controller = CanonicalExecutionController.prepare_generation(
            run_dir, stage_dir
        )
        commit_point = stage_dir / "experiment_result_set.json"
    elif generation == "stage13":
        stage_dir = run_dir / "stage-13"
        controller = CanonicalRefinementController.prepare_generation(
            run_dir, stage_dir
        )
        commit_point = stage_dir / "refinement_result_set.json"
    else:
        stage_dir = run_dir / "stage-14"
        controller = CanonicalAnalysisController.prepare_generation(
            run_dir, stage_dir
        )
        commit_point = run_dir / "canonical_experiment_evidence.json"

    (run_dir / "canonical_experiment_evidence.json").write_text(
        "stale", encoding="utf-8"
    )
    commit_point.write_text("stale", encoding="utf-8")
    detached = tmp_path / "run-moved"
    run_dir.rename(detached)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    run_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="run_identity_changed"):
        controller.close()

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert tuple(external.iterdir()) == (sentinel,)
    assert not (detached / "canonical_experiment_evidence.json").exists()
    assert not (detached / commit_point.relative_to(run_dir)).exists()


def test_stage12_parent_replacement_rejects_before_journal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES},
    )
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-12"
    run_dir.mkdir()
    controller = CanonicalExecutionController.prepare_generation(run_dir, stage_dir)
    detached = tmp_path / "run-moved"
    run_dir.rename(detached)
    external = tmp_path / "external"
    (external / "stage-12").mkdir(parents=True)
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    run_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="run_directory_changed"):
        controller.acquire(
            generation_binding_sha256="a" * 64,
            experiment_contract_sha256="b" * 64,
            sealed_candidate_manifest_sha256="c" * 64,
            config_semantic_sha256="d" * 64,
        )
    with pytest.raises(RuntimeError, match="run_identity_changed"):
        controller.close()

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert tuple((external / "stage-12").iterdir()) == ()
    assert not (detached / "stage-12/execution_invocation_journal.jsonl").exists()


@pytest.mark.parametrize("generation", ("stage13", "stage14"))
def test_parent_replacement_rejects_fd_bound_stage13_stage14_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generation: str,
) -> None:
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES},
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.json").write_text("{}\n", encoding="utf-8")
    if generation == "stage13":
        controller = CanonicalRefinementController.prepare_generation(
            run_dir, run_dir / "stage-13"
        )
    else:
        controller = CanonicalAnalysisController.prepare_generation(
            run_dir, run_dir / "stage-14"
        )

    detached = tmp_path / "run-moved"
    run_dir.rename(detached)
    external = tmp_path / "external"
    (external / f"stage-{13 if generation == 'stage13' else 14}").mkdir(
        parents=True
    )
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    run_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="run_directory_changed"):
        if generation == "stage13":
            controller.publish_directory_tree("evidence-v1", source)
        else:
            controller.publish_candidate_tree("cand-" + "a" * 64, source)
    with pytest.raises(RuntimeError, match="run_identity_changed"):
        controller.close()

    assert sentinel.read_text(encoding="utf-8") == "keep"
    external_stage = external / f"stage-{13 if generation == 'stage13' else 14}"
    assert tuple(external_stage.iterdir()) == ()


@pytest.mark.parametrize("generation", ("stage12", "stage13", "stage14"))
def test_prepare_generation_parent_replacement_before_canonical_lock_is_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generation: str,
) -> None:
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES},
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stage_number = {"stage12": 12, "stage13": 13, "stage14": 14}[generation]
    stage_dir = run_dir / f"stage-{stage_number}"
    stage_dir.mkdir()
    commit_name = {
        "stage12": "experiment_result_set.json",
        "stage13": "refinement_result_set.json",
        "stage14": None,
    }[generation]
    if commit_name is not None:
        (stage_dir / commit_name).write_text("stale", encoding="utf-8")
    (run_dir / "canonical_experiment_evidence.json").write_text(
        "stale", encoding="utf-8"
    )
    detached = tmp_path / "run-moved"
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    real_acquire = ReleaseGraphLock.acquire.__func__
    replaced = False

    def acquire_and_replace(
        cls: type[ReleaseGraphLock],
        target: Path,
        entrypoint: str,
        *,
        mode: str = "write",
    ) -> ReleaseGraphLock:
        nonlocal replaced
        lease = real_acquire(cls, target, entrypoint, mode=mode)
        if not replaced:
            replaced = True
            run_dir.rename(detached)
            run_dir.symlink_to(external, target_is_directory=True)
        return lease

    monkeypatch.setattr(
        ReleaseGraphLock, "acquire", classmethod(acquire_and_replace)
    )
    controller_type = {
        "stage12": CanonicalExecutionController,
        "stage13": CanonicalRefinementController,
        "stage14": CanonicalAnalysisController,
    }[generation]

    with pytest.raises(RuntimeError, match="run_identity_changed"):
        controller_type.prepare_generation(run_dir, stage_dir)

    assert tuple(external.iterdir()) == (sentinel,)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (detached / "canonical_experiment_evidence.json").exists()
    if commit_name is not None:
        assert not (detached / f"stage-{stage_number}" / commit_name).exists()


def test_tree_publication_rejects_source_file_replaced_by_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import bound_output_namespace as bound_module

    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-13"
    stage_dir.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    child = source / "payload.txt"
    child.write_text("original", encoding="utf-8")
    external = tmp_path / "external.txt"
    external.write_text("EXTERNAL-SECRET", encoding="utf-8")
    real_read = bound_module._read_all_fd
    changed = False

    def replace_before_read(descriptor: int) -> bytes:
        nonlocal changed
        if not changed:
            changed = True
            child.unlink()
            child.symlink_to(external)
        return real_read(descriptor)

    monkeypatch.setattr(bound_module, "_read_all_fd", replace_before_read)
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-13") as namespace:
        with pytest.raises(OSError, match="source changed"):
            namespace.publish_directory_tree("evidence-v1", source)
        assert "evidence-v1" not in namespace.direct_entries()


def test_tree_publication_rejects_source_directory_replaced_by_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import bound_output_namespace as bound_module

    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-13"
    stage_dir.mkdir(parents=True)
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_text("original", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "payload.txt").write_text("EXTERNAL-SECRET", encoding="utf-8")
    real_copy = bound_module._copy_tree_fd_to_fd
    calls = 0

    def replace_before_recursive_copy(source_fd: int, destination_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            moved = tmp_path / "nested-moved"
            nested.rename(moved)
            nested.symlink_to(external, target_is_directory=True)
        real_copy(source_fd, destination_fd)

    monkeypatch.setattr(
        bound_module, "_copy_tree_fd_to_fd", replace_before_recursive_copy
    )
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-13") as namespace:
        with pytest.raises(OSError, match="source changed"):
            namespace.publish_directory_tree("evidence-v1", source)
        assert "evidence-v1" not in namespace.direct_entries()


def test_tree_publication_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-13"
    stage_dir.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "pipe")

    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-13") as namespace:
        with pytest.raises(OSError, match="unsafe entry"):
            namespace.publish_directory_tree("evidence-v1", source)
        assert "evidence-v1" not in namespace.direct_entries()


def test_tree_publication_rejects_unix_socket(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-13"
    stage_dir.mkdir(parents=True)
    source = Path(tempfile.mkdtemp(prefix="rc-sock-", dir="/tmp"))
    socket_path = source / "endpoint.sock"
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        endpoint.bind(str(socket_path))
    except PermissionError:
        endpoint.close()
        source.rmdir()
        pytest.skip("sandbox forbids creating Unix-domain sockets")
    try:
        with BoundOutputNamespace.open(run_dir, stage_dir, "stage-13") as namespace:
            with pytest.raises(OSError, match="unsafe entry"):
                namespace.publish_directory_tree("evidence-v1", source)
            assert "evidence-v1" not in namespace.direct_entries()
    finally:
        endpoint.close()
        socket_path.unlink()
        source.rmdir()


def test_tree_publication_rejects_character_device_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import bound_output_namespace as bound_module

    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-13"
    stage_dir.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    (source / "device").write_text("placeholder", encoding="utf-8")
    real_open = bound_module.os.open

    def open_device_for_source_child(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "device" and dir_fd is not None:
            return real_open("/dev/null", flags)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-13") as namespace:
        monkeypatch.setattr(bound_module.os, "open", open_device_for_source_child)
        with pytest.raises(OSError, match="unsafe entry"):
            namespace.publish_directory_tree("evidence-v1", source)
        assert "evidence-v1" not in namespace.direct_entries()


def test_nested_tree_post_rename_identity_failure_rolls_back_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-14"
    candidates = stage_dir / "evidence_candidates"
    candidates.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text("{}\n", encoding="utf-8")
    candidate_id = "cand-" + "a" * 64
    detached = tmp_path / "run-moved"
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    def replace_parent_and_fail() -> None:
        run_dir.rename(detached)
        run_dir.symlink_to(external, target_is_directory=True)
        raise RuntimeError("injected identity failure")

    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-14") as namespace:
        monkeypatch.setattr(namespace, "assert_canonical", replace_parent_and_fail)
        with pytest.raises(RuntimeError, match="identity failure"):
            namespace.publish_tree_child(
                "evidence_candidates", candidate_id, source
            )
        assert namespace.read_flat_directory("evidence_candidates") == {}

    assert tuple(external.iterdir()) == (sentinel,)
    run_dir.unlink()
    detached.rename(run_dir)
    assert not (candidates / candidate_id).exists()
