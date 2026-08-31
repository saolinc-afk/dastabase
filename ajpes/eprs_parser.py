from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from ingestion.models import (
    CompanyRecord,
    FactRecord,
    OwnershipRecord,
    PersonRecord,
    PersonRoleRecord,
)


SOURCE = "ajpes_eprs"
BASE_URL = "https://www.ajpes.si/prs/"
LOGIN_REQUIRED_MARKERS = (
    "Za prikaz podatkov je obvezna prijava.",
    "PRIJAVA V SISTEM",
)


@dataclass(frozen=True)
class SearchCandidate:
    name: str | None
    registration_number: str | None
    tax_number: str | None
    detail_url: str | None


@dataclass(frozen=True)
class SearchResult:
    status: str
    candidates: list[SearchCandidate] = field(default_factory=list)
    message: str | None = None


@dataclass(frozen=True)
class AjpesEprsParseResult:
    company_record: CompanyRecord
    raw: dict[str, Any]


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def digits_only(value: Any) -> str | None:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits or None


def parse_tax_number(value: Any) -> str | None:
    text = normalize_space(value)
    text = re.sub(r"^SI\s*", "", text, flags=re.IGNORECASE)
    return digits_only(text)


def parse_date(value: Any) -> str | None:
    text = normalize_space(value)
    match = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", text)
    if not match:
        return None

    day, month, year = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def parse_percent(value: Any) -> float | None:
    text = normalize_space(value).replace(",", ".")
    match = re.match(r"^(\d+(?:\.\d+)?)\s*%$", text)
    return float(match.group(1)) if match else None


def extract_entity_id(url: str | None) -> str | None:
    if not url:
        return None

    values = parse_qs(urlparse(url).query).get("e")
    return values[0] if values else None


def soup_from_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def page_requires_login(html: str) -> bool:
    text = soup_from_html(html).get_text(" ", strip=True)
    return any(marker in text for marker in LOGIN_REQUIRED_MARKERS)


def parse_search_results(html: str, *, base_url: str = BASE_URL) -> SearchResult:
    soup = soup_from_html(html)
    text = soup.get_text(" ", strip=True)

    if any(marker in text for marker in LOGIN_REQUIRED_MARKERS):
        return SearchResult(status="LOGIN_REQUIRED", message="AJPES login required")

    if "Ni zadetkov" in text:
        return SearchResult(status="NO_RESULT", message="AJPES returned no result")

    candidates: list[SearchCandidate] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        headers = [normalize_space(cell.get_text(" ")) for cell in rows[0].find_all(["th", "td"])]
        if "Matična številka" not in headers:
            continue

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < len(headers):
                continue

            values = {
                header: normalize_space(cell.get_text(" "))
                for header, cell in zip(headers, cells)
            }
            link = row.find("a", href=re.compile(r"podjetje\.asp"))
            candidates.append(
                SearchCandidate(
                    name=values.get("Firma") or values.get("Skrajšana firma"),
                    registration_number=digits_only(values.get("Matična številka")),
                    tax_number=parse_tax_number(values.get("Davčna številka")),
                    detail_url=urljoin(base_url, link["href"]) if link else None,
                )
            )

    if not candidates:
        return SearchResult(status="NO_RESULT", message="No parseable AJPES candidates")

    return SearchResult(status="OK", candidates=candidates)


def field_pairs(root) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for row in root.select(".row"):
        label = row.select_one(".text-thin")
        if label is None:
            continue

        value = label.find_next_sibling()
        label_text = normalize_space(label.get_text(" "))
        value_text = normalize_space(value.get_text(" ")) if value else ""
        if label_text:
            pairs[label_text] = value_text
    return pairs


def section_by_id(soup: BeautifulSoup, section_id: str):
    return soup.select_one(f"#{section_id}") or BeautifulSoup("", "html.parser")


def relevant_links(root, *, base_url: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for link in root.find_all("a"):
        href = link.get("href")
        text = normalize_space(link.get_text(" "))
        if not href:
            continue

        href_lower = href.lower()
        if (
            "rezultati_osebe.asp" in href_lower
            or "podjetje_zgodovina.asp" in href_lower
            or "jolp/podjetje" in href_lower
            or ("podjetje.asp" in href_lower and text.startswith("VPOGLED V "))
        ):
            links.append({"text": text, "href": urljoin(base_url, href)})
    return links


def parse_people(soup: BeautifulSoup, *, base_url: str) -> list[PersonRecord]:
    people: list[PersonRecord] = []
    sections = (
        ("accordianBody3", "REPRESENTATIVE"),
        ("accordianBody4", "SUPERVISORY_BOARD"),
    )

    for section_id, category in sections:
        section = section_by_id(soup, section_id)
        for block in section.select(".matchHeight"):
            fields = field_pairs(block)
            name = (
                fields.get("Osebno ime")
                or fields.get("osebno ime")
                or fields.get("Ime in priimek")
            )
            role_title = (
                fields.get("Vrsta zastopnika")
                or fields.get("tip člana")
                or fields.get("Tip člana")
            )
            source_role_id = (
                fields.get("Zap. št. zastopnika")
                or fields.get("zap. št. člana")
                or fields.get("Zap. št. člana")
            )
            if not name or not role_title:
                continue

            address = fields.get("Naslov")
            safe_fields = dict(fields)
            if address:
                safe_fields["Naslov"] = "[REDACTED]"

            link = block.find("a", string=re.compile(r"POVEZANE OSEBE"))
            linked_href = urljoin(base_url, link["href"]) if link and link.get("href") else None
            people.append(
                PersonRecord(
                    full_name=normalize_space(name),
                    roles=[
                        PersonRoleRecord(
                            role_type=category,
                            title=normalize_space(role_title),
                            valid_from=parse_date(
                                fields.get("Datum podelitve pooblastila")
                                or fields.get("datum izvolitve ali imenovanja")
                            ),
                            valid_to=parse_date(fields.get("Datum prenehanja")),
                            source_role_id=source_role_id,
                            confidence=0.95,
                            evidence={
                                "source": SOURCE,
                                "section_id": section_id,
                                "linked_people_href": linked_href,
                                "raw_fields": safe_fields,
                                "personal_address_present": bool(address),
                            },
                        )
                    ],
                )
            )

    return people


def parse_ownership(soup: BeautifulSoup, *, base_url: str, detail_url: str) -> list[OwnershipRecord]:
    section = section_by_id(soup, "accordianBody2")
    owners: dict[str, dict[str, Any]] = {}
    shares: list[dict[str, str]] = []

    for column in section.select(".col-md-6"):
        heading = normalize_space(column.find("h4").get_text(" ") if column.find("h4") else "")
        for block in column.select("div[style*='page-break-inside']"):
            fields = field_pairs(block)
            if heading == "DRUŽBENIKI":
                shareholder_id = fields.get("Zap. št. družbenika")
                owner_type = "LEGAL_ENTITY" if fields.get("Firma") else "NATURAL_PERSON"
                raw_name = fields.get("Firma") or fields.get("Osebno ime") or fields.get("Ime in priimek")
                safe_fields = dict(fields)
                if safe_fields.get("Naslov") and owner_type == "NATURAL_PERSON":
                    safe_fields["Naslov"] = "[REDACTED]"

                owners[shareholder_id or raw_name or ""] = {
                    "owner_type": owner_type,
                    "owner_raw_name": normalize_space(raw_name),
                    "owner_raw_identifier": fields.get("Identifikacijska številka"),
                    "source_shareholder_id": shareholder_id,
                    "valid_from": parse_date(fields.get("Datum vstopa")),
                    "valid_to": parse_date(fields.get("Datum izstopa")),
                    "raw_fields": safe_fields,
                    "links": relevant_links(block, base_url=base_url),
                    "personal_address_present": bool(fields.get("Naslov"))
                    and owner_type == "NATURAL_PERSON",
                }
            elif heading == "POSLOVNI DELEŽI":
                shares.append(fields)

    ownership: list[OwnershipRecord] = []
    for fields in shares:
        holders = fields.get("Imetniki") or ""
        holder_ids = re.findall(r"Zap\.?\s*št\.?\s*družbenika\s*(\d+)", holders, re.IGNORECASE)
        if not holder_ids and len(owners) == 1:
            holder_ids = list(owners)

        for holder_id in holder_ids:
            owner = owners.get(holder_id)
            if owner is None:
                continue

            share_raw = fields.get("Delež v odstotku ali ulomku")
            ownership.append(
                OwnershipRecord(
                    source=SOURCE,
                    source_shareholder_id=owner.get("source_shareholder_id"),
                    source_share_id=fields.get("Zap. št. deleža"),
                    owner_raw_name=owner.get("owner_raw_name"),
                    owner_raw_identifier=owner.get("owner_raw_identifier"),
                    owner_type=owner.get("owner_type"),
                    share_raw=share_raw,
                    share_percent=parse_percent(share_raw),
                    nominal_value_raw=fields.get("Osnovni vložek"),
                    valid_from=owner.get("valid_from"),
                    valid_to=owner.get("valid_to"),
                    is_current=True,
                    evidence_url=detail_url,
                    evidence={
                        "source": SOURCE,
                        "section_id": "accordianBody2",
                        "owner_raw_fields": owner.get("raw_fields"),
                        "share_raw_fields": fields,
                        "owner_links": owner.get("links"),
                        "personal_address_present": owner.get("personal_address_present"),
                    },
                )
            )

    return ownership


def company_facts(fields: dict[str, str], *, detail_url: str) -> list[FactRecord]:
    facts: list[FactRecord] = []
    mapping = {
        "Pravnoorganizacijska oblika": "ajpes_eprs.legal_form",
        "Status subjekta": "ajpes_eprs.registry_status",
        "Osnovni kapital": "ajpes_eprs.share_capital",
        "Število delnic": "ajpes_eprs.share_count",
        "Datum vpisa subjekta v sodni register": "ajpes_eprs.court_register_entered_at",
    }
    for label, fact_key in mapping.items():
        value = fields.get(label)
        if value and value != "ni vpisa":
            facts.append(
                FactRecord(
                    fact_key=fact_key,
                    value=value,
                    confidence=0.95,
                    evidence={"source": SOURCE, "label": label, "url": detail_url},
                )
            )
    return facts


def parse_company_detail(
    html: str,
    *,
    detail_url: str,
    registration_number_hint: str | None = None,
    tax_number_hint: str | None = None,
) -> AjpesEprsParseResult:
    soup = soup_from_html(html)
    fields = field_pairs(section_by_id(soup, "accordianBody1"))
    entity_id = extract_entity_id(detail_url)

    registration_number = (
        digits_only(fields.get("Matična številka"))
        or digits_only(registration_number_hint)
    )
    tax_number = parse_tax_number(
        fields.get("Ident. št. za DDV in davčna številka")
        or tax_number_hint
    )

    record = CompanyRecord(
        source=SOURCE,
        external_id=entity_id,
        external_url=detail_url,
        canonical_name=fields.get("Firma") or fields.get("Skrajšana firma"),
        registration_number=registration_number,
        tax_number=tax_number,
        address=fields.get("Poslovni naslov"),
        observed_at=None,
        raw_data={
            "source": SOURCE,
            "detail_url": detail_url,
            "entity_id": entity_id,
            "company_fields": fields,
            "links": relevant_links(soup, base_url=detail_url),
        },
        facts=company_facts(fields, detail_url=detail_url),
        people=parse_people(soup, base_url=detail_url),
        ownership=parse_ownership(soup, base_url=detail_url, detail_url=detail_url),
    )
    return AjpesEprsParseResult(
        company_record=record,
        raw=record.raw_data or {},
    )
