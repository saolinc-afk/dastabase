from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


CDP_URL = "http://127.0.0.1:9222"
EPRS_URL = "https://www.ajpes.si/prs/"
SEARCH_URL = "https://www.ajpes.si/prs/default.asp"
DEDICATED_PAGE_MARKER = "dastabase-ajpes-eprs-probe"
LOGIN_REQUIRED_MARKERS = (
    "Za prikaz podatkov je obvezna prijava.",
    "PRIJAVA V SISTEM",
)


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    title: str
    body_text: str


def normalize_space(value: str | None) -> str:
    if not value:
        return ""

    return re.sub(r"\s+", " ", value).strip()


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def snapshot(page) -> PageSnapshot:
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=10_000)
    except Exception:
        pass

    return PageSnapshot(
        url=page.url,
        title=page.title(),
        body_text=body_text,
    )


def login_required(page) -> bool:
    snap = snapshot(page)
    return any(marker in snap.body_text for marker in LOGIN_REQUIRED_MARKERS)


def count_ajpes_pages(browser) -> int:
    count = 0
    for context in browser.contexts:
        for page in context.pages:
            if "ajpes.si" in page.url.lower():
                count += 1
    return count


def is_dedicated_eprs_page(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                marker => (
                    window.name === marker ||
                    window.sessionStorage.getItem("dastabase_page_marker") === marker
                )
                """,
                DEDICATED_PAGE_MARKER,
            )
        )
    except Exception:
        return False


def mark_dedicated_eprs_page(page) -> None:
    page.evaluate(
        """
        marker => {
            window.name = marker;
            window.sessionStorage.setItem("dastabase_page_marker", marker);
        }
        """,
        DEDICATED_PAGE_MARKER,
    )


def find_or_open_dedicated_eprs_page(browser):
    for context in browser.contexts:
        for page in context.pages:
            if is_dedicated_eprs_page(page):
                return page

    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    page.goto(EPRS_URL, wait_until="domcontentloaded", timeout=30_000)
    mark_dedicated_eprs_page(page)
    return page


def attach_page_diagnostics(payload: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, Any]:
    payload["page_diagnostics"] = diagnostics
    return payload


def extract_controls(page) -> dict[str, Any]:
    controls = page.locator("input, select, textarea, button").evaluate_all(
        """
        els => els.map(e => ({
            tag: e.tagName,
            type: e.getAttribute('type'),
            name: e.getAttribute('name'),
            id: e.id,
            value: e.getAttribute('value'),
            text: (e.innerText || '').trim(),
            placeholder: e.getAttribute('placeholder'),
            className: e.className
        }))
        """
    )
    return {"controls": controls}


def extract_result_links(page) -> list[dict[str, Any]]:
    links = page.locator("a").evaluate_all(
        """
        els => els.map((a, index) => ({
            index,
            text: (a.innerText || '').trim(),
            href: a.href,
            id: a.id,
            className: a.className,
            onclick: a.getAttribute('onclick')
        })).filter(x =>
            x.text ||
            (x.href && x.href.toLowerCase().includes('/prs/'))
        )
        """
    )
    return links


def relevant_eprs_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relevant: list[dict[str, Any]] = []
    for link in links:
        href = link.get("href", "")
        href_lower = href.lower()
        text = normalize_space(link.get("text"))
        if (
            "/prs/rezultati_osebe" in href_lower
            or "/jolp/podjetje" in href_lower
            or "/prs/podjetje_zgodovina" in href_lower
            or ("/prs/podjetje.asp" in href_lower and text.startswith("VPOGLED V "))
        ):
            relevant.append(link)
    return relevant


def extract_tables(page) -> list[dict[str, Any]]:
    return page.locator("table").evaluate_all(
        """
        els => els.map((table, index) => ({
            index,
            text: (table.innerText || '').trim(),
            html: table.outerHTML
        }))
        """
    )


def extract_field_pairs(root) -> dict[str, str]:
    rows = root.locator(".row").evaluate_all(
        """
        rows => rows.map(row => {
            const label = row.querySelector('.text-thin');
            if (!label) return null;
            const value = label.nextElementSibling;
            return {
                label: (label.innerText || '').trim(),
                value: value ? (value.innerText || '').trim() : ''
            };
        }).filter(Boolean)
        """
    )
    return {
        normalize_space(item["label"]): normalize_space(item["value"])
        for item in rows
        if normalize_space(item["label"])
    }


def redact_address_fields(fields: dict[str, str]) -> tuple[dict[str, str], bool]:
    redacted = dict(fields)
    address_present = bool(redacted.get("Naslov"))
    if address_present:
        redacted["Naslov"] = "[REDACTED]"
    return redacted, address_present


def is_legal_entity(fields: dict[str, str]) -> bool:
    return bool(fields.get("Firma"))


def extract_ownership_section(page) -> dict[str, Any]:
    section = page.locator("#accordianBody2")
    if section.count() == 0:
        return {"owners": [], "shares": [], "evidence": {"section_id": "accordianBody2"}}

    columns = section.locator(".col-md-6").evaluate_all(
        """
        columns => columns.map((column, columnIndex) => ({
            columnIndex,
            heading: (column.querySelector('h4')?.innerText || '').trim(),
            blocks: Array.from(column.querySelectorAll("div[style*='page-break-inside']")).map((block, blockIndex) => ({
                blockIndex,
                fields: Array.from(block.querySelectorAll('.row')).map(row => {
                    const label = row.querySelector('.text-thin');
                    if (!label) return null;
                    const value = label.nextElementSibling;
                    return {
                        label: (label.innerText || '').trim(),
                        value: value ? (value.innerText || '').trim() : ''
                    };
                }).filter(Boolean),
                links: Array.from(block.querySelectorAll('a')).map(a => ({
                    text: (a.innerText || '').trim(),
                    href: a.getAttribute('href')
                }))
            })),
            columnLinks: Array.from(column.querySelectorAll('a')).map(a => ({
                text: (a.innerText || '').trim(),
                href: a.getAttribute('href')
            }))
        }))
        """
    )

    owners: list[dict[str, Any]] = []
    shares: list[dict[str, Any]] = []

    for column in columns:
        heading = normalize_space(column.get("heading"))
        for block in column.get("blocks") or []:
            fields = {
                normalize_space(item["label"]): normalize_space(item["value"])
                for item in block.get("fields") or []
                if normalize_space(item.get("label"))
            }
            links = [
                link
                for link in (block.get("links") or column.get("columnLinks") or [])
                if normalize_space(link.get("text")) == "POVEZANE OSEBE"
            ]
            if heading == "DRUŽBENIKI":
                address_present = bool(fields.get("Naslov"))
                safe_fields = dict(fields)
                if address_present and not is_legal_entity(fields):
                    safe_fields["Naslov"] = "[REDACTED]"
                owners.append(
                    {
                        "owner_type": "LEGAL_ENTITY" if is_legal_entity(fields) else "NATURAL_PERSON",
                        "name": fields.get("Firma") or fields.get("Osebno ime") or fields.get("Ime in priimek"),
                        "shareholder_id": fields.get("Zap. št. družbenika"),
                        "identifier": fields.get("Identifikacijska številka"),
                        "entered_at": fields.get("Datum vstopa"),
                        "exited_at": fields.get("Datum izstopa"),
                        "liability": fields.get("Vrsta odgovornosti za obveznosti družbe"),
                        "address": safe_fields.get("Naslov"),
                        "address_present": address_present,
                        "links": links,
                        "raw_fields": safe_fields,
                    }
                )
            elif heading == "POSLOVNI DELEŽI":
                shares.append(
                    {
                        "share_id": fields.get("Zap. št. deleža"),
                        "nominal_value": fields.get("Osnovni vložek"),
                        "share_fraction_or_percent": fields.get("Delež v odstotku ali ulomku"),
                        "holders": fields.get("Imetniki"),
                        "raw_fields": fields,
                    }
                )

    return {
        "owners": owners,
        "shares": shares,
        "evidence": {
            "section_id": "accordianBody2",
            "column_count": len(columns),
        },
    }


def extract_people_section(page, section_id: str, section_name: str) -> list[dict[str, Any]]:
    section = page.locator(f"#{section_id}")
    if section.count() == 0:
        return []

    people: list[dict[str, Any]] = []
    blocks = section.locator(".matchHeight")

    for index in range(blocks.count()):
        block = blocks.nth(index)
        fields = extract_field_pairs(block)
        if not fields:
            continue

        link = None
        linked = block.locator("a", has_text="POVEZANE OSEBE")
        if linked.count():
            link = linked.first.get_attribute("href")

        name = (
            fields.get("Osebno ime")
            or fields.get("osebno ime")
            or fields.get("Ime in priimek")
            or fields.get("ime in priimek")
        )
        role = (
            fields.get("Vrsta zastopnika")
            or fields.get("tip člana")
            or fields.get("Tip člana")
        )
        source_id = (
            fields.get("Zap. št. zastopnika")
            or fields.get("zap. št. člana")
            or fields.get("Zap. št. člana")
        )
        valid_from = (
            fields.get("Datum podelitve pooblastila")
            or fields.get("datum izvolitve ali imenovanja")
            or fields.get("Datum vpisa/podelitve pooblastila")
        )

        safe_fields, address_present = redact_address_fields(fields)
        people.append(
            {
                "name": name,
                "roles": [
                    {
                        "role": role,
                        "category": section_name,
                        "valid_from": valid_from,
                        "valid_to": fields.get("Datum prenehanja"),
                        "representation_mode": fields.get("Nacin zastopanja")
                        or fields.get("Način zastopanja"),
                        "restrictions": fields.get("Omejitve"),
                        "ownership_share": fields.get("Poslovni delež"),
                        "source_role_id": source_id,
                    }
                ],
                "source_person_id": source_id,
                "address": safe_fields.get("Naslov"),
                "address_present": address_present,
                "evidence": {
                    "section_id": section_id,
                    "linked_people_href": link,
                    "raw_fields": safe_fields,
                },
            }
        )

    return people


def extract_detail(page) -> dict[str, Any]:
    company = extract_field_pairs(page.locator("#accordianBody1"))
    people = []
    people.extend(
        extract_people_section(
            page,
            "accordianBody3",
            "Osebe, pooblascene za zastopanje",
        )
    )
    people.extend(
        extract_people_section(
            page,
            "accordianBody4",
            "Clani organa nadzora",
        )
    )

    return {
        "url": page.url,
        "title": page.title(),
        "login_required": login_required(page),
        "company": company,
        "ownership": extract_ownership_section(page),
        "people": people,
        "links": relevant_eprs_links(extract_result_links(page)),
        "evidence": {
            "accordion_sections": page.locator("[id^=accordianBody]").count(),
        },
    }


def table_rows_from_text(table_text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw_row in table_text.splitlines():
        cells = [normalize_space(cell) for cell in raw_row.split("\t")]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)
    return rows


def label_value_pairs_from_text(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    lines = [normalize_space(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    for index, line in enumerate(lines[:-1]):
        if line.endswith(":") and lines[index + 1]:
            pairs[line[:-1]] = lines[index + 1]

    return pairs


def parse_visible_page(page) -> dict[str, Any]:
    snap = snapshot(page)
    tables = extract_tables(page)
    page_text = snap.body_text

    return {
        "url": snap.url,
        "title": snap.title,
        "login_required": login_required(page),
        "company": label_value_pairs_from_text(page_text),
        "people": [],
        "tables": [
            {
                "index": table["index"],
                "rows": table_rows_from_text(table.get("text") or ""),
            }
            for table in tables
        ],
        "links": relevant_eprs_links(extract_result_links(page)),
        "evidence": {
            "table_count": len(tables),
        },
    }


def find_company_detail_href(page) -> str | None:
    links = page.locator("table a[href*='podjetje.asp']").evaluate_all(
        """
        links => links.map(a => ({
            text: (a.innerText || '').trim(),
            href: a.href
        })).filter(x => x.text && x.href.includes('/prs/podjetje.asp'))
        """
    )

    if len(links) != 1:
        return None

    return links[0]["href"]


def run_probe(registration_number: str) -> dict[str, Any]:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        page_count_before_acquire = count_ajpes_pages(browser)
        page = find_or_open_dedicated_eprs_page(browser)
        page_count_after_acquire = count_ajpes_pages(browser)

        if "ajpes.si/prs" not in page.url.lower():
            page.goto(EPRS_URL, wait_until="domcontentloaded", timeout=30_000)
            mark_dedicated_eprs_page(page)

        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
        mark_dedicated_eprs_page(page)
        page.locator("#naziv").fill("")
        page.locator("#davcna").fill("")
        page.locator("#maticna").fill(registration_number)
        page.locator("button[type='submit']").filter(has_text=re.compile(r"^I")).last.click()
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1_000)

        parsed = parse_visible_page(page)
        parsed["input"] = {"registration_number": registration_number}
        parsed["source"] = "ajpes_eprs"
        parsed["task_type"] = "company_registry_probe"

        if parsed["login_required"]:
            page_count_after_lookup = count_ajpes_pages(browser)
            return attach_page_diagnostics(
                parsed,
                {
                    "ajpes_pages_before_acquire": page_count_before_acquire,
                    "ajpes_pages_after_acquire": page_count_after_acquire,
                    "ajpes_pages_after_lookup": page_count_after_lookup,
                    "lookup_opened_new_ajpes_pages": page_count_after_lookup
                    > page_count_after_acquire,
                    "single_dedicated_page_reused": page_count_after_lookup
                    <= page_count_after_acquire,
                },
            )

        detail_href = find_company_detail_href(page)
        if not detail_href:
            parsed["status"] = "NO_UNIQUE_DETAIL_LINK"
            page_count_after_lookup = count_ajpes_pages(browser)
            return attach_page_diagnostics(
                parsed,
                {
                    "ajpes_pages_before_acquire": page_count_before_acquire,
                    "ajpes_pages_after_acquire": page_count_after_acquire,
                    "ajpes_pages_after_lookup": page_count_after_lookup,
                    "lookup_opened_new_ajpes_pages": page_count_after_lookup
                    > page_count_after_acquire,
                    "single_dedicated_page_reused": page_count_after_lookup
                    <= page_count_after_acquire,
                },
            )

        page.goto(urljoin(page.url, detail_href), wait_until="domcontentloaded", timeout=30_000)
        mark_dedicated_eprs_page(page)
        page.wait_for_timeout(1_000)
        detail = extract_detail(page)
        detail["input"] = parsed["input"]
        detail["source"] = parsed["source"]
        detail["task_type"] = parsed["task_type"]
        page_count_after_lookup = count_ajpes_pages(browser)
        return attach_page_diagnostics(
            detail,
            {
                "ajpes_pages_before_acquire": page_count_before_acquire,
                "ajpes_pages_after_acquire": page_count_after_acquire,
                "ajpes_pages_after_lookup": page_count_after_lookup,
                "lookup_opened_new_ajpes_pages": page_count_after_lookup
                > page_count_after_acquire,
                "single_dedicated_page_reused": page_count_after_lookup
                <= page_count_after_acquire,
            },
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only AJPES ePRS one-company diagnostic probe.",
    )
    parser.add_argument("registration_number", help="Maticna stevilka for one lookup")
    args = parser.parse_args(argv)

    try:
        print_json(run_probe(args.registration_number))
        return 0
    except PlaywrightTimeoutError as exc:
        print_json({"status": "TIMEOUT", "error_message": str(exc)})
        return 2
    except Exception as exc:
        print_json(
            {
                "status": "ERROR",
                "error_type": exc.__class__.__name__,
                "error_message": str(exc),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
