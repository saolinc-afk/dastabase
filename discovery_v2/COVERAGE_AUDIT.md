# Discovery coverage / failure funnel audit

`coverage_audit.py` is an offline, read-only analysis tool for recall-recovery
planning. It does not import the Discovery runner or search/AI providers and never
instantiates an HTTP client. SQLite inputs are opened with `mode=ro`, `query_only`,
autocommit, and a short busy timeout. Each source is closed before reports are written.

## Inputs and current-result semantics

- `--master` is the canonical `companies_lite` population and its latest legacy
  `website_discovery` row per company.
- `--current-results` and `--current-run-id` identify the final production run.
- `--reusable-results RESULTS_DB=RUN_ID` may be repeated for earlier Discovery v2
  runs that supplied reusable coverage but were not published to the master DB.
- A v2 result is current only when its attempt is referenced by
  `discovery_run_companies.selected_attempt_id`. Historical attempts/results are
  used for attempt-status totals only and never leak into current coverage.
- Results whose company ID is absent from `companies_lite` are reported under the
  master boundary and excluded from all population/cohort metrics.
- In-boundary result IDs are checked against the run's frozen tax/registration
  identity where both sides provide it; a mismatch aborts rather than joining an
  unrelated source namespace by coincidental numeric ID.

The output directory is the only write target. Existing reports are not replaced
unless `--overwrite` is explicit. The five outputs are:

- `coverage_companies.csv`: one row per canonical master company;
- `unresolved_companies.csv`: all recovery cohorts except `SOLVED` and `INELIGIBLE`;
- `samples.csv`: deterministic fixed-seed cohort samples;
- `snippet_support.csv`: one compact provenance row per persisted snippet-derived
  website candidate, linked to its provider/query/result/rank and
  observation/evidence IDs;
- `summary.json`: funnel, status, reason, cohort, and commercial segment counts.

CSV rows contain compact database/run/attempt/result references and counts, not
stored HTML or large diagnostics.

## Persisted snippet candidate evidence

Discovery v2 persists each search hit as `SEARCH_RESULT` evidence, including its
provider, query type/text, rank, result URL/host, title, and snippet. Candidate
observations derived from that result are linked by `evidence_id`. The audit uses
the existing `WEBSITE_CANDIDATE.extraction_method` contract:

- `snippet_url` becomes `SNIPPET_URL` support;
- `email_domain` becomes `SNIPPET_EMAIL_DOMAIN` support.

It does not reparse stored title/snippet text. Newer artifacts also duplicate the
origin and source-result reference in `value_json`; older schema-v3 artifacts do
not, so the extraction method and linked evidence remain authoritative. Exact
match offsets/spans within the title or snippet are not persisted.

Some older artifacts also contain `email_domain` observations for public mailbox
providers (for example, Gmail). Current Discovery code excludes public-mail
domains before creating this candidate type. The audit preserves and labels what
the historical artifact actually stored; it does not treat that observation as an
accepted website or silently rewrite the source record.

These are candidate-support diagnostics only. They do not select an official
website, create usable coverage, or move a company to `SOLVED`. Company and
unresolved outputs expose per-origin counts, distinct supported-domain counts,
identity-match counts, evidence/observation IDs, and unresolved-support flags.
`summary.json` includes overall totals plus recovery-cohort x snippet-support
cross-tabs.

The dedicated provenance CSV also retains the observation's raw extracted value
(the URL found in text, or the email that supplied a domain), normalized candidate
URL/domain, source locator, provider/query, result rank/URL/host, and stable
evidence/observation lineage IDs. Raw title/snippet prose remains in the source
evidence table and is traceable by ID; it is not duplicated into the report.

Directory/source recognition is intentionally narrow. A source is labelled
`registered_directory_source` only when its stored result URL/host exactly matches
the repository's explicit domain-policy registry with classification `DIRECTORY`
or `COMPANY_DATABASE`. Stored `source_class=THIRD_PARTY` is too broad for this
purpose, and unregistered publishers remain `UNREGISTERED`. The summary records a
hash of the policy registry used, making the classification reproducible.

## Funnel definitions

Funnel stages are deliberately not treated as a partition:

- **Usable/reusable coverage**: an accepted official website, attributable email,
  or attributable phone exists in any supplied source.
- **Official website selected**: latest legacy `VERIFIED`, or selected-attempt v2
  `VERIFIED`, `HIGH`, or `MEDIUM`, with a nonempty official URL.
- **Attributable email**: a v2 attributed default company email, or a legacy email
  revalidated by the repository's pure `email_attribution` rule against the latest
  `VERIFIED`, current-rule website, ownership scope, source page, and bounded contact
  evidence. Old, malformed, or scope-mismatched rows are not counted.
- **Attributable phone**: a v2 attributed default company phone. A generic
  canonical phone column is not silently relabelled as Discovery attribution.
- **Website without email**: accepted website but no attributable email.
- **Search evidence without website**: latest-attempt `SEARCH_RESULT` evidence but
  no accepted website from any supplied source.
- **No useful candidate**: no accepted website and no v2 `WEBSITE_CANDIDATE` or
  nonempty rejected legacy candidate.
- Failed, ineligible, and other unresolved counts are current-run dimensions over
  companies without an accepted website.

## Status meanings

- `COMPLETED`: an atomically selected result exists and acquisition was not marked
  incomplete. This does **not** mean a website was found; the result may be
  `REVIEW` or `NOT_FOUND`.
- `PARTIAL`: a useful selected result exists, but search/transport failure made
  `contact_outcome=INCOMPLETE`; it remains retryable.
- `FAILED`: processing raised an exception; attempt evidence remains.
- `INELIGIBLE`: frozen legal identity matched the explicit bankruptcy exclusion;
  no attempt was made.
- `PENDING`: not attempted yet, or returned to pending after interruption.
- `RUNNING`: an attempt is active.
- `INTERRUPTED`: historical attempt status. The company is returned to `PENDING`,
  so attempt-status counts and manifest-status counts are reported separately.

All real stored statuses are counted dynamically; unknown future labels are not
folded into a misleading bucket.

## Diagnostic reason codes

Reasons overlap. They are emitted only from persisted state/evidence:

- lifecycle: `INELIGIBLE_BANKRUPTCY`, `INELIGIBLE_OTHER`, `ATTEMPT_FAILED`,
  `ATTEMPT_INTERRUPTED`, `ATTEMPT_RUNNING`, `NOT_ATTEMPTED`,
  `NOT_IN_CURRENT_MANIFEST`, `DISCOVERY_INCOMPLETE`;
- search/candidate: `SEARCH_FAILED`, `SEARCH_BUDGET_EXHAUSTED`,
  `SEARCH_NO_RESULTS`, `SEARCH_EVIDENCE_NO_ACCEPTED_WEBSITE`,
  `CANDIDATES_NOT_VERIFIED`, `NO_USEFUL_CANDIDATE`, `NO_DISCOVERY_EVIDENCE`,
  `AMBIGUOUS_IDENTITY`, `OWNERSHIP_EVIDENCE_INSUFFICIENT`,
  `THIRD_PARTY_OR_DISALLOWED_CANDIDATE`;
- snippet support: `SNIPPET_URL_CANDIDATE`,
  `SNIPPET_EMAIL_DOMAIN_CANDIDATE`, `SNIPPET_DOMAIN_SUPPORT_UNRESOLVED`,
  `REGISTERED_DIRECTORY_SNIPPET_SUPPORT`;
- acquisition: `FETCH_DNS_FAILURE`, `FETCH_TIMEOUT`, `FETCH_ACCESS_BLOCKED`,
  `FETCH_TLS_FAILURE`, `FETCH_REDIRECT_OUT_OF_SCOPE`, `FETCH_HTTP_FAILURE`,
  `FETCH_NETWORK_OTHER`;
- contact: `WEBSITE_NO_ATTRIBUTED_EMAIL`, `WEBSITE_NO_ATTRIBUTED_PHONE`,
  `EMAIL_CANDIDATE_NOT_ATTRIBUTED`, `PHONE_CANDIDATE_NOT_ATTRIBUTED`;
- legacy/data quality: `LEGACY_CANDIDATE_NOT_ACCEPTED`,
  `LEGACY_DISCOVERY_ERROR`, `LEGACY_NOT_FOUND`,
  `MALFORMED_STORED_DIAGNOSTICS`.

Exact definitions are also embedded in `summary.json`.

## Recovery cohorts and precedence

Each master company receives exactly one cohort in this order:

1. `INELIGIBLE`: explicit current-run ineligibility; do not send to recovery.
2. `SOLVED`: accepted website plus attributable email; phone remains a separate
   diagnostic dimension.
3. `CONTACT_RECOVERY`: accepted website but no attributable email.
4. `AMBIGUOUS`: no accepted website and explicit identity/ownership ambiguity.
5. `HIGH_POTENTIAL_RECOVERY`: no accepted website but a persisted candidate.
6. `LOW_SIGNAL_LOW_VALUE`: completed zero-result search, no candidate/failure,
   employees below 1 and revenue below EUR 100k. Missing commercial data never
   qualifies a company for this cohort.
7. `SEARCH_RECOVERY`: all remaining eligible companies without an accepted site.

Each cohort is segmented in JSON by employee band (`<1`, `1-4`, `5-9`, `10-49`,
`50-249`, `250+`), revenue band, and reliable leading postal prefix. Registered
activity is included only if the canonical `companies_lite` schema actually has a
recognized activity column; otherwise it is explicitly `UNAVAILABLE`.

## Production command after the run finishes

From `/home/saolinc/dastabase`:

```sh
.venv/bin/python -m discovery_v2.coverage_audit \
  --master database/dastabase_lite_18916_20261001.db \
  --current-results /home/saolinc/dastabase-runs/discovery-v2/18916-serper-20261004/results.sqlite3 \
  --current-run-id 402b7d44275344aeaae8ece8191fad64 \
  --output-dir /home/saolinc/dastabase-runs/discovery-v2/18916-serper-20261004/coverage-audit \
  --sample-size 25 \
  --seed 20261008
```

For each earlier v2 database that contributed to the 3,278 reusable companies but
is not already represented by accepted master rows, append:

```sh
  --reusable-results /absolute/path/to/results.sqlite3=RUN_ID
```

Do not guess those paths or run IDs: omitting an unavailable source is recorded as
a data limitation, while supplying the wrong source would distort coverage.

## Stored-evidence recovery opportunity audit

`recovery_audit.py` is the second, narrower pass over the completed current run.
It reuses this audit's schema validation, master-boundary checks, selected/latest
attempt semantics, read-only SQLite connections, and atomic non-overwriting report
writers. It examines only companies whose manifest status is `PARTIAL` or `FAILED`.
It does not import the runner or a provider, reparse raw HTML, accept a website, or
write knowledge back to any database.

For `PARTIAL`, observed email domains are kept in three explicit dimensions:
attributed domains, raw/unattributed non-public domains, and public/free-mail
domains. Multi-evidence means the same non-public domain occurs in at least two
distinct persisted evidence records, not merely twice in one page. Visited-page
counts likewise separate attributable contacts from raw observations.

The recovery buckets are diagnostic and use this precedence:

1. `MULTIPLE_AMBIGUOUS_CANDIDATES`: two or more non-disallowed stored website
   domains remain, even if one has stronger support;
2. `STRONG_STORED_WEBSITE_CANDIDATE`: the sole plausible domain has persisted
   exact tax/registration/address support, identity-matched support across two
   evidence records and publishers, or identity plus attributed-email agreement;
3. `STRONG_EMAIL_DOMAIN_CANDIDATE`: the sole attributed non-public email domain
   differs from the accepted website and has two evidence records or a matching
   identity-supported website observation;
4. `CONTACT_RECOVERY_ONLY`: stored visited-page contact observations are absent
   from the selected defaults, without a stronger domain signal;
5. `STORED_EVIDENCE_LOW_SIGNAL`: evidence exists but meets none of the above;
6. `LIKELY_NEEDS_NEW_SEARCH`: no plausible stored domain/contact opportunity is
   present.

These criteria intentionally prefer ambiguity over selection. A "strong" signal
is still only a candidate for human/second-pass interpretation. Registered
directory recognition uses only exact matches from the domain-policy registry;
directory domains remain disallowed as official websites. Activity observations
are counted, but no business/activity semantic consistency is claimed because the
artifact does not persist a universal consistency verdict.

For `FAILED`, the audit classifies persisted diagnostic text into circuit or
infrastructure, provider/search, timeout, HTTP/access, DNS/network,
verification, parser/extraction, exhausted-candidate, no-useful-candidate, or
unknown categories. It also distinguishes failure before search evidence, after
search evidence, and after candidate evidence. A bounded retry indication is
limited to persisted transient categories; it is not an execution command.
Ordinary `resume` must not be used for a FAILED-only pass because it also selects
`PARTIAL`. A later implementation should create an explicit frozen ID manifest
from the audited FAILED IDs and execute it as a separate run/artifact.

The four outputs are `recovery_summary.json`, `partial_recovery.csv`,
`failed_analysis.csv`, and `RECOVERY_REPORT.md`. Candidate provenance is compact:
evidence/observation IDs, candidate URL/domain, source kind, provider publisher,
query occurrence/rank, and persisted identity/contact support. Raw page HTML and
full snippet bodies are not exported.

Run on Duke only after deploying/reviewing this code, from
`/home/saolinc/dastabase`, and choose a new output directory:

```sh
.venv/bin/python -m discovery_v2.recovery_audit \
  --master database/dastabase_lite_18916_20261001.db \
  --results /home/saolinc/dastabase-runs/discovery-v2/18916-serper-20261004/results.sqlite3 \
  --run-id 402b7d44275344aeaae8ece8191fad64 \
  --output-dir /home/saolinc/dastabase-runs/discovery-v2/18916-serper-20261004/recovery-opportunity-audit-20261010
```
