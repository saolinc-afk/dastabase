"""
----------------------------------------------------
Dastabase
database_lite.py
Release 1.2
DEL 1 / 2
----------------------------------------------------
"""

import sqlite3
from pathlib import Path

DB_DIR = Path("database")
DB_DIR.mkdir(exist_ok=True)

DB_PATH = DB_DIR / "dastabase_lite.db"


# ----------------------------------------------------
# CONNECTION
# ----------------------------------------------------

def get_connection():

    conn = sqlite3.connect(DB_PATH)

    conn.row_factory = sqlite3.Row

    return conn


# ----------------------------------------------------
# INITIALIZE DATABASE
# ----------------------------------------------------

def initialize_database():

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

def save_company(company):

    conn = get_connection()

    cur = conn.cursor()

    cur.execute("""

    INSERT OR REPLACE INTO companies_lite(

        company_name,

        registration_number,

        tax_number,

        address,

        municipality,

        revenue_2025,

        profit_2025,

        employees_2025,

        gvin_company_id,

        collected_at

    )

    VALUES(

        ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')

    )

    """,

    (

        company.get("company_name"),

        company.get("registration_number"),

        company.get("tax_number"),

        company.get("address"),

        company.get("municipality"),

        company.get("revenue_2025",0),

        company.get("profit_2025",0),

        company.get("employees_2025",0),

        company.get("gvin_company_id")

    ))

    conn.commit()

    conn.close()

    # ----------------------------------------------------
# SAVE WEBSITE
# ----------------------------------------------------

def save_website(result):

    conn = get_connection()

    cur = conn.cursor()

    cur.execute("""

    INSERT OR REPLACE INTO website_discovery(

        company_id,

        website,

        confidence,

        status,

        method,

        checked_at

    )

    VALUES(

        ?, ?, ?, ?, ?, datetime('now')

    )

    """,

    (

        result["company_id"],

        result.get("website",""),

        result.get("confidence",0),

        result.get("status","UNKNOWN"),

        result.get("method","guess")

    ))

    conn.commit()

    conn.close()


# ----------------------------------------------------
# SAVE EMAIL
# ----------------------------------------------------

def save_email(

    conn,

    company_id,

    email,

    confidence,

    page_url,

    page_title,

    found_in

):

    cur = conn.cursor()

    domain = ""

    try:

        domain = email.split("@")[1]

    except:

        pass

    cur.execute("""

    INSERT OR IGNORE INTO email_discovery(

        company_id,

        email,

        confidence,

        website,

        page_url,

        page_title,

        found_in,

        checked_at

    )

    VALUES(

        ?, ?, ?, ?, ?, ?, ?, datetime('now')

    )

    """,

    (

        company_id,

        email,

        confidence,

        domain,

        page_url,

        page_title,

        found_in

    ))

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