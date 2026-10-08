# Profile Activity

Profile Activity stores a frozen, auditable copy of accepted Discovery evidence in an isolated
SQLite artifact. Its deterministic layer follows the rule **fetch once, preserve evidence,
interpret many times**: extraction reads only copied `profile_evidence` and
`profile_content_blocks` and has no provider or network dependency.

`python -m profile_v1.runner run --results PROFILE.sqlite3 --run-id RUN_ID --deterministic-only`
runs metadata and narrow signal extraction without semantic interpretation. Ordinary `run` and
`replay` commands also create deterministic results before optional semantic claims.

Deterministic results are immutable per attempt. `profile_deterministic_results` records the rule
version, stable corpus hash, corpus status, and counts. `profile_deterministic_facts` stores one
normalized value per supported field. `profile_deterministic_fact_evidence` retains the copied
Profile evidence/block IDs and original Discovery provenance without duplicating raw HTML.

Only `FIRST_PARTY` evidence authorized by the selected Discovery result can create company facts.
Candidate and search evidence can describe corpus availability but cannot become first-party
metadata or signals. `UNKNOWN` means that no qualifying evidence exists in the preserved corpus;
it does not assert a negative fact.

`RICH_FIRST_PARTY` requires at least two substantive heading/paragraph/list blocks containing at
least 160 characters in total. A smaller accepted corpus is `THIN_FIRST_PARTY`; without accepted
first-party evidence the status is `CANDIDATE_ONLY`, `SEARCH_ONLY`, or `NO_EVIDENCE` in that order.

Schema version 4 adds deterministic storage and lineage guards. Opening a version 2 or 3 Profile
artifact through `profile_v1.store.Store` performs the explicit transactional migration. Other
schema versions are rejected.
