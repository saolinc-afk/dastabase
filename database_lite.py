"""
----------------------------------------------------
Dastabase
database_lite.py
Release 1.2
DEL 1 / 2
----------------------------------------------------
"""

import sqlite3
import json
import re
from datetime import datetime, timezone
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent / "database"

DB_PATH = DB_DIR / "dastabase_lite.db"


# ----------------------------------------------------
# CONNECTION
# ----------------------------------------------------

def get_connection():

    conn = sqlite3.connect(DB_PATH)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    return conn


# ----------------------------------------------------
# INITIALIZE DATABASE
# ----------------------------------------------------

def initialize_database():

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()

    cur = conn.cursor()

    # ------------------------------------------------
    # Companies
    # ------------------------------------------------

    cur.execute("""

    CREATE TABLE IF NOT EXISTS companies_lite(

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        company_name TEXT,

        registration_number TEXT UNIQUE,

        tax_number TEXT,

        address TEXT,

        municipality TEXT,

        revenue_2025 REAL,

        profit_2025 REAL,

        employees_2025 REAL,

        gvin_company_id TEXT,

        collected_at TEXT

    )

    """)

    # ------------------------------------------------
    # Website Discovery
    # ------------------------------------------------

    cur.execute("""

    CREATE TABLE IF NOT EXISTS website_discovery(

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        company_id INTEGER,

        website TEXT,

        confidence INTEGER,

        status TEXT,

        method TEXT,

        checked_at TEXT,

        FOREIGN KEY(company_id)

        REFERENCES companies_lite(id)

    )

    """)

    # ------------------------------------------------
    # Email Discovery
    # ------------------------------------------------

    cur.execute("""

    CREATE TABLE IF NOT EXISTS email_discovery(

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        company_id INTEGER NOT NULL,

        email TEXT NOT NULL,

        confidence INTEGER,

        website TEXT,

        page_url TEXT,

        page_title TEXT,

        found_in TEXT,

        checked_at TEXT,

        UNIQUE(company_id,email)

    )

    """)

    # Additive migration: keep every existing company and its internal ID.
    columns = {row[1] for row in cur.execute("PRAGMA table_info(companies_lite)")}
    for name in ("gvin_company_id", "gvin_detail_url", "financial_raw_json", "financial_status"):
        if name not in columns:
            cur.execute(f"ALTER TABLE companies_lite ADD COLUMN {name} TEXT")
    for name in ("assets_2025", "capital_2025"):
        if name not in columns:
            cur.execute(f"ALTER TABLE companies_lite ADD COLUMN {name} REAL")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_lite_tax ON companies_lite(tax_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_lite_gvin ON companies_lite(gvin_company_id)")

    conn.commit()

    conn.close()


# ----------------------------------------------------
# RESET DATABASE
# ----------------------------------------------------

def reset_database():

    conn = get_connection()

    cur = conn.cursor()

    cur.execute(

        "DROP TABLE IF EXISTS email_discovery"

    )

    cur.execute(

        "DROP TABLE IF EXISTS website_discovery"

    )

    cur.execute(

        "DROP TABLE IF EXISTS companies_lite"

    )

    conn.commit()

    conn.close()

    initialize_database()


# ----------------------------------------------------
# SAVE COMPANY
# ----------------------------------------------------

class IdentityConflict(ValueError):
    """Conflicting identifiers must be reviewed, never merged automatically."""


def normalize_identifier(value, *, tax=False):
    value = re.sub(r"\s+", "", str(value or "")).upper()
    if tax and value.startswith("SI"):
        value = value[2:]
    if not value or not value.isascii() or not value.isdigit():
        return None
    return value


def save_company(company, conn=None, *, return_id=False):
    """Stable-ID upsert. An optional connection belongs to the page transaction."""
    if conn is None:
        connection = get_connection()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                return save_company(company, connection, return_id=return_id)
        finally:
            connection.close()

    data = dict(company)
    for key in ("registration_number", "tax_number", "gvin_company_id"):
        data[key] = normalize_identifier(data.get(key), tax=key == "tax_number")
    if not any(data.get(k) for k in ("registration_number", "tax_number", "gvin_company_id")):
        raise IdentityConflict("No usable registration, tax or GVIN identifier")

    # Normalize existing identifiers during matching without rewriting legacy rows.
    # Exact registration is primary; tax/GVIN fallbacks must identify the same row.
    conn.create_function("lite_identifier", 2, lambda value, tax: normalize_identifier(value, tax=bool(tax)))
    matches = conn.execute(
        """SELECT * FROM companies_lite
           WHERE lite_identifier(registration_number, 0) = ?
              OR lite_identifier(tax_number, 1) = ?
              OR lite_identifier(gvin_company_id, 0) = ?""",
        (data["registration_number"], data["tax_number"], data["gvin_company_id"]),
    ).fetchall()
    if len(matches) > 1:
        raise IdentityConflict("Identifiers match multiple existing companies")
    existing = matches[0] if matches else None
    if existing:
        for key in ("registration_number", "tax_number", "gvin_company_id"):
            previous = normalize_identifier(existing[key], tax=key == "tax_number")
            if previous and data[key] and previous != data[key]:
                raise IdentityConflict(f"Conflicting {key} for company ID {existing['id']}")

    fields = (
        "company_name", "registration_number", "tax_number", "address", "municipality",
        "revenue_2025", "profit_2025", "employees_2025", "assets_2025", "capital_2025", "gvin_company_id",
        "gvin_detail_url", "collected_at", "financial_raw_json", "financial_status",
    )
    data["collected_at"] = data.get("collected_at") or datetime.now(timezone.utc).isoformat()
    values = [data.get(key) if data.get(key) != "" else None for key in fields]
    # Resolve the identity first, then UPSERT on its stable primary key. This also
    # handles legacy formatting and rows known only by a tax/GVIN identifier.
    assignments = ", ".join(f"{key}=COALESCE(excluded.{key}, companies_lite.{key})" for key in fields)
    conn.execute(
        f"INSERT INTO companies_lite (id, {', '.join(fields)}) "
        f"VALUES ({', '.join('?' for _ in range(len(fields) + 1))}) "
        f"ON CONFLICT(id) DO UPDATE SET {assignments}",
        [existing["id"] if existing else None, *values],
    )
    action = "updated" if existing else "inserted"
    company_id = existing["id"] if existing else conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return (action, company_id) if return_id else action


# ----------------------------------------------------
# SAVE WEBSITE (unchanged discovery API)
# ----------------------------------------------------

def initialize_discovery_database():
    """Additive Phase 2 migration. Existing discovery history is retained."""
    conn = get_connection()
    try:
        with conn:
            for table, fields in {
                "website_discovery": {
                    "discovered_at": "TEXT", "evidence_json": "TEXT", "contact_page": "TEXT",
                    "metadata_json": "TEXT", "email_status": "TEXT", "last_error": "TEXT",
                    "attempt_count": "INTEGER DEFAULT 0", "rule_version": "TEXT", "relationship": "TEXT",
                    "verified_scope": "TEXT", "ownership_json": "TEXT",
                },
                "email_discovery": {"discovered_at": "TEXT", "evidence_json": "TEXT"},
            }.items():
                columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                for field, kind in fields.items():
                    if field not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {field} {kind}")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_website_discovery_company ON website_discovery(company_id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS discovery_repair_audit (
                id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, company_id INTEGER NOT NULL,
                table_name TEXT NOT NULL, record_id INTEGER NOT NULL, action TEXT NOT NULL,
                reason TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT,
                created_at TEXT NOT NULL, FOREIGN KEY(company_id) REFERENCES companies_lite(id))""")
    finally:
        conn.close()


def save_website(result):
    """Update the latest company row under a write lock; never replace/delete IDs.

    Legacy duplicate history, if any, is retained; this writer creates no new
    duplicates. All readers select the latest row consistently.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT id FROM website_discovery WHERE company_id=? ORDER BY id DESC LIMIT 1",
                                    (result['company_id'],)).fetchone()
            fields = ('company_id', 'website', 'confidence', 'status', 'method', 'checked_at',
                      'discovered_at', 'evidence_json', 'contact_page', 'metadata_json', 'email_status',
                      'last_error', 'rule_version', 'relationship', 'verified_scope', 'ownership_json')
            values = (result['company_id'], result.get('final_url', result.get('website', '')),
                      min(100, max(0, result.get('confidence', 0))), result.get('status', 'ERROR'),
                      result.get('method', 'guess'), now, now,
                      json.dumps(result.get('evidence', []), ensure_ascii=False), result.get('contact_page', ''),
                      json.dumps({'attempts': result.get('attempts', []), 'errors': result.get('errors', [])}, ensure_ascii=False),
                      result.get('email_status', 'PENDING'), '\n'.join(result.get('errors', [])),
                      result.get('rule_version', 'phase2-mvp-1'), result.get('relationship', 'UNRESOLVED'),
                      result.get('verified_scope',''), json.dumps(result.get('ownership',{}), ensure_ascii=False))
            if existing:
                assignments = ', '.join(f'{f}=?' for f in fields if f != 'discovered_at')
                update_values = tuple(v for f, v in zip(fields, values) if f != 'discovered_at')
                conn.execute(f"UPDATE website_discovery SET {assignments}, discovered_at=COALESCE(discovered_at, ?), "
                             "attempt_count=COALESCE(attempt_count,0)+1 WHERE id=?", (*update_values, now, existing['id']))
            else:
                conn.execute(f"INSERT INTO website_discovery ({', '.join(fields)}, attempt_count) "
                             f"VALUES ({', '.join('?' for _ in fields)}, 1)", values)
    finally:
        conn.close()


def save_email(conn, company_id, email, confidence, page_url, page_title, found_in,
               *, website=None, evidence=None):
    if not website:
        raise ValueError('Verified source website is required for email provenance')
    # Defense in depth: callers must not be able to persist directory or
    # marketplace contact addresses as company email addresses.
    from discovery.website_verifier import blocked_url
    if blocked_url(website) or blocked_url(page_url):
        raise ValueError('Blocked third-party website cannot be an email source')
    from discovery.ownership import email_attribution
    company = conn.execute('SELECT * FROM companies_lite WHERE id=?', (company_id,)).fetchone()
    site = conn.execute('SELECT * FROM website_discovery WHERE company_id=? ORDER BY id DESC LIMIT 1', (company_id,)).fetchone()
    if not company or not site:
        raise ValueError('Persisted target identity and ownership evidence are required')
    source = evidence or {}
    ownership = json.loads(site['ownership_json'] or '{}')
    attribution = email_attribution(dict(company), email, page_url, source.get('publication',''),
                                    ownership, json.loads(site['evidence_json'] or '[]'), page_title,
                                    source.get('contact_block'), source.get('visible_email',''))
    if not attribution['attributable']:
        raise ValueError('Insufficient entity-specific email provenance: '+attribution['reason'])
    evidence = {**source, 'attribution': attribution}
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""INSERT INTO email_discovery
        (company_id,email,confidence,website,page_url,page_title,found_in,checked_at,discovered_at,evidence_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(company_id,email) DO UPDATE SET
        confidence=excluded.confidence, website=excluded.website, page_url=excluded.page_url,
        page_title=excluded.page_title, found_in=excluded.found_in, checked_at=excluded.checked_at,
        evidence_json=excluded.evidence_json""",
        (company_id, email.strip().lower(), min(100, max(0, confidence)), website, page_url,
         page_title, found_in, now, now, json.dumps(evidence or {}, ensure_ascii=False)))


def invalidate_blocked_discovery_records(company_ids):
    """Remove blocked-host emails and demote only the supplied company rows.

    This is a targeted data repair for a prior verifier defect. It intentionally
    does not touch enrichment data belonging to other companies.
    """
    from discovery.website_verifier import blocked_url
    ids = sorted({int(company_id) for company_id in company_ids})
    if not ids:
        return {'websites': 0, 'emails': 0}
    placeholders = ','.join('?' for _ in ids)
    conn = get_connection()
    repaired_websites = repaired_emails = 0
    try:
        with conn:
            website_rows = conn.execute(
                f"SELECT id, company_id, website FROM website_discovery WHERE company_id IN ({placeholders})", ids
            ).fetchall()
            for row in website_rows:
                if blocked_url(row['website'] or ''):
                    conn.execute("UPDATE website_discovery SET status='REVIEW', confidence=0, email_status='SKIPPED', "
                                 "last_error='Blocked third-party listing domain; requires rediscovery' WHERE id=?", (row['id'],))
                    repaired_websites += 1
            email_rows = conn.execute(
                f"SELECT id, website, page_url FROM email_discovery WHERE company_id IN ({placeholders})", ids
            ).fetchall()
            for row in email_rows:
                if blocked_url(row['website'] or '') or blocked_url(row['page_url'] or ''):
                    conn.execute('DELETE FROM email_discovery WHERE id=?', (row['id'],))
                    repaired_emails += 1
    finally:
        conn.close()
    return {'websites': repaired_websites, 'emails': repaired_emails}


def mark_group_review_records(company_ids):
    """Keep parent/group sites as evidence while preventing subsidiary attribution."""
    ids = sorted({int(company_id) for company_id in company_ids})
    if not ids:
        return {'websites': 0, 'emails': 0}
    placeholders = ','.join('?' for _ in ids)
    conn = get_connection()
    websites = emails = 0
    try:
        with conn:
            rows = conn.execute(
                f"SELECT id FROM website_discovery WHERE company_id IN ({placeholders}) ORDER BY id DESC", ids
            ).fetchall()
            seen = set()
            for row in rows:
                if row['id'] in seen: continue
                seen.add(row['id'])
                conn.execute("UPDATE website_discovery SET status='GROUP_REVIEW', relationship='GROUP_PARENT', "
                             "email_status='SKIPPED', confidence=MIN(confidence,60), "
                             "last_error='Parent/group website requires entity-specific evidence' WHERE id=?", (row['id'],))
                websites += 1
            emails = conn.execute(f"SELECT count(*) FROM email_discovery WHERE company_id IN ({placeholders})", ids).fetchone()[0]
            conn.execute(f"DELETE FROM email_discovery WHERE company_id IN ({placeholders})", ids)
    finally:
        conn.close()
    return {'websites': websites, 'emails': emails}


# ----------------------------------------------------
# COUNTS
# ----------------------------------------------------

def company_count():

    conn = get_connection()

    cur = conn.cursor()

    cur.execute(

        "SELECT COUNT(*) FROM companies_lite"

    )

    count = cur.fetchone()[0]

    conn.close()

    return count


def website_count():

    conn = get_connection()

    cur = conn.cursor()

    cur.execute(

        "SELECT COUNT(*) FROM website_discovery"

    )

    count = cur.fetchone()[0]

    conn.close()

    return count


def email_count():

    conn = get_connection()

    cur = conn.cursor()

    cur.execute(

        "SELECT COUNT(*) FROM email_discovery"

    )

    count = cur.fetchone()[0]

    conn.close()

    return count


# ----------------------------------------------------
# MAIN
# ----------------------------------------------------

if __name__ == "__main__":

    initialize_database()

    print()

    print("=" * 60)

    print("Dastabase Lite Database")

    print("=" * 60)

    print()

    print("Companies :", company_count())

    print("Websites  :", website_count())

    print("Emails    :", email_count())

    print()

    print("Database ready.")

    print("=" * 60)
