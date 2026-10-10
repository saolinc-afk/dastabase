# Human Review / Ground Truth layer

The import represented here is append-only and separate from canonical company and
Discovery result databases. Original Discovery evidence remains immutable.

## Assertion contract

Each assertion records canonical company, claim type, exact asserted value,
normalized values, status, source and actor, methodology/version, authority,
confidence, evidence references, review/freshness timestamps, and explicit
supersedes/conflicts metadata. Re-importing the same source row is idempotent.
A changed record with the same deterministic ID is rejected rather than updated.

Today's source is `xlsx:ambiguity_human_review_100.xlsx:sha256:5450081cd9db4c7cf6c6c1373ba9cfb25060032077c1cf619f672e25ec441d4f`. `HUMAN_REVIEW` has explicit
`HUMAN_VALIDATED` authority rank 400. That rank represents precedence policy input,
not permission to erase older assertions. Conflicting assertions remain separate
rows; `SUPERSEDES` and `CONFLICTS_WITH` are explicit relations.

Statuses used for this review:

- `CONFIRMED_OFFICIAL_WEBSITE`
- `LIKELY_NO_OFFICIAL_WEBSITE`
- `UNRESOLVED_INSUFFICIENT_EVIDENCE`
- `COMPLEX_RELATIONSHIP`
- `WRONG_ENTITY_OR_CROSS_COUNTRY`

`LIKELY_NO_OFFICIAL_WEBSITE` is intentionally weaker than a proven nonexistence
claim. Complex and wrong-entity values preserve what the reviewer entered without
silently treating that URL as an accepted company website.

## Future Knowledge Repository / Merlin use

1. Query all assertions for the company and claim before enrichment.
2. Reuse only assertions meeting the caller's authority, confidence, and freshness
   requirements, while surfacing unresolved conflicts.
3. Invoke paid AI only when knowledge is missing, stale, conflicting, or below the
   required confidence after deterministic resolution.
4. Store validated AI results as `AI_ENRICHMENT` assertions with model/provider,
   prompt/method version, evidence references, token counts, cost metadata, and
   validity/freshness timestamps.
5. Never update or delete the human row. A later result either coexists, explicitly
   conflicts with it, or explicitly supersedes it after review.
6. Knowledge Repository integration should be a later read-policy adapter; this
   benchmark does not alter its current behavior.
