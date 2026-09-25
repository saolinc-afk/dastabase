"""Central Phase 2 domain policy for publisher/ownership decisions.

Entries name known *operators*, never words in a company name.  A policy is
therefore safe for a manufacturer's own dealer or product site; the structural
ownership verifier still decides unknown and shared-host cases.
"""
from dataclasses import dataclass
from discovery.domain_generator import normalize_domain, normalize_url

ALLOW = 'ALLOW'
BLOCK_AS_OFFICIAL = 'BLOCK_AS_OFFICIAL'
GROUP_REVIEW = 'GROUP_REVIEW'
REQUIRE_SCOPED_OWNERSHIP = 'REQUIRE_SCOPED_OWNERSHIP'

FIRST_PARTY_ALLOWED = 'FIRST_PARTY_ALLOWED'
DIRECTORY = 'DIRECTORY'
COMPANY_DATABASE = 'COMPANY_DATABASE'
MARKETPLACE = 'MARKETPLACE'
JOB_PORTAL = 'JOB_PORTAL'
BOOKING_AGGREGATOR = 'BOOKING_AGGREGATOR'
TOURISM_PROFILE = 'TOURISM_PROFILE'
SOCIAL_PLATFORM = 'SOCIAL_PLATFORM'
MEDIA_NEWS = 'MEDIA_NEWS'
DEALER_PORTAL = 'DEALER_PORTAL'
GROUP_PARENT = 'GROUP_PARENT'
OTHER_THIRD_PARTY = 'OTHER_THIRD_PARTY'


@dataclass(frozen=True)
class DomainPolicy:
    classification: str
    policy: str


def _entries(domains, classification, policy):
    return {domain: DomainPolicy(classification, policy) for domain in domains}


# This is intentionally a small, explicit registry of hosts already rejected
# by Phase 2 plus audited third-party operators.  It is not a growing list of
# ordinary company domains.
REGISTRY = {}
REGISTRY.update(_entries({
    'sloexport.si', 'imenik-podjetij.com', 'itis.si', 'itis.siol.net',
    'informiran.si', 'firmas.si', 'moja-dejavnost.si', 'mojastoritev.si',
    'cylex.si', 'cybo.com', 'topograph.co', 'moje-podjetje.net',
    'findglocal.com', 'starofservice.si', 'mapcarta.com', 'mapquest.com', 'mojmojster.net',
    'yelp.com', 'najdi.si', 'acompio.si',
}, DIRECTORY, BLOCK_AS_OFFICIAL))
REGISTRY.update(_entries({
    'ajpes.si', 'bizi.si', 'companywall.si', 'companywall.com', 'pirs.si', 'gvin.com',
    'dnb.com', 'dnb.si', 'bloomberg.com', 'zoominfo.com', 'crunchbase.com',
    'kompass.com', 'europages.com', 'europages.si', 'ebonitete.si',
    'infobel.com', 'infobel.si', 'optius.com', 'panjiva.com',
}, COMPANY_DATABASE, BLOCK_AS_OFFICIAL))
REGISTRY.update(_entries({
    'mascus.com', 'mascus.co.uk', 'mascus.si', 'mascus.at', 'mascus.com.au',
    'mascus.be', 'mascus.bg', 'mascus.com.br', 'mascus.cz', 'mascus.de',
    'mascus.dk', 'mascus.ee', 'mascus.es', 'mascus.fi', 'mascus.fr',
    'mascus.gr', 'mascus.hr', 'mascus.hu', 'mascus.ie', 'mascus.it',
    'mascus.jp', 'mascus.co.kr', 'mascus.lt', 'mascus.lu', 'mascus.lv',
    'mascus.me', 'mascus.nl', 'mascus.no', 'mascus.co.nz', 'mascus.pl',
    'mascus.pt', 'mascus.ro', 'mascus.rs', 'mascus.se', 'mascus.sk',
    'mascus.com.tr', 'mascus.com.tw', 'mascus.ua', 'mascus.com.ua',
    'mascus.co.za', 'cnmasike.com', 'machineryline.info', 'machinio.com',
    'machinerytrader.com', 'doberavto.si', 'onlinecomponents.com',
}, MARKETPLACE, BLOCK_AS_OFFICIAL))
REGISTRY.update(_entries({'techpilot.com'}, DEALER_PORTAL, BLOCK_AS_OFFICIAL))
REGISTRY.update(_entries({'trivago.com'}, BOOKING_AGGREGATOR, BLOCK_AS_OFFICIAL))
REGISTRY.update(_entries({'bergfex.com', 'evendo.com'}, TOURISM_PROFILE, BLOCK_AS_OFFICIAL))
REGISTRY.update(_entries({
    'facebook.com', 'instagram.com', 'linkedin.com', 'twitter.com', 'x.com',
    'tiktok.com', 'youtube.com',
}, SOCIAL_PLATFORM, BLOCK_AS_OFFICIAL))
REGISTRY.update(_entries({'svet24.si', '1001ideja.si'}, MEDIA_NEWS, BLOCK_AS_OFFICIAL))
REGISTRY.update(_entries({
    'google.com', 'google.si', 'wikipedia.org', 'business.site',
    'konzum.hr', 'mojaobcina.si', 'urejam.si', 'avruparuyasi.com.tr',
    'licenseplatesdirect.com', 'mini.si',
}, OTHER_THIRD_PARTY, BLOCK_AS_OFFICIAL))
# Group domains remain candidates for scoped relationship review, never a
# third-party blacklist.  Their pages may prove entity-specific operation.
REGISTRY.update(_entries({'veto.si', 'oqema.com'}, GROUP_PARENT, GROUP_REVIEW))


def policy_for_url(url):
    """Return the most-specific registered operator policy, if any."""
    if not normalize_url(url):
        return DomainPolicy(OTHER_THIRD_PARTY, BLOCK_AS_OFFICIAL)
    host = normalize_domain(url)
    matches = [domain for domain in REGISTRY if host == domain or host.endswith('.' + domain)]
    return REGISTRY[max(matches, key=len)] if matches else None


def blocks_official(url):
    policy = policy_for_url(url)
    return bool(policy and policy.policy == BLOCK_AS_OFFICIAL)


def is_group_parent(url):
    policy = policy_for_url(url)
    return bool(policy and policy.policy == GROUP_REVIEW)
