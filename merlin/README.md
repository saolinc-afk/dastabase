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

There are still no MERLIN web routes, authentication, request-aware workbook
columns, provider implementations, Profile integration, or separate enrichment
implementation.
