"""
trojnet/data.py
---------------
Trust-Hub .bench netlist parser, SCOAP metric computation,
and heterogeneous graph construction for PyTorch Geometric.

Supported gate types (ISCAS .bench format):
  AND, NAND, OR, NOR, XOR, XNOR, NOT, BUF, DFF
  INPUT, OUTPUT declarations

Node feature vector (per gate):
  [gate_type_onehot(9), fan_in, fan_out, cc0, cc1, co,
   is_dff_boundary, is_primary_input, is_primary_output]
  → dim = 9 + 2 + 3 + 3 = 17

Usage:
  from trojnet.data import NetlistGraph
  g = NetlistGraph.from_bench("data/trust_hub/AES/aes_1.bench",
                               trojan_file="data/trust_hub/AES/aes_1_trojan_nodes.txt")
  data = g.to_pyg()   # torch_geometric.data.Data
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import torch
from torch_geometric.data import Data

# ── Gate type vocabulary ────────────────────────────────────────────────────

GATE_TYPES = ["INPUT", "OUTPUT", "DFF", "AND", "NAND", "OR", "NOR",
              "XOR", "XNOR", "NOT", "BUF"]
GATE2IDX = {g: i for i, g in enumerate(GATE_TYPES)}
GATE_DIM = len(GATE_TYPES)          # 11

# ── Regex patterns ──────────────────────────────────────────────────────────

_INPUT_RE  = re.compile(r"^INPUT\s*\((\s*\S+\s*)\)", re.IGNORECASE)
_OUTPUT_RE = re.compile(r"^OUTPUT\s*\((\s*\S+\s*)\)", re.IGNORECASE)
_GATE_RE   = re.compile(
    r"^(\S+)\s*=\s*(AND|NAND|OR|NOR|XOR|XNOR|NOT|BUF|DFF)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


@dataclass
class NetlistGraph:
    """In-memory representation of a parsed .bench netlist."""

    # Ordered node list (index = node id)
    nodes: list[str] = field(default_factory=list)
    node2idx: dict[str, int] = field(default_factory=dict)
    gate_type: dict[str, str] = field(default_factory=dict)  # node → gate type string

    # Directed edges: src → list of dst (signal flow direction)
    edges: dict[str, list[str]] = field(default_factory=dict)
    rev_edges: dict[str, list[str]] = field(default_factory=dict)  # dst → list of src

    primary_inputs: set[str] = field(default_factory=set)
    primary_outputs: set[str] = field(default_factory=set)
    dff_nodes: set[str] = field(default_factory=set)

    # Ground-truth Trojan labels (1 = Trojan gate, 0 = benign)
    trojan_labels: dict[str, int] = field(default_factory=dict)

    # SCOAP metrics (computed after parsing)
    cc0: dict[str, float] = field(default_factory=dict)
    cc1: dict[str, float] = field(default_factory=dict)
    co:  dict[str, float] = field(default_factory=dict)

    # ── Parsing ────────────────────────────────────────────────────────────

    @classmethod
    def from_bench(
        cls,
        bench_path: str | Path,
        trojan_file: Optional[str | Path] = None,
    ) -> "NetlistGraph":
        """Parse a .bench file and return a NetlistGraph.

        trojan_file: optional path to a text file listing Trojan gate names
                     (one per line).  If omitted, all labels are 0.
        """
        g = cls()
        bench_path = Path(bench_path)
        lines = bench_path.read_text(encoding="utf-8", errors="replace").splitlines()

        # ── First pass: collect all node names and gate types ──────────────
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            m = _INPUT_RE.match(line)
            if m:
                name = m.group(1).strip()
                g._add_node(name, "INPUT")
                g.primary_inputs.add(name)
                continue

            m = _OUTPUT_RE.match(line)
            if m:
                name = m.group(1).strip()
                # OUTPUT may alias an existing node; mark it if new
                if name not in g.node2idx:
                    g._add_node(name, "OUTPUT")
                g.primary_outputs.add(name)
                continue

            m = _GATE_RE.match(line)
            if m:
                out_name  = m.group(1).strip()
                gate_type = m.group(2).upper()
                inp_names = [x.strip() for x in m.group(3).split(",") if x.strip()]

                g._add_node(out_name, gate_type)
                if gate_type == "DFF":
                    g.dff_nodes.add(out_name)

                for inp in inp_names:
                    if inp not in g.node2idx:
                        g._add_node(inp, "INPUT")   # dangling → treat as input
                    g.edges.setdefault(inp, []).append(out_name)
                    g.rev_edges.setdefault(out_name, []).append(inp)
                continue

        # ── Load Trojan labels ─────────────────────────────────────────────
        g.trojan_labels = {n: 0 for n in g.nodes}
        if trojan_file is not None:
            trojan_path = Path(trojan_file)
            if trojan_path.exists():
                for name in trojan_path.read_text().splitlines():
                    name = name.strip()
                    if name and name in g.node2idx:
                        g.trojan_labels[name] = 1

        # ── Compute SCOAP metrics ──────────────────────────────────────────
        g._compute_scoap()

        return g

    def _add_node(self, name: str, gate_type: str) -> None:
        if name not in self.node2idx:
            self.node2idx[name] = len(self.nodes)
            self.nodes.append(name)
        # Only override gate type if not already set or current is INPUT placeholder
        existing = self.gate_type.get(name, "")
        if not existing or existing == "INPUT":
            self.gate_type[name] = gate_type.upper()

    # ── SCOAP ─────────────────────────────────────────────────────────────

    def _compute_scoap(self) -> None:
        """Compute CC0, CC1, CO for all nodes using iterative propagation.

        Simplified SCOAP (combinational):
          CC0(INPUT) = 1, CC1(INPUT) = 1, CO(OUTPUT) = 0
          For gates: standard SCOAP combinational rules.
          DFF: modelled as pseudo-inputs (CC0=CC1=1 from prior cycle).
        """
        INF = 1e9
        n = len(self.nodes)
        cc0 = np.full(n, INF)
        cc1 = np.full(n, INF)

        # Primary inputs and DFFs: base cost = 1
        for name in self.primary_inputs | self.dff_nodes:
            idx = self.node2idx[name]
            cc0[idx] = 1.0
            cc1[idx] = 1.0

        # Forward pass (topological order approximation via repeated relaxation)
        for _ in range(len(self.nodes)):
            changed = False
            for node in self.nodes:
                idx = self.node2idx[node]
                gtype = self.gate_type.get(node, "INPUT")
                preds = self.rev_edges.get(node, [])
                if not preds:
                    continue

                pidx = [self.node2idx[p] for p in preds]
                p_cc0 = cc0[pidx]
                p_cc1 = cc1[pidx]

                new_cc0, new_cc1 = _gate_scoap_cc(gtype, p_cc0, p_cc1)
                if new_cc0 < cc0[idx] or new_cc1 < cc1[idx]:
                    cc0[idx] = new_cc0
                    cc1[idx] = new_cc1
                    changed = True
            if not changed:
                break

        # Observability: backward pass
        co = np.full(n, INF)
        for name in self.primary_outputs:
            co[self.node2idx[name]] = 0.0

        for _ in range(len(self.nodes)):
            changed = False
            for node in reversed(self.nodes):
                idx = self.node2idx[node]
                succs = self.edges.get(node, [])
                if not succs:
                    continue
                min_co = INF
                for succ in succs:
                    sidx = self.node2idx[succ]
                    gtype = self.gate_type.get(succ, "BUF")
                    preds_of_succ = self.rev_edges.get(succ, [])
                    # CO(n) = CO(succ) + sum CC1 of sibling inputs + 1
                    siblings_cc1 = sum(
                        cc1[self.node2idx[p]]
                        for p in preds_of_succ if p != node
                    )
                    candidate = co[sidx] + siblings_cc1 + 1.0
                    min_co = min(min_co, candidate)
                if min_co < co[idx]:
                    co[idx] = min_co
                    changed = True
            if not changed:
                break

        # Normalise to [0, 1] (log scale to handle large circuits)
        def _norm(arr: np.ndarray) -> np.ndarray:
            arr = np.where(arr >= INF / 2, INF, arr)  # clip unreachable
            log_arr = np.log1p(np.clip(arr, 0, INF))
            mx = log_arr[log_arr < np.log1p(INF / 2)].max() if (log_arr < np.log1p(INF / 2)).any() else 1.0
            return np.where(arr >= INF / 2, 1.0, log_arr / (mx + 1e-9))

        cc0_norm = _norm(cc0)
        cc1_norm = _norm(cc1)
        co_norm  = _norm(co)

        for name in self.nodes:
            idx = self.node2idx[name]
            self.cc0[name] = float(cc0_norm[idx])
            self.cc1[name] = float(cc1_norm[idx])
            self.co[name]  = float(co_norm[idx])

    # ── PyG conversion ────────────────────────────────────────────────────

    def to_pyg(self) -> Data:
        """Convert to a torch_geometric.data.Data object.

        Node features (dim=16):
          gate_type_onehot(11) | fan_in(1) | fan_out(1) | cc0(1) | cc1(1) |
          co(1) | is_dff_boundary(1) | is_pi(1) | is_po(1)
        Edge index: directed (src → dst signal flow).
        y: Trojan label (0/1).
        """
        n = len(self.nodes)

        # Gate type one-hot
        type_oh = np.zeros((n, GATE_DIM), dtype=np.float32)
        for name in self.nodes:
            idx = self.node2idx[name]
            gtype = self.gate_type.get(name, "BUF")
            tidx = GATE2IDX.get(gtype, GATE2IDX["BUF"])
            type_oh[idx, tidx] = 1.0

        # Fan-in / fan-out (normalised by log)
        fan_in  = np.array([np.log1p(len(self.rev_edges.get(n, []))) for n in self.nodes], dtype=np.float32)
        fan_out = np.array([np.log1p(len(self.edges.get(n, [])))     for n in self.nodes], dtype=np.float32)
        fan_in  /= (fan_in.max()  + 1e-9)
        fan_out /= (fan_out.max() + 1e-9)

        # SCOAP
        cc0_arr = np.array([self.cc0.get(n, 0.5) for n in self.nodes], dtype=np.float32)
        cc1_arr = np.array([self.cc1.get(n, 0.5) for n in self.nodes], dtype=np.float32)
        co_arr  = np.array([self.co.get(n, 0.5)  for n in self.nodes], dtype=np.float32)

        # Boolean flags
        is_dff = np.array([1.0 if n in self.dff_nodes       else 0.0 for n in self.nodes], dtype=np.float32)
        is_pi  = np.array([1.0 if n in self.primary_inputs   else 0.0 for n in self.nodes], dtype=np.float32)
        is_po  = np.array([1.0 if n in self.primary_outputs  else 0.0 for n in self.nodes], dtype=np.float32)

        x = np.concatenate([
            type_oh,
            fan_in[:, None], fan_out[:, None],
            cc0_arr[:, None], cc1_arr[:, None], co_arr[:, None],
            is_dff[:, None], is_pi[:, None], is_po[:, None],
        ], axis=1)  # (N, 11+2+3+3) = (N, 19)

        # Edge index
        src_list, dst_list = [], []
        for src, dsts in self.edges.items():
            if src not in self.node2idx:
                continue
            s = self.node2idx[src]
            for dst in dsts:
                if dst in self.node2idx:
                    src_list.append(s)
                    dst_list.append(self.node2idx[dst])

        if src_list:
            edge_index = torch.tensor([src_list, dst_list], dtype=torch.long)
        else:
            edge_index = torch.zeros((2, 0), dtype=torch.long)

        # Labels
        y = torch.tensor(
            [self.trojan_labels.get(name, 0) for name in self.nodes],
            dtype=torch.long,
        )

        data = Data(
            x=torch.tensor(x, dtype=torch.float),
            edge_index=edge_index,
            y=y,
            num_nodes=n,
        )
        data.node_names = self.nodes
        data.dff_mask   = torch.tensor(is_dff, dtype=torch.bool)
        data.pi_mask    = torch.tensor(is_pi,  dtype=torch.bool)
        return data

    # ── Convenience ───────────────────────────────────────────────────────

    @property
    def num_nodes(self) -> int:
        return len(self.nodes)

    @property
    def num_trojan(self) -> int:
        return sum(self.trojan_labels.values())

    @property
    def trojan_density(self) -> float:
        return self.num_trojan / max(self.num_nodes, 1)


# ── SCOAP gate rules ────────────────────────────────────────────────────────

def _gate_scoap_cc(
    gtype: str,
    p_cc0: np.ndarray,
    p_cc1: np.ndarray,
) -> tuple[float, float]:
    """Return (CC0, CC1) for a gate given predecessor controllabilities."""
    INF = 1e9
    gtype = gtype.upper()

    if len(p_cc0) == 0:
        return 1.0, 1.0

    if gtype in ("AND",):
        cc1 = float(p_cc1.sum()) + 1
        cc0 = float(p_cc0.min()) + 1
    elif gtype in ("NAND",):
        cc0 = float(p_cc1.sum()) + 1
        cc1 = float(p_cc0.min()) + 1
    elif gtype in ("OR",):
        cc0 = float(p_cc0.sum()) + 1
        cc1 = float(p_cc1.min()) + 1
    elif gtype in ("NOR",):
        cc1 = float(p_cc0.sum()) + 1
        cc0 = float(p_cc1.min()) + 1
    elif gtype in ("XOR",):
        cc1 = float(min(p_cc0[0] + p_cc1[1] if len(p_cc0) > 1 else INF,
                        p_cc1[0] + p_cc0[1] if len(p_cc0) > 1 else INF)) + 1
        cc0 = float(min(p_cc0.sum(), p_cc1.sum())) + 1
    elif gtype in ("XNOR",):
        cc0 = float(min(p_cc0[0] + p_cc1[1] if len(p_cc0) > 1 else INF,
                        p_cc1[0] + p_cc0[1] if len(p_cc0) > 1 else INF)) + 1
        cc1 = float(min(p_cc0.sum(), p_cc1.sum())) + 1
    elif gtype in ("NOT",):
        cc0 = float(p_cc1[0]) + 1
        cc1 = float(p_cc0[0]) + 1
    elif gtype in ("BUF", "OUTPUT"):
        cc0 = float(p_cc0[0]) + 1
        cc1 = float(p_cc1[0]) + 1
    elif gtype in ("DFF",):
        cc0, cc1 = 1.0, 1.0   # modelled as pseudo-input
    else:
        cc0 = float(p_cc0.min()) + 1
        cc1 = float(p_cc1.min()) + 1

    return cc0, cc1


# ── Dataset loader ──────────────────────────────────────────────────────────

CIRCUIT_NAMES = ["AES", "RS232", "USB", "PIC", "VGA"]


def load_circuit(
    data_dir: str | Path,
    circuit: str,
    bench_glob: str = "*.bench",
) -> list[tuple[Data, str]]:
    """Load all .bench files for a given circuit.

    Supports these layouts:
      - data_dir/<CIRCUIT>/*.bench
      - data_dir/<CIRCUIT>.bench
      - data_dir/<CIRCUIT>_*/*.bench

    Returns list of (Data, filename) tuples.
    Each .bench may have an optional sidecar *_trojan_nodes.txt with Trojan node names.
    """
    data_dir = Path(data_dir)
    circuit_dir = data_dir / circuit
    seen: set[Path] = set()
    seen_stems: set[str] = set()
    bench_files: list[Path] = []

    def add_bench_files(paths: list[Path]) -> None:
        for path in paths:
            resolved = path.resolve()
            stem = path.stem.lower()
            if resolved not in seen and stem not in seen_stems:
                bench_files.append(path)
                seen.add(resolved)
                seen_stems.add(stem)

    if circuit_dir.exists():
        add_bench_files(sorted(circuit_dir.glob(bench_glob)))

    flat_candidates = [
        data_dir / f"{circuit}.bench",
        data_dir / f"{circuit.lower()}.bench",
        data_dir / f"{circuit.upper()}.bench",
    ]
    add_bench_files([candidate for candidate in flat_candidates if candidate.exists()])

    variant_dirs = sorted(
        d for d in data_dir.iterdir()
        if d.is_dir() and d.name.lower().startswith(f"{circuit.lower()}_")
    )
    for variant_dir in variant_dirs:
        add_bench_files(sorted(variant_dir.glob(bench_glob)))

    if not bench_files:
        raise FileNotFoundError(
            f"Circuit data not found for {circuit!r}: expected {circuit_dir}/*.bench, "
            f"{data_dir}/{circuit}.bench, or {data_dir}/{circuit}_*/*.bench"
        )

    result: list[Data] = []
    for bench_file in bench_files:
        # Look for sidecar trojan label file: same stem + _trojan_nodes.txt
        trojan_file = bench_file.with_name(bench_file.stem + "_trojan_nodes.txt")
        if not trojan_file.exists():
            trojan_file = None  # type: ignore[assignment]

        g    = NetlistGraph.from_bench(bench_file, trojan_file=trojan_file)
        data = g.to_pyg()
        data.circuit_name    = circuit
        # circuit_variant: file stem, e.g. "AES-T200" or "c880_ht1"
        # Falls back to circuit_name if the stem equals the family name
        stem = bench_file.stem
        data.circuit_variant = stem if stem.upper() != circuit.upper() else circuit
        data.bench_file      = str(bench_file)
        result.append(data)

    return result


def load_all_circuits(
    data_dir:      str | Path,
    circuits:      list[str] | None = None,
    bench_glob:    str = "*.bench",
) -> list[Data]:
    """Load multiple circuits and return a flat list of PyG Data objects.

    Each Data object has:
        .circuit_name  (str)  — circuit identifier, e.g. "AES"
        .bench_file    (str)  — absolute path to source .bench file
        .x             (N, 19) node features
        .edge_index    (2, E)
        .y             (N,)   Trojan labels (0 = clean, 1 = Trojan)
        .dff_mask      (N,)   bool
        .pi_mask       (N,)   bool

    Args:
        data_dir: root directory containing .bench files or circuit subdirectories.
        circuits: list of circuit names to load (e.g. ["AES", "RS232"]).
                  If None, attempts to load all circuits in CIRCUIT_NAMES.
        bench_glob: glob pattern for finding .bench files within a circuit directory.

    Returns:
        List of Data objects, one per .bench file found.
        Circuits with no .bench files are silently skipped with a warning.
    """
    import logging
    logger = logging.getLogger(__name__)

    if circuits is None:
        circuits = CIRCUIT_NAMES

    data_dir = Path(data_dir)
    all_data: list[Data] = []

    for circuit in circuits:
        try:
            items = load_circuit(data_dir, circuit, bench_glob=bench_glob)
            all_data.extend(items)
            logger.info(
                "Loaded %d graph(s) for circuit %s  [nodes: %s]",
                len(items),
                circuit,
                ", ".join(str(d.num_nodes) for d in items),
            )
        except FileNotFoundError as e:
            logger.warning("Skipping circuit %r: %s", circuit, e)

    return all_data
