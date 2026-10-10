# MERLIN

MERLIN means **Market Exploration, Research & Lead Intelligence**. It is the
restricted customer-facing product over Dastabase's existing data, jobs, workers,
Knowledge Repository, enrichment modules, and exports.

`MerlinImportEnrichService` is the initial application boundary. It creates
workspace-owned uploads, queues the shared `IMPORT_ENRICH` job, and exposes only
workspace-scoped, customer-safe upload/job/final-artifact projections. It does not
import the Control Room web application and does not expose database paths,
provider details, Discovery run IDs, logs, or internal artifacts.

M2 persists an explicit, immutable requested-output set for every new MERLIN
Import & Enrich job. The fixed 0.1 vocabulary is `WEBSITE`, `EMAIL`, `PHONE`,
and `FINANCIALS`; Profile is not active. Existing accepted knowledge is reused
before selective Discovery is considered. Financials come only from canonical
2025 data and never create Discovery work.

M3 produces a customer-facing XLSX for both XLSX and CSV uploads. Source rows
are preserved, while the main data sheet receives only `Merlin match`,
`Merlin note`, and explicitly requested output columns. XLSX inputs retain the
existing lossless workbook guarantees; CSV workbooks are built from the durable
parsed upload rows. Legacy Control Room exports retain their existing contract.

There is still no commercial authentication, provider implementation, Profile
integration, or separate enrichment implementation.

M4 adds the separate server-rendered MERLIN application in `merlin.app`. It uses
one configured internal workspace stored in a signed session, queues the shared
durable Import & Enrich job, polls only a customer-safe status projection, and
downloads only the verified final workspace-owned XLSX. It does not register or
import Control Room or Monitor routes.

M5 supports realistic invited-beta imports by retaining one immutable Discovery
run per job while the existing runner works through deterministic bounded
manifest windows. Existing accepted knowledge is checked before planning, and
canonical company IDs are deduplicated before Discovery. The default total
Discovery ceiling is 200 unique companies with a batch size of 10; both are
worker configuration, and exceeding the ceiling fails explicitly without
truncating the cohort.

Customer progress is persisted with the current stage, completed/total units
when meaningful, stage start, and last activity. Matching uses source rows;
Discovery uses unique canonical companies. Knowledge checks and export remain
indeterminate rather than displaying a fabricated percentage. The customer
surface distinguishes queued, recently active, delayed, and terminal jobs from
persisted server timestamps. No ETA is shown yet: the stored Discovery stage
timestamps and completed units are the foundation, but an estimate is not
eligible until the actual Discovery cohort is known and enough current-job
units have completed to establish stable throughput.
