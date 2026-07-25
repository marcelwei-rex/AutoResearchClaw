# Stage 17 Citation Reliability — Frozen Design v1.1

Status: **FROZEN DESIGN / RETROSPECTIVE AMENDMENT / PARTIALLY IMPLEMENTED**

Frozen on: 2026-07-24

Amended on: 2026-07-25

Purpose: provide the implementation and review authority for the narrow
Stage 16/17 citation-reliability changes. This amendment records the fresh-run
evidence that triggered the previously deferred immutable-anchor fallback,
reconciles the frozen design with production commit `3c7c2f8`, and freezes the
next C1/C2 boundaries. It does not claim that C1/C2 have been implemented or
accepted by a fresh Stage 1–25 run.

## 1. Decision

The agreed architecture is:

1. Keep exact citation, fact, provenance, replay, and publication gates.
2. Keep the Stage 16 citation plan extension from Introduction/Related Work to a
   section-scoped and claim-type-scoped authority model that can also cover
   Method and Experiments.
3. Make the evidence anchor code-owned and immutable. A provider must never be
   asked to reproduce, select, rewrite, acknowledge, or authorize exact anchor
   bytes.
4. Remove the empty-JSON provider acknowledgement introduced by `3c7c2f8`.
   It produces no manuscript or authority bytes and must not remain a transport
   or schema failure dependency.
5. Separate the internal `EvidenceAnchor` from the rendered
   `ManuscriptClaim`. Scope-specific rendering must be explicit and
   fail-closed:
   - `pipeline_validation` may render a
     `ManuscriptClaim(provenance="verbatim")`;
   - `research_release` must reject a verbatim claim before Stage 17 renders
     its first citation-bearing byte unless a future, separately reviewed
     validated-paraphrase policy is implemented.
6. Ordinary HITL approval, guidance, or a researcher signature is not a
   release-authority override. A future human risk-acceptance workflow requires
   a new policy and a separate permission-checked artifact.
7. Do not change the writer model before the architecture and plan coverage
   change has been evaluated.

Historical failed F0 runs must not be modified, resumed, or used as mutable
fixtures. Their artifacts remain read-only diagnostic evidence. No historical
run can validate C1/C2.

## 2. Evidence and corrected history

The following facts are frozen:

- A pre-single-anchor run (`f0-trojnet-pv-20260723-084731`) contained all 25
  planned claims, but only one plan `claim_text` survived byte-exactly in the
  draft. Exact copying has therefore failed historically.
- Later failures included anchor ordering, foreign-claim exposure, and an
  unauthorized citation candidate in a Method-like heading.
- At the v1.0 freeze point, the plan parser accepted only `Introduction` and
  `Related Work`, and only the `background` claim type.
- Subsequent implementation extended the plan with section- and
  claim-type-scoped authority and retained exact replay.
- The recent failures cited above were correct fail-closed rejections. There is
  no current evidence that the citation integrity gate itself is too strict.
- Fresh no-resume run `f0-trojnet-pv-20260725-073024` failed after the
  single-anchor change because the provider still rewrote or extended one
  exact anchor under bounded repair. This satisfies the evidence trigger in
  v1.0 Section 10 for a code-owned immutable anchor.
- Commit `3c7c2f8` made the anchor code-owned, but retained one initial and at
  most one repair call that can only return
  `{"after":"","before":""}`. Those calls produce no manuscript content or
  authority and are a known C1 defect.
- Stage 16 currently assigns a selected evidence-card
  `excerpt_text` directly to `claim_text`. Code-owned rendering therefore
  places verbatim source text in the paper. This is acceptable only as an
  explicit `pipeline_validation` representation, not as an implicit
  `research_release` representation.

The v1.0 conclusion that the historical failure did not yet require the
immutable scaffold is superseded by the later fresh-run evidence. The scaffold
trigger is satisfied. This amendment is the required separate design review;
it approves code-owned anchors but not the empty provider acknowledgement or a
research-release verbatim path.

## 3. Scope

### 3.1 In scope

- C0: this docs-only retrospective amendment.
- C1: remove the empty-JSON provider acknowledgement, its repair path, its
  outbound budget, and provider-response diagnostics without changing
  assembled anchor bytes.
- C2: introduce typed `EvidenceAnchor` and `ManuscriptClaim` projections,
  explicit provenance, strict `claim_scope` behavior, replay, and closure.
- Regression tests that freeze section/claim-type authority,
  zero-authority generation, exact marker insertion, captured-authority replay,
  and generic-v1 behavior.
- One fresh `pipeline_validation` Stage 1–25 run after C2 and independent
  review.

### 3.2 Out of scope

- Relaxing any existing citation or canonical-evidence guard.
- Changing Stage 20 verdict or publication semantics.
- Solving missing figures, thin Results, insufficient experiments, or narrow
  evaluation coverage.
- Adding new experiments or allowing the writer to invent missing evidence.
- Changing the writer model without a controlled comparison.
- Modifying a prior or in-flight F0.
- Refactoring the large paper-writing or citation-plan modules.
- Implementing the validated-paraphrase compiler in C0/C1/C2.
- Adding transition prose merely to preserve a provider call.
- Adding a compatibility HITL or accepted-risk override for
  `research_release`.

## 4. Frozen generation protocol

The code-owned anchor architecture is treated as an invariant:

1. `EvidenceAnchor` bytes come only from the captured and strictly replayed
   citation plan and evidence-card graph.
2. No provider call receives the anchor bytes or selects, confirms, rewrites,
   or authorizes them.
3. Citation anchor assembly and order are deterministic code
   responsibilities.
4. Deterministic marker insertion occurs only after complete anchor validation.
5. Multi-anchor compatibility paths remain rejected.
6. Captured authority, exact-byte validation, replay, parent replacement, and
   final citation closure remain fail-closed.
7. Deleting the provider acknowledgement must also delete its semantic repair,
   fallback, outbound-call budget, and response diagnostics. No dead
   compatibility switch may reactivate it.

For a heading group with no citation authority:

1. The initial response must contain no citation candidate.
2. A detected candidate permits at most one closed regeneration.
3. A second violation fails Stage 17.
4. The system must not silently delete the citation, add it to the plan, or
   expand authority after generation.

### 4.1 Typed citation representation

`EvidenceAnchor` is an internal domain-v2 authority projection. Its v1 identity
payload has exactly:

```text
{
  schema_version,
  policy_version,
  citation_plan_path,
  citation_plan_sha256,
  claim_id,
  section_path,
  claim_type,
  cite_key,
  evidence_card_path,
  evidence_card_sha256,
  evidence_excerpt_id,
  evidence_excerpt_sha256,
  support_status,
  claim_scope,
  canonical_experiment_evidence_path,
  canonical_experiment_evidence_sha256,
  config_source_path,
  config_source_sha256
}
```

`schema_version` is the true integer `1`;
`policy_version="stage17-evidence-anchor-v1"`. Paths are normalized safe
run-relative paths. `section_path` is the exact parsed plan array. The excerpt
SHA-256 is computed from the exact UTF-8 bytes of the captured excerpt string;
the bytes remain captured separately for replay.

It is not rendered merely because it exists.

`ManuscriptClaim` is the only citation-bearing text eligible for rendering. Its
v1 identity payload has exactly:

```text
{
  schema_version,
  policy_version,
  evidence_anchor_id,
  claim_scope,
  provenance,
  claim_text_sha256,
  validation_policy_version,
  validation_report_sha256
}
```

`schema_version` is the true integer `1`;
`policy_version="stage17-manuscript-claim-v1"`. `provenance` is exactly
`verbatim` or `paraphrased`. The claim SHA-256 is computed from the exact UTF-8
rendered bytes. A deterministic validation report is mandatory for both
provenance values, so no identity field is optional.

Both IDs are the lowercase SHA-256 of canonical identity JSON bytes:
`json.dumps(payload, sort_keys=True, separators=(",", ":"),
ensure_ascii=False, allow_nan=False).encode("utf-8")`. The ID itself and every
stored object/manifest hash are excluded from their own payload. Parsers reject
duplicate keys, unknown/missing fields, non-canonical JSON bytes, bool-as-int,
non-finite numbers, unsafe paths, and non-NFC paths. Stored or caller-supplied
IDs, hashes, provenance, scope, status, and generation fields are comparison
inputs only; they never construct expected identity.

The citation plan, evidence card/excerpt, active-config snapshot, canonical
experiment evidence, anchor, and manuscript claim must belong to one captured
generation. Their path/hash/config/generation bindings must be exactly equal.
Missing or inapplicable domain-v2 bindings are not omitted or represented by a
stored fallback; they fail typed projection. Snapshot A supplies all semantic
replay inputs. Snapshot B and fresh loads are comparison-only and cannot
replace A. Any mixed generation fails before rendering.

The producer, disk replay, citation closure, citation-bearing downstream
consumers, publication closure, and independent reconstruction must derive or
compare the same typed projection. C2 must not change unrelated Stage 18–25
semantics.

### 4.2 Claim-scope matrix

| Claim scope | Allowed rendered claim | Failure behavior |
|---|---|---|
| `pipeline_validation` | `verbatim` or a future validated `paraphrased` claim | Missing/invalid typed binding fails Stage 16/17 |
| `exploratory` | Same as `pipeline_validation`, but never release authority | Missing/invalid typed binding fails Stage 16/17 |
| `research_release` | Only a separately specified and validated `paraphrased` claim | Verbatim, missing validation, or fallback fails before Stage 17 renders its first citation-bearing byte; no Stage 17 success, downstream success authority, or publication candidate may be created |

Before typed projection, scope is independently replayed from the captured
active-config snapshot and compared for exact equality with both the canonical
experiment evidence config binding and the independently rebuilt final citation
plan. A missing or unequal value fails before any typed identity or
citation-bearing byte is constructed. API parameters, runtime callers, stored
plan/claim fields, and compatibility defaults cannot select, downgrade, or
upgrade scope. No LLM response, ordinary HITL action, reviewer note, or unsigned
file can permit a forbidden provenance. Consistent with the canonical release
design, a future human risk-acceptance workflow requires a new policy and
separate permission-checked artifact.

### 4.3 Future validated paraphrase boundary

Validated paraphrase is C3 work and requires a separate design review before
implementation. At minimum it must:

- consume only captured `EvidenceAnchor` bytes;
- prohibit new or changed numeric values, units, entities, methods, datasets,
  results, comparisons, and citation candidates;
- bind the exact paraphrase bytes to the anchor and validation input;
- use deterministic, versioned parsers for every machine-enforced predicate;
- reject unknown or ambiguous cases;
- never use an LLM similarity score or producer self-report as authority;
- replay independently at plan construction, Stage 17 capture/publication, and
  release reconstruction;
- expose all paraphrased claims for researcher reading without allowing that
  reading step to override failed machine authority.

Deterministic checks can prove selected negative properties, not complete
semantic equivalence. Human scientific review remains mandatory, but it is not
a substitute for the machine release gate.

## 5. Versioned section and claim-type authority

Existing plan versions retain their existing semantics and must not be
reinterpreted by a later parser.

The current domain plan v3 uses this frozen compatibility matrix:

| Section | Allowed claim types | Authority boundary |
|---|---|---|
| Introduction | `background`, `problem_context`, `prior_work` | Literature-supported background, motivation, and prior findings |
| Related Work | `prior_work`, `method_comparison` | Prior methods and literature-grounded comparisons |
| Method | `method_origin`, `algorithm_definition` | External method or algorithm that the canonical experiment actually uses |
| Experiments | `dataset_origin`, `benchmark_definition`, `evaluation_protocol` | Bound dataset, benchmark, or externally defined evaluation protocol |

The enum names, semantic partitions, and default-deny behavior are frozen.
Adding, removing, renaming, or repartitioning a claim type or section requires
a new schema version and a separate independent review.

Results, Discussion, Limitations, and Conclusion do not create new citation
authority in this version.

## 6. Deterministic “actually used” eligibility

“Actually used” must not be decided by the LLM, topic text, fuzzy matching, or
free-form prose.

An eligible Method or Experiments citation claim must bind all of:

- one retained literature source and evidence excerpt;
- one exact planned claim anchor;
- one allowed section and claim type;
- one canonical usage token;
- the canonical evidence/config hash from which that token was derived;
- the version of the token-derivation policy.

Canonical usage tokens may come only from a closed projection of explicit
authority fields, such as:

- condition identities;
- evaluator identity and evaluator schema;
- dataset bound labels;
- benchmark bound labels;
- explicitly bound evaluation-protocol identifiers.

Token derivation must use a versioned deterministic mapping. It must not infer
an algorithm from a generic substring. Splitting
`trojnet_community_graphsage` can authorize an exact registered GraphSAGE token
only if the mapping says so; it cannot infer Louvain from the word
`community`. If Louvain is not explicitly represented by canonical evidence or
a trusted evaluator-package registry, a Louvain citation is ineligible in this
version and plan construction must fail closed.

Likewise, an Experiments citation may describe an explicitly bound dataset,
benchmark, or evaluation protocol, but it cannot authorize:

- an observed metric;
- a result value;
- an experiment count;
- a runtime claim;
- a dataset or benchmark absent from canonical evidence;
- a method that was not canonically bound.

Literature citation markers never authorize numeric or source-use claims that
belong to the canonical fact sheet.

## 7. Required rejection behavior

The implementation must reject at least:

- an unknown cite key;
- a foreign claim anchor;
- a missing, duplicate, reordered, or rewritten anchor;
- a marker moved away from its bound sentence;
- a section/claim-type mismatch;
- a Method claim without an eligible canonical usage token;
- an Experiments claim for an unbound dataset, benchmark, or protocol;
- any citation used to authorize current-run observations or metrics;
- new authority created for Results, Discussion, Limitations, or Conclusion;
- a mismatch between captured authority and current artifacts;
- repair-budget or outbound-call-budget exhaustion on a path that still owns a
  provider call, including zero-authority and generic writer paths. The C1
  citation-anchor path has no call, repair, fallback, or outbound budget.

Unknown classifications default to rejection.

## 8. Red-test acceptance set

Implementation starts with failing tests for:

1. Valid Introduction background citation.
2. Valid Related Work prior-method citation.
3. Valid Method citation for an explicitly registered, actually used method.
4. Valid Experiments citation for a bound dataset or benchmark.
5. Valid Experiments citation for an explicitly bound evaluation protocol.
6. Rejection of a Method citation for an unused or unregistered method.
7. Rejection of an inferred method that is not explicitly represented, such as
   inferring Louvain from a generic community-method identifier.
8. Rejection of a Method citation that attempts to authorize a current-run
   result.
9. Rejection of an Experiments citation that attempts to authorize an observed
   metric or count.
10. Rejection of new authority in Results, Discussion, Limitations, or
    Conclusion.
11. Rejection of claim/card/excerpt/source mismatch.
12. Rejection of reordered, duplicated, missing, foreign, or rewritten anchors.
13. Zero citation-anchor provider calls after C1.
14. No empty-JSON parser, repair, fallback, budget, or response diagnostic can
    reactivate an anchor call.
15. Exact code-owned anchor bytes, order, and deterministic marker insertion
    remain unchanged after C1.
16. Zero-authority initial violation followed by one valid regeneration.
17. Zero-authority second violation causing Stage 17 failure.
18. Captured-authority mutation and parent-replacement replay rejection.
19. Existing generic and prior-version plan behavior unchanged.
20. Canonical fact-sheet numeric and provenance authority unchanged.
21. Typed projection rejects duplicate, missing, unknown, mixed-generation, or
    self-reported identity/provenance fields.
22. `pipeline_validation` explicitly permits a valid verbatim claim.
23. `research_release` rejects verbatim even when an ordinary HITL action,
    reviewer note, or synchronized stored status claims approval.
24. No failed typed projection leaves Stage 17 or downstream success authority.

Tests must include positive and negative branches. A test that only proves the
new sections parse is insufficient.

## 9. Implementation sequence

The minimum sequence is:

1. Commit C0 as a docs-only retrospective amendment after independent review.
2. Add C1 red tests for zero anchor provider calls and unchanged assembled
   bytes.
3. Implement C1 and independently review the narrow deletion.
4. Add C2 red tests for typed identity, provenance, scope, replay, cleanup, and
   generic-path isolation.
5. Implement C2 without adding provider prose or paraphrase.
6. Run targeted deterministic suites and fake-transport production-chain
   tests.
7. Perform an independent read-only review focused on authority expansion,
   scope bypass, captured replay, parent replacement, and generic regression.
8. Commit C1 and C2 separately only after their reviews accept the scope.
9. Start one new clean `pipeline_validation` Stage 1–25 F0. Do not resume or
   mutate prior runs. Acceptance requires all stages done with no degradation,
   independent release reconstruction, Stage 24/25 semantic replay, and
   `pipeline_validation` `release_check` at its documented expected result.
10. If Stage 17 passes, continue through Stage 20. A legitimate Stage 20
    rejection remains a paper-quality finding, not a citation-gate failure.
11. Design and implement C3 before any `research_release` acceptance run.

No step may convert an existing failed or diagnostic run into release evidence.

## 10. Scaffold trigger disposition

The v1.0 scaffold trigger is **satisfied** by the fresh bounded single-anchor
failure recorded in Section 2. Commit `3c7c2f8` implemented code-owned anchors.
This v1.1 amendment is the required retrospective separate design review.

The accepted part of that implementation is deterministic code-owned anchor
assembly. The empty provider acknowledgement is rejected and scheduled for C1.
Transition prose is not authorized by this amendment. If later evidence shows
that the typed manuscript needs bounded transition prose, it requires its own
design, validation, degradation budget, and independent review; it must not
restore a provider dependency to anchor construction.

## 11. Model decision

Do not switch models as part of this change.

After the current domain-plan implementation completes deterministic validation
and one clean provider probe for the remaining provider-owned prose paths, a
controlled A/B may compare Flash and Pro using the same captured inputs,
prompts, model parameters, repair budget, and validator. The selection criteria
are contract-success rate, manuscript quality, latency, and cost. Provider
choice never changes authority semantics.

## 12. Non-blocking hardening

A hard UTF-8 response-byte limit may later be added in addition to the current
provider token limit and recorded `response_utf8_bytes`. It is a useful defense
in depth, but it is not a prerequisite for the narrow plan-coverage fix and
must not silently truncate a response. Exceeding it must be an explicit
fail-closed contract error.

## 13. How this document is used

For implementation:

- cite this document as the frozen decision source;
- implement one bounded batch at a time;
- list any proposed deviation before editing code;
- never treat this document as evidence that tests or F0 passed.
- preserve C0/C1/C2/C3 as separate review and commit boundaries.

For review:

- compare code and tests against Sections 4–9;
- classify any broadened authority as a blocking issue;
- keep Stage 17 citation reliability separate from Stage 20 paper-quality work.

For final acceptance:

- require targeted deterministic tests, independent review, and a fresh
  Stage 1–25 run at the intended claim scope;
- for `pipeline_validation`, require done/no-degraded execution, independent
  release reconstruction, Stage 24/25 semantic replay, and the documented
  `pipeline_validation` `release_check` result;
- retain the failed-run evidence rather than rewriting or resuming it;
- record Stage 20 quality findings independently from Stage 17 contract
  reliability.

This v1.1 design is frozen when committed as a docs-only change. C1, C2, and C3
remain separately scoped production changes with independent review.
