# Resolver / Search vNext requirements from the 27-case benchmark

This is a design specification only. It does not change Discovery execution or
acceptance behavior.

## P0 — deterministic fixes

1. **Identifier-bound attribution and hard entity conflicts.** Require exact tax,
   registration, or equivalent address/legal evidence before transferring a domain
   from a same/similar-name entity. This covers GRADBENIŠTVO LORENČIČ and C.I.A.K.
2. **First-class relationship states.** Represent `PARENT`, `GROUP`, `PRINCIPAL`,
   `OWNER`, `SUBSIDIARY`, `BRAND`, `SUCCESSOR`, `FOREIGN_OFFICE`, and `UNRELATED`.
   A related domain must never silently become `OFFICIAL_WEBSITE`.
3. **Multi-domain resolution.** Retain multiple validated official/alternate domains,
   select a canonical domain by explicit policy, and preserve every relationship.
   BAIMS is the concrete control case.
4. **Marketplace/directory and same-domain handling.** Marketplace/profile operators
   remain evidence publishers, not official websites. Group host/path variants by
   registrable domain before ranking. SUPER STRELA is the control case.
5. **No-content and fetch-state fail-closed rules.** Empty, parked, blocked, or
   nonfunctional pages cannot prove ownership. Return UNKNOWN unless an independent,
   exact structured identity bridge exists; do not ask AI to hallucinate page content.
6. **Stored-result extraction replay.** Before another query, re-run URL/email-domain
   extraction over persisted result titles/snippets for absent domains, recording
   extraction method and evidence IDs. PRO ACTIV and ENERKO BIRO are controls.
7. **Country gate.** A foreign ccTLD or foreign legal entity requires an explicit
   relationship to the target; otherwise retain UNKNOWN.

## P1 — search and brand recovery

1. Build queries from exact legal identity plus address, municipality, tax and
   registration identifiers, then add bounded trading-name/activity variants.
2. Extract trading/venue/product brands from structured sources and persisted
   snippets; store the legal-entity-to-brand bridge as evidence before querying it.
3. Add bounded tokenization variants: hyphens, abbreviations, removal of generic
   suffixes such as BIRO, and activity words such as gradnje. Never enumerate broad
   fuzzy domains without identity constraints.
4. Add explicit predecessor/successor and rebrand queries. Results must be stored as
   relationship candidates, not automatically accepted websites.
5. Audit each query for `retrieved result -> extracted candidate` conversion so
   QUERY_MISS and RESULT_EXTRACTION_MISS are measurable rather than inferred.
6. Investigate registration-linked structured sources such as Sloexport as identity
   bridges. A structured source URL still cannot alone prove official ownership.

## P2 — selective AI fallback

AI is permitted only after P0 replay and P1 bounded recovery produce no deterministic
decision. The 14 benchmark cases marked `YES_AFTER_P0_P1` are the initial eligible
classes: difficult brand/legal-name mapping, rebrand/successor interpretation,
parent/principal/owner attribution, international group attribution, multi-domain
relationship interpretation, and the explicitly AI-dependent Hiša Cvet case.

AI receives a compact, immutable package:

- canonical company ID, legal name, tax/registration, address and country;
- every candidate domain/URL with candidate type, query, rank and publisher;
- stored result title/snippet and fetched-page title/identity excerpts;
- evidence and observation IDs, extraction methods, timestamps and attempt/run lineage;
- exact identifier/address/name matches and conflicts;
- known directory/marketplace/foreign-domain blocks;
- existing versioned assertions and conflicts.

AI must return schema-valid JSON:

```json
{
  "decision": "OFFICIAL_WEBSITE|BRAND_OR_GROUP_WEBSITE|NO_OFFICIAL_WEBSITE|INSUFFICIENT_EVIDENCE",
  "candidate_domain": null,
  "relationship_type": null,
  "confidence": "LOW|MEDIUM|HIGH",
  "supporting_evidence_ids": [],
  "conflicting_evidence_ids": [],
  "reason_codes": []
}
```

Contract rules:

- `candidate_domain` must be null or exactly one supplied candidate. AI cannot invent
  a domain or browse.
- Every evidence ID must belong to the same canonical company and attempt/evidence
  package; unknown IDs reject the response.
- `OFFICIAL_WEBSITE` requires HIGH confidence plus later deterministic ownership
  validation. AI alone does not promote the site.
- `BRAND_OR_GROUP_WEBSITE` records a relationship and cannot populate the official
  website field without entity-specific deterministic proof.
- `NO_OFFICIAL_WEBSITE` is a versioned assertion, not proof of nonexistence; it
  requires exhausted configured search and no unresolved candidate conflict.
- LOW confidence maps to `INSUFFICIENT_EVIDENCE`.
- Human ground truth remains stronger; conflicting AI output is preserved, never
  overwritten or silently selected.

## Acceptance tests before implementation

- The 13 deterministic rows reach the expected official/related/UNKNOWN outcome
  without AI.
- The 14 AI-eligible rows are not sent to AI until P0/P1 are exhausted.
- No foreign, parent, marketplace, empty-content, or wrong-entity domain becomes
  official without the named deterministic evidence gate.
- Search and extraction failures are reported separately using stored query/result/
  observation lineage.
- Every output is replayable offline from frozen evidence.
