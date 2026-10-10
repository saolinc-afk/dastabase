# Discovery ambiguity benchmark — human review

This report is diagnostic. It does not change or promote Discovery results.

## Source

- `xlsx:ambiguity_human_review_100.xlsx:sha256:5450081cd9db4c7cf6c6c1373ba9cfb25060032077c1cf619f672e25ec441d4f`
- Reviewer: `Andrej Solinc`
- Reviewed at: `2026-10-10T20:04:40Z`
- Companies: 100

## Main findings

- Human review recorded a website domain for **48** companies.
- **32 / 48** human-found websites (66.7%) were already among candidates shown in the stored compact export. This is a lower bound on full-evidence presence.
- **16 / 48** human-found websites (33.3%) were absent from the compact candidate preview.
- **1** of those absent-domain rows had a truncated candidate preview and therefore remain unresolved rather than being labeled search misses.
- **13 / 48** human-found websites (27.1%) remain genuine search-recall/extraction misses after separating explicit relationship and wrong-entity cases.
- **45** companies are human-reviewed as likely having no official website; this is not an absolute nonexistence claim.
- **8** are brand/group/rebrand/parent/marketplace or multi-domain relationship cases.
- **4** are explicit wrong-entity or cross-country cases.

### Outcome counts

- COMPLEX_BRAND_OR_GROUP_RELATIONSHIP: 8
- CORRECT_DOMAIN_ALREADY_IN_STORED_EVIDENCE: 14
- CORRECT_DOMAIN_PRESENT_BUT_NOT_SELECTED: 14
- HUMAN_REVIEW_UNRESOLVED: 2
- LIKELY_NO_OFFICIAL_WEBSITE: 45
- OFFICIAL_EXISTS_BUT_SEARCH_MISSED: 13
- WRONG_ENTITY_OR_CROSS_COUNTRY: 4

The outcome names are deliberately narrow: `CORRECT_DOMAIN_ALREADY_IN_STORED_EVIDENCE` means the human domain was the diagnostic leader; `CORRECT_DOMAIN_PRESENT_BUT_NOT_SELECTED` means it was another stored candidate; and `OFFICIAL_EXISTS_BUT_SEARCH_MISSED` means no human-entered domain was in a complete compact candidate preview.

## Diagnostic-category trust

| Ambiguity category | n | leader supported | any candidate supported | search miss | likely no site | complex/wrong | unresolved |
|---|---:|---:|---:|---:|---:|---:|---:|
| CROSS_COUNTRY_COLLISION | 3 | 2 | 2 | 0 | 1 | 2 | 0 |
| MANY_LOW_SIGNAL_CANDIDATES | 3 | 1 | 2 | 0 | 0 | 1 | 0 |
| NO_MEANINGFUL_IDENTITY_SUPPORT | 30 | 2 | 5 | 8 | 14 | 3 | 0 |
| ONE_CLEAR_EVIDENCE_LEADER | 20 | 6 | 8 | 2 | 8 | 1 | 1 |
| ONE_MODERATE_LEADER | 20 | 5 | 6 | 2 | 10 | 3 | 0 |
| SAME_DOMAIN_VARIANTS | 1 | 1 | 1 | 0 | 0 | 1 | 0 |
| THIRD_PARTY_NOISE_DOMINATES | 20 | 1 | 5 | 1 | 12 | 1 | 1 |
| TWO_CLOSE_CANDIDATES | 3 | 0 | 3 | 0 | 0 | 0 | 0 |

“Leader supported” means the human-entered domain equals the diagnostic leader. It is not an acceptance-rate estimate. Likely-no-site, complex, and unresolved rows remain separate instead of being forced into correct/incorrect.

## Easy versus difficult

- Easy to find: 8; stored candidate present for 5; missed for 3.
- Difficult/impossible to find: 20; stored candidate present for 11; missed for 7.
- Easy misses point first to query/result extraction gaps. Difficult misses and relationship cases are the strongest selective-AI candidates.

## Deterministic false-negative mechanisms

- AUTHORIZATION_THRESHOLD_TOO_STRICT: 14
- CANDIDATE_RANKING_OR_ATTRIBUTION: 14
- ENTITY_OR_COUNTRY_COLLISION: 4
- ENTITY_RELATIONSHIP_NOT_MODELED: 8
- INSUFFICIENT_OR_TRUNCATED_EVIDENCE: 2
- NO_FALSE_NEGATIVE_SUPPORTED: 45
- SEARCH_RECALL_OR_EXTRACTION_GAP: 13

- A supported diagnostic leader that was not accepted indicates an authorization or attribution gap, not a need for broader AI search.
- A correct non-leading stored candidate indicates ranking, entity attribution, or conflict-resolution work for Resolver vNext.
- A human domain absent from a complete candidate preview indicates search recall or candidate extraction failure; truncated previews remain unresolved.
- Parent/group/rebrand/multi-domain cases require an explicit relationship model.
- Likely-no-site outcomes must remain UNKNOWN/likely absent rather than being converted into a false accepted website.

## Discovery vNext stages

1. Structured deterministic sources: exact registration/tax-linked sources; investigate Sloexport separately, without treating it as official-site proof.
2. Search collection: preserve query/result/rank/snippet and candidate extraction with explicit publisher and entity attribution.
3. Deterministic resolver: hard exclusions, relationship classification, evidence tiers, categorical margins, and frozen-evidence ownership validation.
4. Selective AI fallback: only after stages 1–3 remain ambiguous, for difficult search interpretation, entity relationships, or cross-country collisions.
5. UNKNOWN/likely-no-website: retain this outcome when evidence cannot support a site; never force a winner.

AI is not allowed when a deterministic evidence tier can resolve the candidate, merely because a low score or old threshold blocked it. AI output remains a versioned assertion and cannot outrank conflicting human ground truth.
