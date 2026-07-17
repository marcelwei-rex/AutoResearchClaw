# TrojNet ISCAS-85 validation fixture v1

Status: pre-canonical review fixture.

This package freezes the code and controlled synthetic ISCAS-85 data used by
the current TrojNet manuscript. It tests whether a domain-owned evaluator can
produce baseline, proposed-method, multi-seed, and complete localization
metrics without reading the live paper workspace.

It is not a Stage 9-14 evaluator authority and must not be used as
`research_release` evidence. Canonical integration requires a separate reviewed
change to the metric-authority selector, experiment contract, Stage 10
ownership seal, Stage 12 result schema, and independent replay.

Profiles:

- `smoke`: c432 variants, seed 0, 20 epochs, 9 observations.
- `validation`: six circuit families, seeds 0/1/2, 20 epochs, 162 observations.

Conditions:

- `raw_cc1`
- `scoap_isolation_forest`
- `trojnet_community_graphsage`

Primary metric: AUPRC. Secondary metrics are AUROC, tie-aware top-k precision,
accuracy, precision, recall, F1, and false-positive rate. Runtime is diagnostic
and excluded from `semantic_sha256`.

Install the exact optional dependency projection, then run the dependency-strict
acceptance command. Missing or mismatched dependencies fail the command; they
are not converted into skipped tests.

```bash
python -m pip install -e '.[trojnet-validation]'
python scripts/validate_trojnet_fixture.py \
  --output /tmp/trojnet-fixture-validation.json
```

The script is the only supported execution entry. It rejects any
`__pycache__` below the fixture before importing the runner, then starts a
child interpreter with a temporary external `PYTHONPYCACHEPREFIX`. Do not use
`python -m ...runner` as an acceptance command. Run fixture unit tests with an
external cache prefix as well:

```bash
PYTHONPYCACHEPREFIX=/tmp/researchclaw-test-pycache \
  python -m pytest -q tests/test_trojnet_validation_fixture.py
```

The command executes all 18 variants, three seeds, and three conditions (162
observations). The runner verifies the exact frozen source/data bundle and the
separate evaluator policy identity before execution, then replays all per-seed
and aggregate summaries before publishing a result. Runtime Python and package
versions are part of the semantic result.
