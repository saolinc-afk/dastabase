"""
company_matcher.py

Matches a company from an input source (Excel / Google Sheets)
against candidate companies returned by GVIN/AJPES/etc.

The matcher deliberately does NOT access GVIN itself.
It only scores candidates. This keeps matching separate from
data collection and makes it easy to test safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
import unicodedata
from typing import Any, Iterable


# Common Slovenian / regional legal-form suffixes.
LEGAL_FORMS = {
    "doo",
    "dd",
    "sp",
    "jdoo",
    "dno",
    "kd",
    "kdd",
    "dooel",
    "sro",
    "ag",
    "se",
    "inc",
    "ltd",
    "llc",
}

STOPWORDS = {
    "druzba",
    "druzba za",
    "podjetje",
}


@dataclass
class CompanyInput:
    """Company information from our source list."""

    name: str = ""
    email: str = ""
    phone: str = ""
    person_name: str = ""
    registration_number: str = ""
    tax_number: str = ""

    @property
    def email_domain(self) -> str:
        return extract_email_domain(self.email)


@dataclass
class CompanyCandidate:
    """A company returned by GVIN/AJPES/etc."""

    name: str = ""
    domain: str = ""
    email_domain: str = ""
    phone: str = ""
    registration_number: str = ""
    tax_number: str = ""
    address: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MatchResult:
    """Scored candidate."""

    candidate: CompanyCandidate
    score: float
    confidence: str
    reasons: list[str] = field(default_factory=list)


def _ascii(text: str) -> str:
    """Lowercase and remove accents."""
    text = str(text or "").strip().lower()
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )


def normalize_company_name(name: str) -> str:
    """
    Normalize a company name for identity comparison.

    Slovenian legal forms such as "d.o.o." are converted BEFORE
    punctuation is removed, so they do not distort the comparison.
    """
    value = _ascii(name)

    # Normalize common Slovenian legal forms before stripping punctuation.
    value = re.sub(r"\bd\s*[.\-]?\s*o\s*[.\-]?\s*o\b", " doo ", value)
    value = re.sub(r"\bd\s*[.\-]?\s*d\b", " dd ", value)
    value = re.sub(
        r"\bj\s*[.\-]?\s*d\s*[.\-]?\s*o\s*[.\-]?\s*o\b",
        " jdoo ",
        value,
    )

    # Remove punctuation/separators.
    value = re.sub(r"[^a-z0-9]+", " ", value)
    tokens = value.split()

    # Remove common legal-form tokens.
    tokens = [token for token in tokens if token not in LEGAL_FORMS]

    # Remove legal wording that is not useful for identity matching.
    legal_words = {"druzba", "omejeno", "odgovornostjo", "z"}
    tokens = [token for token in tokens if token not in legal_words]

    return " ".join(tokens).strip()


def normalize_domain(domain: str) -> str:
    """Normalize a domain/URL to its hostname."""
    value = str(domain or "").strip().lower()

    if "@" in value:
        value = value.split("@", 1)[1]

    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    value = value.split("/", 1)[0]
    value = value.split(":", 1)[0]

    return value.strip().rstrip(".")


def extract_email_domain(email: str) -> str:
    """Extract and normalize the domain from an email address."""
    email = str(email or "").strip().lower()

    if "@" not in email:
        return ""

    return normalize_domain(email.rsplit("@", 1)[1])


def normalize_phone(phone: str) -> str:
    """Normalize Slovenian phone numbers to 386XXXXXXXXX."""
    digits = re.sub(r"\D", "", str(phone or ""))

    if not digits:
        return ""

    if digits.startswith("00386"):
        digits = digits[2:]
    elif digits.startswith("386"):
        pass
    elif len(digits) == 9 and digits.startswith("0"):
        digits = "386" + digits[1:]
    elif len(digits) == 8:
        digits = "386" + digits

    return digits


def phone_match(input_phone: str, candidate_phone: str) -> bool:
    """
    Compare phone numbers reasonably without assuming a country format.

    Exact digit match is strongest. As a fallback, compare the last
    8 digits when both numbers are sufficiently long.
    """
    a = normalize_phone(input_phone)
    b = normalize_phone(candidate_phone)

    if not a or not b:
        return False

    if a == b:
        return True

    if len(a) >= 8 and len(b) >= 8 and a[-8:] == b[-8:]:
        return True

    return False


def name_similarity(name_a: str, name_b: str) -> float:
    """Return a 0..100 fuzzy similarity score."""
    a = normalize_company_name(name_a)
    b = normalize_company_name(name_b)

    if not a or not b:
        return 0.0

    if a == b:
        return 100.0

    # Character similarity.
    ratio = SequenceMatcher(None, a, b).ratio() * 100

    # Token similarity helps with word-order differences.
    a_tokens = set(a.split())
    b_tokens = set(b.split())

    if a_tokens and b_tokens:
        token_score = (
            len(a_tokens & b_tokens) / len(a_tokens | b_tokens)
        ) * 100
    else:
        token_score = 0.0

    # Favor token agreement slightly more than raw character similarity.
    return round((ratio * 0.55) + (token_score * 0.45), 2)


def _domain_match(input_domain: str, candidate: CompanyCandidate) -> bool:
    """Check the input email domain against candidate domains."""
    input_domain = normalize_domain(input_domain)

    if not input_domain:
        return False

    candidate_domains = {
        normalize_domain(candidate.domain),
        normalize_domain(candidate.email_domain),
    }

    return input_domain in {d for d in candidate_domains if d}


def score_candidate(
    company: CompanyInput,
    candidate: CompanyCandidate,
) -> MatchResult:
    """
    Score one candidate.

    Scoring philosophy:
      - registration/tax number: decisive when available
      - domain: very strong
      - phone: strong
      - company name: fuzzy supporting signal

    We intentionally don't use a single fuzzy name match as proof.
    """

    score = 0.0
    reasons: list[str] = []

    # Decisive identifiers, if our input ever contains them.
    input_registration = str(
        company.__dict__.get("registration_number", "")
    ).strip()
    input_tax = str(company.__dict__.get("tax_number", "")).strip()

    if (
        input_registration
        and candidate.registration_number
        and input_registration == candidate.registration_number
    ):
        score += 100
        reasons.append("registration_number_exact")

    if (
        input_tax
        and candidate.tax_number
        and input_tax == candidate.tax_number
    ):
        score += 100
        reasons.append("tax_number_exact")

    # Email domain / website domain.
    if _domain_match(company.email_domain, candidate):
        score += 80
        reasons.append("email_domain_match")

    # Phone.
    if phone_match(company.phone, candidate.phone):
        score += 60
        reasons.append("phone_match")

    # Company name.
    similarity = name_similarity(company.name, candidate.name)

    if similarity >= 95:
        score += 55
        reasons.append(f"name_exact_or_near_exact:{similarity:.0f}")
    elif similarity >= 85:
        score += 45
        reasons.append(f"name_strong:{similarity:.0f}")
    elif similarity >= 70:
        score += 30
        reasons.append(f"name_good:{similarity:.0f}")
    elif similarity >= 55:
        score += 15
        reasons.append(f"name_weak:{similarity:.0f}")

    # Cap the normal score at 100 for easier interpretation.
    # Exact registration/tax + other signals still becomes 100%.
    final_score = min(score, 100.0)

    if final_score >= 90:
        confidence = "HIGH"
    elif final_score >= 70:
        confidence = "MEDIUM"
    elif final_score >= 50:
        confidence = "LOW"
    else:
        confidence = "REJECT"

    return MatchResult(
        candidate=candidate,
        score=round(final_score, 2),
        confidence=confidence,
        reasons=reasons,
    )


def match_company(
    company: CompanyInput,
    candidates: Iterable[CompanyCandidate],
    limit: int = 5,
) -> list[MatchResult]:
    """Score and rank candidates, highest score first."""
    results = [
        score_candidate(company, candidate)
        for candidate in candidates
    ]

    results.sort(key=lambda result: result.score, reverse=True)

    return results[:limit]


def best_match(
    company: CompanyInput,
    candidates: Iterable[CompanyCandidate],
) -> MatchResult | None:
    """Return the highest-scoring candidate, or None."""
    results = match_company(company, candidates, limit=1)
    return results[0] if results else None


if __name__ == "__main__":
    # Small self-test so the module can be run directly.
    company = CompanyInput(
        name="Kompas Xnet",
        email="branka@kompas-xnet.si",
        phone="041 745 785",
        person_name="Branka",
    )

    candidates = [
        CompanyCandidate(
            name="KOMPAS XNET, d.o.o.",
            domain="https://www.kompas-xnet.si",
            phone="041745785",
        ),
        CompanyCandidate(
            name="KOMPAS, d.d.",
            domain="https://www.kompas.si",
            phone="01 123 45 67",
        ),
        CompanyCandidate(
            name="XNET SISTEMI, d.o.o.",
            domain="https://xnet.si",
            phone="041 111 222",
        ),
    ]

    print(f"\nInput: {company.name}")
    print(f"Email domain: {company.email_domain}\n")

    for result in match_company(company, candidates):
        print(
            f"{result.score:6.1f}  {result.confidence:7s}  "
            f"{result.candidate.name}"
        )
        if result.reasons:
            print(f"        {', '.join(result.reasons)}")
