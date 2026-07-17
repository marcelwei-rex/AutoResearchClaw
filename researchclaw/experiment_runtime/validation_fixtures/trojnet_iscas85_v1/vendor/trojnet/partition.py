"""
trojnet/partition.py
--------------------
Three netlist partitioning strategies used in the DFF ablation experiment:

  1. DFFPartition   — partition at D flip-flop boundaries (the TrojNet approach)
                      Groups combinational logic between DFF stages.
  2. RandomPartition — random k-way partition with same K as DFF (null baseline)
  3. LouvainPartition — community detection via greedy modularity (structural baseline)

Each strategy returns a list of subgraphs as PyG Data objects, one per module.
The parent Data object gets a `module_id` attribute (tensor, shape N) indicating
which module each node belongs to.

Usage:
    from trojnet.partition import DFFPartition
    modules, data_with_ids = DFFPartition().partition(data)
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np
import torch
from torch_geometric.data import Data
from torch_geometric.utils import subgraph


# ── Base class ───────────────────────────────────────────────────────────────

class BasePartition(ABC):
    """Abstract base for netlist partitioning strategies."""

    @abstractmethod
    def assign_modules(self, data: Data) -> torch.Tensor:
        """Return module_id tensor (shape N, dtype=long) for each node."""
        ...

    def partition(self, data: Data) -> tuple[list[Data], Data]:
        """Partition data into subgraph modules.

        Returns:
            modules: list of Data, one per module (nodes renumbered locally)
            data_with_ids: original Data with added .module_id attribute
        """
        module_ids = self.assign_modules(data)
        data_with_ids = data.clone()
        data_with_ids.module_id = module_ids

        n_modules = int(module_ids.max().item()) + 1
        modules: list[Data] = []

        for m in range(n_modules):
            node_mask = module_ids == m
            node_idx  = node_mask.nonzero(as_tuple=True)[0]
            if len(node_idx) == 0:
                continue

            sub_edge_index, _ = subgraph(
                node_idx, data.edge_index,
                relabel_nodes=True,
                num_nodes=data.num_nodes,
            )
            sub_data = Data(
                x=data.x[node_idx],
                edge_index=sub_edge_index,
                y=data.y[node_idx] if data.y is not None else None,
                num_nodes=len(node_idx),
            )
            sub_data.global_node_idx = node_idx   # map back to parent graph
            sub_data.module_id_val   = m
            modules.append(sub_data)

        return modules, data_with_ids

    @staticmethod
    def num_modules(data: Data) -> int:
        if hasattr(data, "module_id"):
            return int(data.module_id.max().item()) + 1
        return 1


# ── 1. DFF-boundary partition ────────────────────────────────────────────────

class DFFPartition(BasePartition):
    """Partition at D flip-flop boundaries (TrojNet structural prior).

    Algorithm:
      1. Remove all edges incident to DFF nodes (they represent register
         boundaries between combinational stages).
      2. Compute connected components in the resulting undirected graph.
      3. Each component becomes one module.
      4. DFF nodes themselves are assigned to the module of their combinational
         predecessors (fan-in cone). If a DFF has no predecessor in a module,
         it forms its own singleton module.

    This faithfully models the FMTD insight: hardware Trojan trigger logic
    is inserted in the combinational logic between DFF stages, not spanning
    across them. Partitioning at DFF boundaries isolates candidate Trojan
    regions to individual modules.
    """

    def assign_modules(self, data: Data) -> torch.Tensor:
        n = data.num_nodes
        dff_mask = data.dff_mask if hasattr(data, "dff_mask") else torch.zeros(n, dtype=torch.bool)

        # Build adjacency excluding DFF nodes
        edge_index = data.edge_index
        src, dst = edge_index[0], edge_index[1]

        # Mask out edges where either endpoint is a DFF
        valid_edge = ~(dff_mask[src] | dff_mask[dst])
        filt_src = src[valid_edge]
        filt_dst = dst[valid_edge]

        # Union-Find on non-DFF nodes
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for s, d in zip(filt_src.tolist(), filt_dst.tolist()):
            if not dff_mask[s] and not dff_mask[d]:
                union(s, d)

        # Assign sequential module IDs to non-DFF components
        root2mod: dict[int, int] = {}
        module_ids = torch.zeros(n, dtype=torch.long)
        mod_counter = 0

        for i in range(n):
            if dff_mask[i]:
                continue
            root = find(i)
            if root not in root2mod:
                root2mod[root] = mod_counter
                mod_counter += 1
            module_ids[i] = root2mod[root]

        # Assign DFF nodes to the module of their majority fan-in predecessor
        dff_indices = dff_mask.nonzero(as_tuple=True)[0].tolist()
        for dff_idx in dff_indices:
            preds = (dst == dff_idx).nonzero(as_tuple=True)[0]
            pred_src = src[preds]
            # Filter to non-DFF predecessors
            non_dff_preds = [p.item() for p in pred_src if not dff_mask[p]]
            if non_dff_preds:
                # Assign to the most common predecessor module
                counts: dict[int, int] = {}
                for p in non_dff_preds:
                    m = module_ids[p].item()
                    counts[m] = counts.get(m, 0) + 1
                best_mod = max(counts, key=counts.__getitem__)
                module_ids[dff_idx] = best_mod
            else:
                # No non-DFF predecessors → new singleton module
                module_ids[dff_idx] = mod_counter
                mod_counter += 1

        return module_ids


# ── 2. Random k-way partition ────────────────────────────────────────────────

class RandomPartition(BasePartition):
    """Random k-way partition.

    Assigns nodes to k = num_modules modules uniformly at random.
    Serves as the null baseline: if DFFPartition ≈ RandomPartition,
    the DFF structural prior adds no value.
    """

    def __init__(self, k: Optional[int] = None, seed: int = 42):
        """
        k: number of modules. If None, inferred from DFFPartition on the same data.
        seed: random seed for reproducibility.
        """
        self.k    = k
        self.seed = seed

    def assign_modules(self, data: Data) -> torch.Tensor:
        n = data.num_nodes
        k = self.k

        if k is None:
            # Infer k from DFF partition
            dff_ids = DFFPartition().assign_modules(data)
            k = int(dff_ids.max().item()) + 1

        rng = np.random.default_rng(self.seed)
        ids = rng.integers(0, k, size=n)
        return torch.tensor(ids, dtype=torch.long)


# ── 3. Louvain / greedy modularity partition ─────────────────────────────────

class LouvainPartition(BasePartition):
    """Community detection via greedy modularity maximisation (networkx).

    Uses the undirected version of the netlist graph.
    Serves as the structural baseline: purely topology-driven partitioning
    without the hardware-semantic DFF boundary prior.
    """

    def assign_modules(self, data: Data) -> torch.Tensor:
        try:
            import networkx as nx
            from networkx.algorithms.community import greedy_modularity_communities
        except ImportError as e:
            raise ImportError("networkx required for LouvainPartition: pip install networkx") from e

        n = data.num_nodes
        G = nx.Graph()
        G.add_nodes_from(range(n))

        src, dst = data.edge_index[0].tolist(), data.edge_index[1].tolist()
        G.add_edges_from(zip(src, dst))

        communities = list(greedy_modularity_communities(G))

        module_ids = torch.zeros(n, dtype=torch.long)
        for mod_idx, community in enumerate(communities):
            for node in community:
                module_ids[node] = mod_idx

        return module_ids


# ── Utility ──────────────────────────────────────────────────────────────────

class FastLouvainPartition(BasePartition):
    """Community detection via NetworkX Louvain heuristic for large graphs.

    The original ``LouvainPartition`` name is kept for backward-compatible
    manuscript evidence, where it actually uses greedy modularity. This class
    makes the scalable Louvain variant explicit for large external diagnostics.
    """

    def __init__(self, seed: int = 42, resolution: float = 1.0):
        self.seed = seed
        self.resolution = resolution

    def assign_modules(self, data: Data) -> torch.Tensor:
        try:
            import networkx as nx
            from networkx.algorithms.community import louvain_communities
        except ImportError as e:
            raise ImportError("networkx with louvain_communities required for FastLouvainPartition") from e

        n = data.num_nodes
        G = nx.Graph()
        G.add_nodes_from(range(n))

        src, dst = data.edge_index[0].tolist(), data.edge_index[1].tolist()
        G.add_edges_from(zip(src, dst))

        communities = list(
            louvain_communities(
                G,
                seed=self.seed,
                resolution=self.resolution,
            )
        )

        module_ids = torch.zeros(n, dtype=torch.long)
        for mod_idx, community in enumerate(communities):
            for node in community:
                module_ids[node] = mod_idx

        return module_ids


def partition_stats(data_with_ids: Data) -> dict:
    """Return per-module statistics for logging."""
    module_ids = data_with_ids.module_id
    n_modules  = int(module_ids.max().item()) + 1
    sizes = [(module_ids == m).sum().item() for m in range(n_modules)]
    y = data_with_ids.y

    trojan_per_mod = []
    for m in range(n_modules):
        mask = module_ids == m
        if y is not None:
            trojan_per_mod.append(y[mask].sum().item())
        else:
            trojan_per_mod.append(0)

    return {
        "num_modules":     n_modules,
        "avg_module_size": float(np.mean(sizes)),
        "max_module_size": int(np.max(sizes)),
        "min_module_size": int(np.min(sizes)),
        "trojan_modules":  sum(1 for t in trojan_per_mod if t > 0),
        "total_nodes":     data_with_ids.num_nodes,
    }
