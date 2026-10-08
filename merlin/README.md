# MERLIN

MERLIN means **Market Exploration, Research & Lead Intelligence**. It is the
restricted customer-facing product over Dastabase's existing data, jobs, workers,
Knowledge Repository, enrichment modules, and exports.

`MerlinImportEnrichService` is the initial application boundary. It creates
workspace-owned uploads, queues the shared `IMPORT_ENRICH` job, and exposes only
workspace-scoped, customer-safe upload/job/final-artifact projections. It does not
import the Control Room web application and does not expose database paths,
provider details, Discovery run IDs, logs, or internal artifacts.

M1 contains no web routes, authentication, requested-output selection, provider
calls, Profile integration, or separate enrichment implementation.
