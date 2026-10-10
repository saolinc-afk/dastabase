# Discovery ambiguity audit and resolver design

`ambiguity_audit.py` is an offline, read-only subdivision of recovery rows already
classified as `MULTIPLE_AMBIGUOUS_CANDIDATES`. It reuses the coverage/recovery
schema validation, frozen-identity boundary, selected-attempt semantics, and
read-only SQLite connection. It never imports the runner, search provider, or an
HTTP client and never changes a candidate or accepted result.

## Why the recovery bucket is broad

The 8,705 count does not mean 8,705 companies have multiple equally credible
official websites. It primarily reflects a deliberate mismatch between lossless
collection and the coarse recovery diagnostic:

- Every direct search-result URL is persisted as a `WEBSITE_CANDIDATE`, even when
  the result is only a weak name match. URLs and non-public email domains appearing
  in snippets are persisted too.
- The recovery audit calls every candidate host "plausible" unless it is the
  already accepted host or matches the intentionally small explicit
  `domain_policy` block registry. It does not apply runner `eligible()`, candidate
  fusion, fetched-page ownership, or search-resolver authorization.
- Unknown third-party publishers are intentionally not guessed into the registry,
  so directories, media, profiles, and unrelated same-name entities outside that
  registry remain in the plausible count.
- Recovery groups exact hosts. A root and a company-specific subdomain can
  therefore look like two candidates even when both belong to one registrable
  domain.
- A search result can contain target identity and several URLs in the same stored
  evidence row. That co-location is useful provenance but does not, by itself,
  attribute every URL to the target.

The expected explanation is therefore a mixture dominated by broad candidate
collection/audit classification, plus unknown third-party noise and same-domain
variants. Genuine multi-site entities, cross-country same-name collisions, and
weak verification margins remain real subsets. The production ambiguity audit is
needed to quantify the mixture.

## Diagnostic classification

Candidate hosts are grouped by an offline registrable-domain approximation. The
audit has an explicit bounded list for common compound suffixes and otherwise uses
the final two labels. It does not download a public-suffix list. Both original host
and grouped domain remain in the CSV.

Each candidate records:

- exact tax, registration, legal-name, address, street, postal, and municipality
  observations linked to its evidence;
- persisted identifier/address/entity conflicts;
- attributed email-domain and phone agreement;
- raw/unattributed email-domain agreement as a separate weaker signal;
- evidence IDs, observation IDs, evidence count, publishers, providers, methods,
  query recurrence, best rank, broad identity flag, and direct brand relationship;
- exact domain-policy classification, blocked status, country-code flag, and a
  diagnostic type.

Candidate types are `LIKELY_OFFICIAL_OR_BUSINESS_DOMAIN`,
`DIRECTORY_OR_COMPANY_DATABASE`, `SOCIAL_NETWORK`,
`MARKETPLACE_OR_PLATFORM`, `NEWS_OR_MEDIA`,
`GOVERNMENT_OR_PUBLIC_REGISTRY`, `OTHER_REGISTERED_THIRD_PARTY`, and
`UNCLASSIFIED_DOMAIN`. "Likely" requires positive persisted support and is not an
acceptance decision. Unregistered hosts stay unclassified rather than being
guessed from prose.

The mutually exclusive company categories use this precedence:

1. `SAME_DOMAIN_VARIANTS`: multiple hosts collapse to one registrable domain;
2. `ONE_CLEAR_EVIDENCE_LEADER`: an exact identifier or equivalent high-grade
   identity/contact basis leads by at least 50 diagnostic points;
3. `ONE_MODERATE_LEADER`: legal/address/contact support scores at least 45 and
   leads by at least 20 without persisted conflict;
4. `CROSS_COUNTRY_COLLISION`: supported country-code candidates span countries;
5. `THIRD_PARTY_NOISE_DOMINATES`: known blocked operators comprise at least half
   of all candidate hosts and no leader qualifies;
6. `NO_MEANINGFUL_IDENTITY_SUPPORT`: all plausible candidates have only broad
   name/search/rank evidence;
7. `TWO_CLOSE_CANDIDATES`: two registrable domains remain without a leader;
8. `MANY_LOW_SIGNAL_CANDIDATES`: three or more remain without a leader.

The score is a transparent audit measurement, not a proposed acceptance threshold.
Exact tax/registration is weighted above attributable email, which is above
address, phone, legal name, recurrence, brand, and rank. Identifier/entity/address
conflicts and registered blocked operators carry strong negative values. The JSON
report explicitly states that neither score nor leader means official website.

## Outputs and sample

- `ambiguity_summary.json`: cardinality, category, candidate-type, and negative
  signal counts plus category/cardinality and category/type-presence cross-tabs;
- `ambiguity_companies.csv`: every ambiguous company with compact candidate JSON;
- `ambiguity_sample_100.csv`: deterministic category-round-robin sample;
- `AMBIGUITY_REPORT.md`: concise production counts and interpretation.

The sample retains canonical identity, accepted website if any, candidate URLs,
compact support/conflict signals, type, rank/query recurrence, evidence lineage,
leader/margin, and category. Raw HTML, evidence payloads, and full snippets are
not exported.

## Deterministic resolver design

The repository already names its current categorical component
`search-evidence-resolver-v2`. A future post-run resolver must use a new immutable
version identifier (for example `stored-evidence-resolver-v3`) rather than silently
changing the meaning of the existing v2 artifact.

Recommended stages:

1. **Normalize without deciding.** Canonicalize URL/host, group root/subdomain and
   path variants, retain the strongest entity-specific scope, and keep every
   original observation/evidence ID.
2. **Apply hard exclusions.** Registered directory/social/marketplace/media/public
   operators cannot be official. Exact target identifier conflicts and explicit
   unrelated-entity/operator conflicts disqualify that candidate. A foreign TLD
   alone is not a conflict, but conflicting-country evidence forces review.
3. **Build evidence tiers, not a free-running additive winner.**
   - Tier A: exact tax or registration linked to the candidate's fetched scope, or
     to an explicit target-to-domain statement from a trusted independent source.
   - Tier B: attributed company email-domain agreement plus exact legal/address
     evidence, with no conflict.
   - Tier C: exact legal name plus address/postal/city or attributed phone, supported
     by independent evidence/publishers.
   - Weak tier: broad name match, brand/domain spelling, search recurrence, and
     rank. These can order review but never authorize a website alone.
4. **Require attribution of the bridge.** Identity and a URL merely co-occurring in
   one broad snippet/page is insufficient. The stored locator/method and source
   relationship must show that the identity assertion supports that domain.
5. **Require a categorical margin.** A single highest tier with no peer/conflict may
   advance to offline ownership validation. Two candidates in the same highest
   tier remain `REVIEW`, regardless of numerical score.
6. **Revalidate ownership from frozen evidence.** Reuse the existing P1A/final-URL
   and source-scope rules against stored pages. If required content was never
   fetched, remain `PARTIAL/UNKNOWN`; do not infer acceptance from search evidence.
7. **Resolve entity relationships explicitly.** Brand, parent, subsidiary, and
   entity-on-group-domain cases require scoped relationship evidence. A group root
   must not automatically become the legal entity's site.
8. **Resolve contacts only after scope.** Email/phone attribution remains bounded to
   the accepted entity/site scope. Raw contacts can propose candidates but cannot
   become accepted facts.
9. **Represent no-site outcomes conservatively.** Completed searches containing
   only hard-excluded operators may support `NO_SUPPORTED_WEBSITE`; transport
   failure, same-name collision, or weak unclassified candidates remain unknown.
10. **Reuse for FAILED without retrying.** The same pure resolver can consume the
    latest FAILED attempt's stored evidence. Network acquisition, if later approved,
    must be a separately frozen FAILED-only run—not normal resume.

This design intentionally prefers false negatives and `UNKNOWN/PARTIAL` over a
wrong official website.

## Selective AI fallback

AI should run only after deterministic normalization/exclusion and only on cases
still in `TWO_CLOSE_CANDIDATES`, `MANY_LOW_SIGNAL_CANDIDATES`,
`CROSS_COUNTRY_COLLISION`, or `NO_MEANINGFUL_IDENTITY_SUPPORT`. It receives compact
stored excerpts and structured signals, never browses, and must return a schema
such as:

```json
{
  "judgment": "CANDIDATE_DOMAIN|NO_OFFICIAL_WEBSITE|INSUFFICIENT_EVIDENCE",
  "candidate_domain": null,
  "confidence": "LOW|MEDIUM|HIGH",
  "supporting_evidence_ids": [],
  "conflicting_evidence_ids": [],
  "reason_codes": []
}
```

Every cited ID must belong to the same company/attempt and be validated before the
judgment is stored. AI output remains review evidence unless separately validated
by deterministic ownership rules.

Until the production breakdown is run, a cautious planning estimate is that
roughly **50–80%** of the 8,705 cases may still require AI/human review or remain
unknown. This is explicitly a hypothesis: the measured clear/moderate leader,
same-domain, close-candidate, and no-support counts should replace it.

## Production command

From `/home/saolinc/dastabase`, choose a new output directory:

```sh
.venv/bin/python -m discovery_v2.ambiguity_audit \
  --master database/dastabase_lite_18916_20261001.db \
  --results /home/saolinc/dastabase-runs/discovery-v2/18916-serper-20261004/results.sqlite3 \
  --run-id 402b7d44275344aeaae8ece8191fad64 \
  --output-dir /home/saolinc/dastabase-runs/discovery-v2/18916-serper-20261004/ambiguity-audit-20261010 \
  --sample-size 100 \
  --seed 20261010
```

## Human-review export

`discovery_v2.ambiguity_review` turns the completed audit into a compact,
read-only 100-company review sheet. It reuses `ambiguity_sample_100.csv` only
when that file has approximately the requested 30/20/20/20 primary-category
mix and 10 tail cases; otherwise it derives a stable hash-based stratified
sample from `ambiguity_companies.csv`. Candidate summaries and evidence previews
come only from persisted audit/result rows. Search snippets remain diagnostic
evidence and never become accepted websites.

The exporter opens both SQLite inputs read-only, performs no provider or HTTP
calls, and writes only into a new output directory. From
`/home/saolinc/dastabase` on Duke:

```sh
.venv/bin/python -m discovery_v2.ambiguity_review \
  --master database/dastabase_lite_18916_20261001.db \
  --results /home/saolinc/dastabase-runs/discovery-v2/18916-serper-20261004/results.sqlite3 \
  --run-id 402b7d44275344aeaae8ece8191fad64 \
  --ambiguity-companies /home/saolinc/dastabase-runs/discovery-v2/18916-serper-20261004/ambiguity-audit-20261010/ambiguity_companies.csv \
  --existing-sample /home/saolinc/dastabase-runs/discovery-v2/18916-serper-20261004/ambiguity-audit-20261010/ambiguity_sample_100.csv \
  --output-dir /home/saolinc/dastabase-runs/discovery-v2/18916-serper-20261004/ambiguity-human-review-20261010 \
  --seed 20261010 \
  --max-candidates 6
```

The new directory contains `ambiguity_human_review_100.csv` and
`AMBIGUITY_HUMAN_REVIEW_GUIDE.md`. Existing outputs are not overwritten unless
`--overwrite` is explicitly supplied.
