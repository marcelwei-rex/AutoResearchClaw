"""Produce deterministic TrojNet node-score evidence from captured inputs.

This trusted package emits raw scores only. Labels, metrics, and aggregates are
owned by the separately captured verifier.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable


def _normalise(values: Any) -> Any:
    import numpy as np

    array = np.asarray(values, dtype=np.float64)
    low = float(array.min())
    high = float(array.max())
    if high <= low:
        return np.zeros_like(array)
    return (array - low) / (high - low)


def _score_raw_cc1(data: Any, seed: int) -> Any:
    del seed
    import torch

    return torch.tensor(_normalise(data.x[:, 14].numpy()), dtype=torch.float32)


def _score_scoap_if(data: Any, seed: int) -> Any:
    import numpy as np
    import torch
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler

    features = data.x[:, [13, 14, 15]].numpy().astype(np.float64)
    if float(features.std(axis=0).max()) < 1e-8:
        return torch.zeros(data.num_nodes, dtype=torch.float32)
    scaled = StandardScaler().fit_transform(features)
    model = IsolationForest(
        n_estimators=200,
        contamination=0.05,
        random_state=seed,
        n_jobs=1,
    )
    model.fit(scaled)
    return torch.tensor(
        _normalise(-model.decision_function(scaled)), dtype=torch.float32
    )


def _score_trojnet(data: Any, seed: int, epochs: int) -> Any:
    import numpy as np
    import torch
    from trojnet.anomaly import AnomalyScorer
    from trojnet.partition import LouvainPartition
    from trojnet.train import TrainConfig, TrojNetTrainer

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    config = TrainConfig(
        gnn_type="GraphSAGE",
        in_dim=data.x.size(1),
        hidden_dim=128,
        out_dim=64,
        proj_dim=32,
        epochs=epochs,
        lr=1e-3,
        temperature=0.5,
        p_feat=0.3,
        p_edge=0.4,
        device="cpu",
        seed=seed,
        log_every=epochs,
    )
    partitioner = LouvainPartition()
    _, data_with_ids = partitioner.partition(data)
    trainer = TrojNetTrainer(config=config, partitioner=partitioner)
    trainer.fit([data_with_ids])
    embeddings = trainer.embed(data_with_ids)
    return AnomalyScorer(
        contamination=0.05,
        random_state=seed,
        n_jobs=1,
    ).fit_score(embeddings, data_with_ids)


def _canonical_decimal(value: object) -> str:
    decimal = Decimal(str(float(value)))
    if not decimal.is_finite():
        raise ValueError("score must be finite")
    text = format(decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _load_policy(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("execution_policy_version") != 1:
        raise ValueError("execution policy is invalid")
    return value


def produce_scores(
    *, vendor_root: Path, data_root: Path, policy_path: Path, output_path: Path
) -> None:
    policy = _load_policy(policy_path)
    sys.path.insert(0, str(vendor_root.parent))
    from trojnet.data import load_all_circuits
    import torch

    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(1)
    data_list = load_all_circuits(
        data_root, circuits=list(policy["circuit_families"])
    )
    expected = len(policy["circuit_families"]) * policy["variants_per_family"]
    if len(data_list) != expected:
        raise ValueError("captured circuit namespace does not match policy")
    scorers: dict[str, Callable[[Any, int], Any]] = {
        "raw_cc1": _score_raw_cc1,
        "scoap_isolation_forest": _score_scoap_if,
        "trojnet_community_graphsage": lambda data, seed: _score_trojnet(
            data, seed, 20
        ),
    }
    lines: list[bytes] = []
    for seed in policy["seeds"]:
        for data in data_list:
            for condition in policy["conditions"]:
                scores = scorers[condition](data, seed)
                row = {
                    "circuit_family": str(data.circuit_name),
                    "circuit_variant": str(data.circuit_variant),
                    "condition": condition,
                    "node_ids": list(data.node_names),
                    "schema_version": 1,
                    "scores": [_canonical_decimal(item) for item in scores.tolist()],
                    "seed": seed,
                }
                lines.append(
                    json.dumps(
                        row,
                        allow_nan=False,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n"
                )
    if len(lines) != 162:
        raise ValueError("score evidence must contain exactly 162 rows")
    output_path.write_bytes(b"".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    produce_scores(
        vendor_root=args.vendor_root,
        data_root=args.data_root,
        policy_path=args.policy,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
