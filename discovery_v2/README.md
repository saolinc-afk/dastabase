# Discovery v2 — Phase A

An isolated `DISCOVERY_CONTACTS` module. It reads company identities from a frozen
Lite snapshot and writes only to a separate Discovery v2 SQLite database. It does
not publish to Lite, the existing V2 database, or Monitor. `PROFILE_ACTIVITY` and
`SUCCESSION` are reserved future modules, not stages of this job.

## Offline tests (run these first)

From the repository root, using the existing virtual environment:

```sh
.venv/bin/python -m unittest discover -s discovery_v2/tests -v
.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

The new tests use temporary source/result databases and fake search/HTTP responses;
they forbid live HTTP/search. The existing browser regression requires a usable
local Chromium installation and permission to launch it. No new dependencies.

## Create a small run without network access

Use an already frozen, checkpointed SQLite source containing `companies_lite`.
The source must have no nonempty WAL/journal and must remain unchanged while the
run is created/executed/resumed. No checkpoint or schema operation is issued to it.
Only identity columns (`id`, `company_name`, `tax_number`, `registration_number`,
`address`, `municipality`) are read. Old website/email tables are never read.

The following exact commands create a **one-company** manifest using source ID 1.
Choose a different explicit ID if needed. `/tmp` keeps experimental results out of
the repository; use a durable directory outside `database/` for retained results.
Creating the manifest does not perform discovery or access the network.

```sh
.venv/bin/python -m discovery_v2 create \
  --source database/dastabase_lite.db \
  --namespace sparrow-0.9-frozen-input \
  --results /tmp/dastabase-discovery-v2-small.sqlite3 \
  --ids 1 \
  --job-type DISCOVERY_CONTACTS \
  --execution-mode FRESH_DISCOVERY > /tmp/dastabase-discovery-v2-run.json
```

The descriptor in `discovery_runs.source_snapshot_json` contains the namespace,
resolved source path, SHA-256, byte size and table name. There is no snapshot registry.
The selected identities, their order and configuration are hashed too. Company IDs
are scoped to the source namespace and run, not assumed to be existing V2 IDs.

## Optional live execution — separate explicit step

Only after the offline tests pass and a small live test is intended:

```sh
RUN_ID=$(.venv/bin/python -c 'import json; print(json.load(open("/tmp/dastabase-discovery-v2-run.json"))["run_id"])')
.venv/bin/python -m discovery_v2 run \
  --results /tmp/dastabase-discovery-v2-small.sqlite3 \
  --run-id "$RUN_ID" --max-items 1
```

Search uses Serper when `SERPER_API_KEY` is present in the runner environment and
DDGS otherwise. The key is used only as the `X-API-KEY` request header and is not
stored in evidence or diagnostics. Do not use the locked-200 or full population
for a small live check.
There is no implicit “all companies” selection; `--ids` is mandatory at creation.
Several IDs use `--ids 1,2,3`. Larger manifests are processed serially in chunks
(`--batch-size 100`, maximum 200); a 500-company job does not require one transaction
or one in-memory working set for all evidence.

Resume the same manifest, skipping only successfully committed companies:

```sh
.venv/bin/python -m discovery_v2 resume \
  --results /tmp/dastabase-discovery-v2-small.sqlite3 \
  --run-id "$RUN_ID" --max-items 1
```

`run` and `resume` use the same idempotent execution path. To force genuinely new
discovery of an already completed company, issue `create` again and use its new
run ID. No cache or VERIFIED result is reused across attempts. Within an attempt,
the fetcher may cache a response to avoid fetching the same page repeatedly.

## Evidence and contact behavior

Each attempt records these search types:

| Query type | Query |
| --- | --- |
| `LEGAL_COMPANY_CONTACT` | `Podjetje <legal company name> kontakt` |
| `LEGAL_NAME_CONTACT` | `<legal company name> kontakt` |
| `LEGAL_COMPANY_DATABASE` | `Podjetje <legal company name> bizi.si` |
| `DOMAIN_CONTACT` | `<likely official domain> kontakt` |

Serper executes them as escalation stages. It starts with
`LEGAL_COMPANY_CONTACT`; once that evidence verifies an official website, later
searches are skipped with explicit reasons. Otherwise it proceeds conservatively
through the remaining name queries and, when useful, `DOMAIN_CONTACT`. DDGS keeps
the original schedule of all three name queries before candidate evaluation. The
domain query runs once a likely eligible domain exists; otherwise its explicit skip
reason is recorded. `--use-municipality` adds municipality to name queries.
Default budgets: six retained results/query, eight website candidates,
36 HTTP requests and eight contact crawl pages per company. Candidate verification
uses the existing stateless verifier plus narrowly corroborated v2 brand/subdomain
rules behind `interfaces.py`; the old runner,
fallback strategy and persistence are never invoked. The reused verifier rule
version is recorded in each attempt and website assessment. Tuning config/code
requires version discipline: do not silently change rules for an existing run.

All returned search results within budget are saved with query, rank, provider,
URL/host, title, snippet, timestamps and full provider payload **before** official
website eligibility filtering. Observations include website/domain, email, phone,
identity matches, explicit SKD wording and unclassified descriptive source wording.
Generated domain guesses have `GENERATED_CANDIDATE` evidence so even hypotheses
have an origin. Fetched HTML, visible text, final URL and content hash are retained.

Bizi and known third-party publishers can supply candidate observations. They are
never fetched by a Bizi adapter or made official merely by matching the company.
Unknown directories are also assessed by the verifier's structural ownership rules.
Search snippets alone cannot attribute a contact or make it the default. An
independently verified first-party publication can corroborate the same normalized
contact while retaining all its search provenance. Website ownership and contact
attribution are separate decisions, with tenant/path scope respected.

Every contact references a primary observation and all supporting observations.
Per-observation attribution decisions preserve conflicting or rejected evidence.
Public email addresses on different domains are accepted when the verified page
explicitly attributes them to the target entity. Same-domain membership alone does
not establish attribution. No deliverability check is performed.

Roles: `GENERAL`, `SALES`, `MANAGEMENT`, `PURCHASING`, `ACCOUNTING`, `HR`, `SUPPORT`,
`MARKETING`, `PERSON`, `UNKNOWN`. Email local parts provide conservative role hints;
they do not establish a person's identity. Nullable `person_name`, `person_title`,
`department` and person-context fields are reserved without person enrichment.
Phone roles remain `UNKNOWN` in Phase A. Raw phone values and extensions are
retained; national numbers are not silently assigned a country code.

Defaults are attempt-local, attributed company contacts only. General mailboxes
come first, followed by departments and other company contacts. Explicit company
and contact-section evidence precede the final lexical tie-break. Person contacts are never automatic
defaults. All additional contacts remain available with their attribution status.
Activity observations are reusable evidence, not PROFILE_ACTIVITY results. An
explicit SKD observation is not automatically authoritative and is not used to
infer actual business.

## Storage, isolation and recovery

Exactly seven tables:

| Table | Purpose |
| --- | --- |
| `discovery_runs` | Module, versions, source descriptor, manifest/config hashes |
| `discovery_run_companies` | Frozen identities, order, status, selected attempt |
| `discovery_attempts` | Retry history, counters and query/fetch diagnostics |
| `discovery_evidence` | Search results, pages and generated hypotheses |
| `discovery_observations` | Derived values with source/method/locator provenance |
| `discovery_contacts` | Deduplicated attempt-local contacts and attribution |
| `discovery_company_results` | Website assessment and selected defaults |

Composite foreign keys reject cross-company/run/attempt primary references.
Supplemental JSON references are checked before committing. Contacts, defaults,
company result and selected attempt are committed in a single transaction. Evidence
and diagnostics persist as work progresses, including when completion fails.
The application ID and schema version prevent opening another application's database
for writes. Source paths, production database directory, symlink files and hardlinks
are rejected as result destinations. No production database migration is included.

Company eligibility is decided from the frozen legal name before an attempt is
created. Unicode case folding, punctuation-to-space conversion and whitespace
collapsing normalize the name. The exact token sequence `v stečaju` records
`eligibility_status=INELIGIBLE`, `eligibility_reason=BANKRUPTCY` and terminal
company status `INELIGIBLE`. It creates no attempt, search, fetch, evidence or
contact work. No other legal-status phrase is excluded.

An OS lock prevents concurrent runner invocations from taking each other's work;
it releases automatically on process exit. Interrupted attempts remain historical;
resume creates a new attempt. Search/transport failures retain diagnostics and are
retried on resume, rather than silently publishing a definitive absence. Missing
optional pages and unsuccessful guesses are recorded; failures before an eventual
verified website do not invalidate that website. A failed run can be partial; CLI
exit code 1 indicates company failures or retryable partial attempts, 2 indicates invalid input. `--max-items`
may intentionally leave a healthy run partial.

For current results, use `Store.current_results(run_id)` or join results on
`discovery_run_companies.selected_attempt_id`. Do not union historical contact rows
across attempts/runs and call that “current.” No global current-result publishing
or source-database update is implemented.

Phase A limitations: the frozen ownership code remains unchanged, with a narrow
v2 adapter for corroborated brand/subdomain relationships;
search provider quality/budgets and live contact recall remain unbenchmarked.
No source-specific company-database adapters, obfuscated-email extraction, full
person enrichment, activity classification or production publishing are included.


## Forensic correction checkpoint (engine phase-a-3, schema 3)

Create a **new result database** for this version. Schema 3 adds the company-level
eligibility outcome and reason. Earlier result databases are refused without
migration; the original live validation remains an untouched baseline.
No production schema or source database is changed. Resume works within a new
schema-3 run, including partial companies, and retains prior attempts/results.

Contacts are extracted from visible markup only (comments, scripts/templates,
explicit hidden/aria-hidden and inline CSS-hidden content are excluded). Original
HTML is preserved. The v2 context helper chooses local paragraphs/cards and headings;
contact excerpts include the value. Distributor/partner/regulator/vendor context
prevents attribution. A verified first-party contact section or independent,
entity-specific corroboration can support a different-domain mailbox. Domain
matching alone does not attribute a contact. Company/department aliases are handled
before person-name hints; automatic person defaults remain disabled.

Explicit international optional-trunk notation (`+386 (0)...`) is normalized without
inventing a country code for national numbers. Visible numbers under call/phone
labels are extracted, including Slovenian “Pokličite nas”. Raw values are retained.

Candidate ordering prioritizes company-brand/contact evidence and reserves up to
two slots for strong generated/email-domain hypotheses. Repeated hosts are evaluated
once. Known job portals, directory/profile/news/social results and unrelated results
cannot consume the official-site budget merely by appearing in search. Domain
queries require a company-brand candidate or verified scope. Company-branded parent
domains with product subdomains, and reordered brand tokens, require strong
identity corroboration and company-page presentation; tax/name matches alone do
not establish ownership, and unrelated hosting tenants receive no new exemption.

Access-denial blocking is host-scoped while the request budget stays company-scoped.
Incomplete network acquisition commits evidence, observations, contact assessments
and a result with `contact_outcome=INCOMPLETE`, `PARTIAL` attempt/company state,
and `diagnostics.completeness` (including retryability). Verified facts may remain
in that partial result; callers must inspect completeness before treating a run as
finished. A failed candidate never establishes absence of the company. Resume
creates a new attempt and replaces the selected result only on a successful atomic
commit; historical partial assessments remain available.

Offline fixtures in `tests/fixtures/live5.json` contain original saved page HTML and
search payloads from run `d40ba21a6e3b45fa9353567023b88982`. Tests read this portable
fixture, never the original temporary database. Remaining limits: CSS computed via
external stylesheets and JavaScript-rendered content are not evaluated; context,
roles and branding remain conservative heuristics. Source-specific adapters and
live provider relevance/recall tuning remain outside this correction checkpoint.
