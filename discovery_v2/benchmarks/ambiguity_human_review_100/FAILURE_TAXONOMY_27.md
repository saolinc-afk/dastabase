# Failure taxonomy — 27 ambiguity benchmark cases

This analysis is derived only from the committed 100-company benchmark artifacts.
Andrej's website and comment fields are copied exactly into the CSV and table. No
web lookup, provider call, AI call, or Discovery rerun was performed.

## Selection and evidence limit

The cohort contains exactly 27 rows: 13 `OFFICIAL_EXISTS_BUT_SEARCH_MISSED`
rows plus 14 complex, wrong-entity, or unresolved rows. The compact benchmark
contains candidate domains and diagnostic summaries, but not full search-result
titles/snippets or evidence IDs. Consequently, absence from a complete candidate
preview proves a candidate-collection failure but does **not** prove whether the
cause was query retrieval or result extraction. Rows 26 and 27 explicitly require
a stored-result extraction replay first; other brand-mismatch rows are marked as
likely new-query needs, not proven raw-result misses.

## Exact aggregate

- SEARCH family: **12** (10 query/brand recovery + 2 candidate-extraction cases)
- RESOLVER / ATTRIBUTION: **5**
- ENTITY RELATIONSHIP: **7**
- GENUINELY INSUFFICIENT EVIDENCE: **3**
- Deterministic P0/P1 outcome reasonably achievable: **13**
- Selective AI adds material value after P0/P1: **14**
- Likely new/better query required: **12**
- Stored candidate preview already contains the decisive or relationship domain: **7**

The seven stored-candidate opportunities are FF GROUP, C.I.A.K., BAIMS, YUZER,
VERENstan, SUPER STRELA, and G + S. Five support deterministic select/reject/UNKNOWN
behavior; YUZER and G + S still need relationship interpretation, but no new domain
invention. Twelve cases likely require a new query. PRO ACTIV and ENERKO BIRO first
require a replay of stored results to distinguish extraction from retrieval. Hiša
Cvet requires the complete stored evidence set before either conclusion.

## Primary failure modes

- BRAND_LEGAL_NAME_MISMATCH: 10
- CROSS_COUNTRY_COLLISION: 2
- DOMAIN_EXISTS_NO_CONTENT: 2
- INSUFFICIENT_EVIDENCE: 1
- MARKETPLACE_OR_PROFILE_ONLY: 1
- MULTI_DOMAIN: 2
- PARENT_GROUP_RELATIONSHIP: 4
- REBRAND_OR_SUCCESSOR_ENTITY: 1
- RETRIEVAL_VS_EXTRACTION_UNDETERMINED: 2
- WRONG_ENTITY: 2

## Brand/trading-name recovery cases

The legal name is insufficient for SPV / SP Velenje, ŠTRK–GRMIČ / EU Sredstva,
BENZ / Restavracija Bolero, GEMINI MS / Vila Prešeren, Otroška igralnica Malina /
Vrtec Malina, KIK-IN / KIK Gradnje, SCHOOL SERVICE / SBUS, BSL / Parkirna Hiša,
ENERGA / Energa Sistemi, and MARS MUSIC / Dallas. PRO ACTIV and ENERKO BIRO add
tokenization/generic-suffix extraction cases. These require identity-bounded brand
discovery, not unconstrained fuzzy domain guessing.

## Parent/group/rebrand cases

- Bartec Central Services: possible parent email domain, explicitly not the local site.
- Outbrain: rebrand/successor relationship to Teads.
- YUZER: possible Austrian principal domain.
- YASKAWA Europe Robotics: international corporate/subsidiary attribution.
- G + S: foreign owners' corporate domain, not the Slovenian entity.
- DELTA TRANSPORTNI SISTEM and VERENstan: foreign-country sites without sufficient
  proof that they are the Slovenian entity's official website.

These cases need explicit relationship assertions. Parent, owner, principal, brand,
successor, and foreign office are not synonyms for `OFFICIAL_WEBSITE`.

## Cases that should remain UNKNOWN

DELTA TRANSPORTNI SISTEM, FF GROUP, GRADBENIŠTVO LORENČIČ, C.I.A.K., ELEKTRO
BRATINA, VERENstan, and any parent/group case without entity-specific evidence
should remain UNKNOWN rather than forcing a website. Hiša Cvet also remains
unresolved until the complete stored evidence is inspected.

## Company-level taxonomy

| ID | Company | Website (exact) | Comment (exact) | Original diagnostic | Candidate preview | Primary / secondary | Owner | Deterministic? | AI value? | Preventive capability |
|---:|---|---|---|---|---|---|---|---|---|---|
| 15812 | SPV, STANOVANJSKO PODJETJE VELEN... | https://www.sp-velenje.si |  | NO_MEANINGFUL_IDENTITY_SUPPORT | NO | BRAND_LEGAL_NAME_MISMATCH / QUERY_VS_EXTRACTION_UNDETERMINED | SEARCH | YES_DETERMINISTIC_AFTER_P1 | NO | Acronym/trading-name query generation plus exact identifier/address corroboration. |
| 16690 | ŠTRK - GRMIČ d.o.o. | https://www.eusredstva.si | težko najti | NO_MEANINGFUL_IDENTITY_SUPPORT | NO | BRAND_LEGAL_NAME_MISMATCH / QUERY_VS_EXTRACTION_UNDETERMINED | SEARCH | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Recover public project/trading names from structured records and snippets, then query them with the legal identity. |
| 11719 | BENZ d.o.o. | https://www.restavracija-bolero.si | težko najti | NO_MEANINGFUL_IDENTITY_SUPPORT | NO | BRAND_LEGAL_NAME_MISMATCH / GENERIC_LEGAL_NAME_AND_QUERY_VS_EXTRACTION_UNDETERMINED | SEARCH | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Trading-name/venue discovery for generic legal names, constrained by address, activity and identifiers. |
| 5869 | Bartec Central Services d.o.o. |  | ni slo www; emaili so na bartec.com ki je morda stran "mame", definitivno pa ne njihova | NO_MEANINGFUL_IDENTITY_SUPPORT | NO_CONFIRMED_OFFICIAL_DOMAIN | PARENT_GROUP_RELATIONSHIP / NO_LOCAL_OFFICIAL_WEBSITE | ENTITY_RELATIONSHIP | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Model parent/group email-domain relationships without promoting the group root as the local entity website. |
| 313 | GEMINI MS d.o.o. | vilapreseren.com | težko najti | NO_MEANINGFUL_IDENTITY_SUPPORT | NO | BRAND_LEGAL_NAME_MISMATCH / QUERY_VS_EXTRACTION_UNDETERMINED | SEARCH | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Trading-name discovery from address/activity/structured sources before domain search. |
| 15908 | OTROŠKA IGRALNICA MALINA, d.o.o. | https://www.vrtecmalina.si |  | NO_MEANINGFUL_IDENTITY_SUPPORT | NO | BRAND_LEGAL_NAME_MISMATCH / TRADING_NAME_DISCOVERY | SEARCH | YES_DETERMINISTIC_AFTER_P1 | NO | Use registered activity, address and persisted snippets to discover and query the Vrtec Malina trading name. |
| 3845 | Outbrain d.o.o. |  | poznam firmo, vmes so naredili rebranding, so del korporacije Teads, teads.com; nisem mogel tega najti | NO_MEANINGFUL_IDENTITY_SUPPORT | NO_CONFIRMED_OFFICIAL_DOMAIN | REBRAND_OR_SUCCESSOR_ENTITY / NO_SUCCESSOR_CANDIDATE | ENTITY_RELATIONSHIP | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Time-versioned predecessor/successor and rebrand relationship assertions with explicit effective dates. |
| 15219 | PRO ACTIV, d.o.o., Ljubljana | https://pro-activ.si | lahko najti; spletna stran se je dlje časa nalagala | NO_MEANINGFUL_IDENTITY_SUPPORT | NO | RETRIEVAL_VS_EXTRACTION_UNDETERMINED / DOMAIN_TOKENIZATION_VARIANT | CANDIDATE_EXTRACTION | YES_DETERMINISTIC_AFTER_P1 | NO | Replay stored results for URL extraction, then add bounded hyphen/token variants before issuing another query. |
| 13803 | ENERKO BIRO, d.o.o. | https://www.enerko.si/kontakt.html | lahko najti | NO_MEANINGFUL_IDENTITY_SUPPORT | NO | RETRIEVAL_VS_EXTRACTION_UNDETERMINED / LEGAL_NAME_SUFFIX_OVERFIT | CANDIDATE_EXTRACTION | YES_DETERMINISTIC_AFTER_P1 | NO | Replay stored result URLs/snippets and query the distinctive legal-name stem without the generic BIRO suffix. |
| 16991 | KIK-IN d.o.o. | https://kik-gradnje.com | lahko najti | NO_MEANINGFUL_IDENTITY_SUPPORT | NO | BRAND_LEGAL_NAME_MISMATCH / QUERY_VS_EXTRACTION_UNDETERMINED | SEARCH | YES_DETERMINISTIC_AFTER_P1 | NO | Generate activity/trading-name combinations such as KIK + gradnje, bounded by exact company identity. |
| 9322 | DELTA TRANSPORTNI SISTEM – DTS d... |  | slovenske strani ni - samo srbska (dts.rs) | NO_MEANINGFUL_IDENTITY_SUPPORT | NO_CONFIRMED_OFFICIAL_DOMAIN | CROSS_COUNTRY_COLLISION / NO_LOCAL_OFFICIAL_WEBSITE | ENTITY_RELATIONSHIP | YES_DETERMINISTIC_UNKNOWN | NO | Country/entity gating: a foreign same-name site cannot become official without an explicit legal relationship. |
| 763 | FF GROUP d.o.o. |  | naj bi bila ffgroup.si vendar nima vsebine | THIRD_PARTY_NOISE_DOMINATES | NO_CONFIRMED_OFFICIAL_DOMAIN_RELATED_OR_NO_CONTENT_CANDIDATE_PRESENT | DOMAIN_EXISTS_NO_CONTENT / INSUFFICIENT_OWNERSHIP_CONTENT | INSUFFICIENT_EVIDENCE | YES_DETERMINISTIC_UNKNOWN | NO | Treat an empty/nonfunctional candidate as UNKNOWN unless an exact structured identity bridge proves ownership. |
| 10224 | SCHOOL SERVICE d.o.o., Ljubljana | https://www.sbus.si/o-nas | težko najti | THIRD_PARTY_NOISE_DOMINATES | NO | BRAND_LEGAL_NAME_MISMATCH / QUERY_VS_EXTRACTION_UNDETERMINED | SEARCH | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Discover public-facing abbreviations/brands such as SBUS from structured or persisted snippet evidence. |
| 13974 | GRADBENIŠTVO LORENČIČ d.o.o. |  | nisem našel www; podobnost z "gradbeni servis lorenčič d.o.o.", vendar to ni ista firma | THIRD_PARTY_NOISE_DOMINATES | NO_CONFIRMED_OFFICIAL_DOMAIN | WRONG_ENTITY / SIMILAR_LEGAL_ENTITY | RESOLVER_ATTRIBUTION | YES_DETERMINISTIC_UNKNOWN | NO | Require exact tax/registration or equivalent address identity before accepting a same/similar-name entity. |
| 2986 | BSL d.o.o. | https://parkirnahisa.si | težko najti | ONE_MODERATE_LEADER | NO | BRAND_LEGAL_NAME_MISMATCH / QUERY_VS_EXTRACTION_UNDETERMINED | SEARCH | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Trading-name/location/activity query expansion for generic short legal names. |
| 14174 | ENERGA, d.o.o. | https://energasistemi.si | težko najti | ONE_MODERATE_LEADER | NO | BRAND_LEGAL_NAME_MISMATCH / QUERY_VS_EXTRACTION_UNDETERMINED | SEARCH | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Query validated brand/service-name expansions and bind them back to legal identity. |
| 11630 | C.I.A.K. d.o.o. |  | ciak.si obstaja ampak je brez vsebine. Ciak-avto.si obstaja ampak je povezan s firmo "c.i.a.k. Avto d.o.o." kar ni prava, je pa zelo podobna. Komplicirani. | ONE_MODERATE_LEADER | NO_CONFIRMED_OFFICIAL_DOMAIN_RELATED_OR_NO_CONTENT_CANDIDATE_PRESENT | WRONG_ENTITY / DOMAIN_EXISTS_NO_CONTENT | RESOLVER_ATTRIBUTION | YES_DETERMINISTIC_UNKNOWN | NO | Identifier-bound entity disambiguation plus no-content handling; never transfer evidence from C.I.A.K. Avto. |
| 10158 | BAIMS d.o.o. | https://notranje-pohistvo.com/ IN http://pohistvo-baims.si | dve spletni strani, ista firma, nenavadno in konfuzno. | ONE_MODERATE_LEADER | YES | MULTI_DOMAIN / CANDIDATE_PRESENT_NOT_SELECTED | RESOLVER_ATTRIBUTION | YES_DETERMINISTIC | NO | Represent multiple validated official domains, retain alternatives, and select a canonical site by explicit policy. |
| 8494 | YUZER d.o.o. |  | nisem našel www;ly-holding.com je pravilno najdena stran iz email naslova ampak se gre za neko avstrijsko stran, morda principal | ONE_MODERATE_LEADER | NO_CONFIRMED_OFFICIAL_DOMAIN_RELATED_OR_NO_CONTENT_CANDIDATE_PRESENT | PARENT_GROUP_RELATIONSHIP / FOREIGN_PRINCIPAL | ENTITY_RELATIONSHIP | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Represent PRINCIPAL/PARENT relationships separately from OFFICIAL_WEBSITE and prohibit implicit promotion. |
| 11788 | MARS MUSIC d.o.o. | dallas.si | težko najti | ONE_CLEAR_EVIDENCE_LEADER | NO | BRAND_LEGAL_NAME_MISMATCH / QUERY_VS_EXTRACTION_UNDETERMINED | SEARCH | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Music-label/trading-name discovery bound to legal identity and address. |
| 11085 | ELEKTRO BRATINA d.o.o. | elektro-bratina.si | stran brez vsebine | ONE_CLEAR_EVIDENCE_LEADER | NO | DOMAIN_EXISTS_NO_CONTENT / RETRIEVAL_VS_EXTRACTION_UNDETERMINED | INSUFFICIENT_EVIDENCE | YES_DETERMINISTIC_UNKNOWN | NO | No-content candidates remain UNKNOWN; use an exact structured identity bridge instead of semantic inference or AI. |
| 18010 | Hiša Cvet d.o.o. | prcvetu.com | praktično nemogoče najti brez ai | ONE_CLEAR_EVIDENCE_LEADER | UNKNOWN_TRUNCATED | INSUFFICIENT_EVIDENCE / TRUNCATED_CANDIDATE_PREVIEW | INSUFFICIENT_EVIDENCE | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Inspect the complete stored candidate/evidence set before search; AI may choose only among evidenced domains. |
| 2924 | YASKAWA Europe Robotics d.o.o. | yaskava.si | konfuzno ker so korporacija, mednarodna. Zelo težko najti pravi rezultat. | ONE_CLEAR_EVIDENCE_LEADER | NO_EXACT_HUMAN_VALUE_NEAR_MATCH_PRESENT | PARENT_GROUP_RELATIONSHIP / INTERNATIONAL_GROUP_ATTRIBUTION_AND_HUMAN_SPELLING_CONFLICT | ENTITY_RELATIONSHIP | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Entity-specific international group/subsidiary modeling plus explicit handling of human-value/domain spelling conflicts. |
| 5336 | BURGER TRANSPORT d.o.o. | burgerdoo.si | več domen povezanih s to firmo, konfuzno | MANY_LOW_SIGNAL_CANDIDATES | NO | MULTI_DOMAIN / CORRECT_DOMAIN_ABSENT_FROM_PREVIEW | RESOLVER_ATTRIBUTION | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Collect and cluster all company-related domains, then classify official, alternate, group and unrelated scopes. |
| 14996 | VERENstan d.o.o. | https://www.verenstan.ba | konfuzno, bosanska stran | CROSS_COUNTRY_COLLISION | RELATED_DOMAIN_PRESENT_NOT_OFFICIAL | CROSS_COUNTRY_COLLISION / FOREIGN_DOMAIN_NOT_LOCAL_ENTITY | ENTITY_RELATIONSHIP | YES_DETERMINISTIC_UNKNOWN | NO | Country/entity gates must keep a Bosnian domain as related/foreign unless exact Slovenian-entity evidence exists. |
| 16303 | SUPER STRELA d.o.o. | superstrela.com | bolha je spletna tržnica, kjer pa imajo trgovine lahko svojo predstavitev | SAME_DOMAIN_VARIANTS | YES | MARKETPLACE_OR_PROFILE_ONLY / CANDIDATE_PRESENT_NOT_SELECTED_AND_SAME_DOMAIN_VARIANTS | RESOLVER_ATTRIBUTION | YES_DETERMINISTIC | NO | Hard-exclude marketplace/profile operators as official; group same registrable-domain variants and prefer verified root scope. |
| 2187 | G + S d.o.o. | diepolstermacher.com | konfuzno - to je korpo stran, ne od slovenske firme temveč njihovih lastnikov | CROSS_COUNTRY_COLLISION | RELATED_DOMAIN_PRESENT_NOT_OFFICIAL | PARENT_GROUP_RELATIONSHIP / FOREIGN_PARENT_OWNER_DOMAIN | ENTITY_RELATIONSHIP | NO_SELECTIVE_AI_AFTER_P1 | YES_AFTER_P0_P1 | Model owner/parent domains explicitly and forbid their promotion to the Slovenian subsidiary without entity evidence. |

## Interpretation discipline

`YES_DETERMINISTIC_UNKNOWN` is a successful precision-first outcome: the resolver
can reject unsafe attribution and persist UNKNOWN. `YES_AFTER_P0_P1` for AI means
AI is allowed only after deterministic and search/brand recovery stages fail. The
AI may classify only supplied candidates and relationships; it may not invent a
domain.
