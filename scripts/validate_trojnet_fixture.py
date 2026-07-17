"""Run the cache-isolated TrojNet validation fixture acceptance check."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


STRICT_CHILD_ENV = "RESEARCHCLAW_TROJNET_STRICT_CHILD"
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = (
    REPO_ROOT
    / "researchclaw"
    / "experiment_runtime"
    / "validation_fixtures"
    / "trojnet_iscas85_v1"
)


def _assert_fixture_cache_free(root: Path = FIXTURE_ROOT) -> None:
    caches = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("__pycache__")
    )
    if caches:
        raise RuntimeError(
            "TrojNet fixture contains executable bytecode cache entries: "
            + ", ".join(caches)
        )


def _run_child(output: Path) -> None:
    _assert_fixture_cache_free()
    prefix = sys.pycache_prefix
    if prefix is None:
        raise RuntimeError("strict child is missing PYTHONPYCACHEPREFIX")
    resolved_prefix = Path(prefix).resolve()
    if resolved_prefix == FIXTURE_ROOT or FIXTURE_ROOT in resolved_prefix.parents:
        raise RuntimeError("strict child pycache prefix is inside the fixture")

    from researchclaw.experiment_runtime.validation_fixtures.trojnet_iscas85_v1.runner import (
        CONDITIONS,
        FIXTURE_ID,
        run_fixture,
        validate_result,
    )

    result = run_fixture("validation")
    validate_result(result)
    expected = 18 * 3 * len(CONDITIONS)
    if len(result["observations"]) != expected:
        raise RuntimeError(
            f"validation fixture produced {len(result['observations'])} observations, expected {expected}"
        )
    if len(result["per_seed"]) != len(CONDITIONS) * 3:
        raise RuntimeError("validation fixture per-seed matrix is incomplete")
    if len(result["aggregate"]) != len(CONDITIONS):
        raise RuntimeError("validation fixture aggregate matrix is incomplete")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"fixture={FIXTURE_ID} profile=validation observations={expected} "
        f"semantic_sha256={result['semantic_sha256']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if os.environ.get(STRICT_CHILD_ENV) == "1":
        _run_child(args.output)
        return

    _assert_fixture_cache_free()
    with tempfile.TemporaryDirectory(prefix="researchclaw-trojnet-pycache-") as cache:
        env = dict(os.environ)
        env[STRICT_CHILD_ENV] = "1"
        env["PYTHONPYCACHEPREFIX"] = cache
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=REPO_ROOT,
            env=env,
            check=False,
        )
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
