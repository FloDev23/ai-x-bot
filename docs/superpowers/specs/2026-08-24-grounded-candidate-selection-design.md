# Grounded Candidate Selection Design

## Goal

Increase the percentage of interesting, publishable FlexDropin drafts without
lowering the editorial threshold, weakening factual checks, or introducing any
automatic X action.

## Constraints

- A slot continues to use exactly one source selected by the content planner.
- Every candidate must pass the existing trust, expiry, claim, numeric, length,
  duplicate, and editorial checks.
- The editorial threshold remains `75`.
- At most three candidates may be generated for one slot evaluation.
- Only one winning draft may be persisted or sent to Telegram.
- Approval remains Telegram-only and `DRY_RUN=true` remains the production-safe
  default.
- Failed candidates never reach X, Telegram, or the draft table.

## Approaches Considered

1. **Bounded candidate tournament — selected.** Generate up to three grounded
   angles, run every candidate through the existing gates, and retain the
   highest-scoring eligible candidate. This costs more model calls but directly
   addresses the observed variance while preserving every safety boundary.
2. **Score-guided rewrite.** Rewrite one low-scoring draft using its weak axes.
   This is cheaper, but tends to optimize toward the rubric and can make copy
   formulaic.
3. **Keep one-shot generation.** This has no implementation cost, but live
   evidence showed materially different outcomes from the same verified source:
   a valid `79`, a rejected numeric claim, and a valid-but-low `70`.

## Architecture

`DraftPipeline` owns candidate orchestration because it already owns the ordered
safety gates and persistence boundary. `AIGenerator` remains responsible only
for producing one candidate at a time. Each call receives one of three bounded
angle hints:

1. a sourced contrast;
2. an overlooked sourced trend or metric;
3. an operator-relevant question supported by the source.

The hints may change framing, never the factual universe. The same verified
source bundle is reused for all attempts.

The pipeline takes one snapshot of recent post text before candidate evaluation.
That exact snapshot is supplied to every scoring call and later reused by the
deterministic duplicate gate, so candidates are comparable and the outcome does
not drift within one run.

## Candidate Flow

For a planned slot:

1. Resolve and validate the intended slot, source IDs, and verified source once.
2. Load recent posts once, excluding the edited draft when applicable.
3. Generate up to three candidates, one per angle hint.
4. For each candidate, apply in order:
   - non-empty and 280-character validation, including the existing source-bound
     rewrite when necessary;
   - `FactGuard` claim and numeric validation against the same source;
   - source-aware editorial scoring with deterministic recent-post novelty;
   - semantic duplicate rejection against the same recent snapshot.
5. Keep only candidates that pass every non-threshold safety gate and have a
   valid score object.
6. Select the highest `total`; ties are resolved by the earliest attempt.
7. Persist exactly one draft only when the winning total is at least `75`.
8. Media matching and the Telegram card continue only after that single atomic
   persistence succeeds.

No candidate text is written to evaluation logs. The final audit contains only
bounded attempt number, outcome/reason codes, source IDs, and scores.

## Failure Handling

- A candidate-specific factual, length, duplicate, or low-score rejection moves
  to the next candidate.
- A source trust/expiry failure aborts the whole slot because every candidate
  would share the invalid source.
- A generator, claim-analysis, or scorer service failure aborts the run rather
  than multiplying calls during an outage.
- If no candidate clears all gates, record one final rejection for the slot with
  sanitized attempt summaries and persist no draft.
- Database persistence keeps the existing create-or-get and slot uniqueness
  behavior, so concurrent workers still produce at most one live draft.

## Telegram and X Boundaries

Telegram receives only the winning persisted draft. Rejected alternatives are
not shown, stored as drafts, or offered for approval. This design does not add
any X method, scheduled engagement, follow, like, reply, direct message, or
automatic publication. The Publisher remains the sole X write boundary and
still requires an approved draft.

## Testing

Tests use deterministic fakes plus the existing real SQLite integration suite.
They must prove:

- three distinct angle hints share the exact same source;
- a first factual failure cannot block a later valid winner;
- the highest eligible score wins, with stable earliest-attempt tie breaking;
- a score below `75` is never persisted even if it is the best attempt;
- duplicate candidates and malformed model responses cannot win;
- recent posts are read once and reused for scoring and duplicate checks;
- source/service failures stop safely with bounded call counts;
- concurrent workers still persist one draft and send at most one Telegram card;
- no new X capability or call path exists;
- the full regression suite, compilation, dependency, and diff checks remain
  clean.

## Acceptance Criteria

The implementation is accepted when the tests above pass, an independent review
reports no Critical or Important finding, production restarts with
`DRY_RUN=true` and `APPROVAL_REQUIRED=true`, and one live dry-run slot either:

- creates a single Telegram draft scoring at least `75`; or
- records a sanitized bounded rejection without any X write.
