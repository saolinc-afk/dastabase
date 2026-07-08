import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path("database") / "dastabase.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def initialize_database():
    DB_PATH.parent.mkdir(exist_ok=True)

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS companies(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT,
        tax_number TEXT UNIQUE,
        registration_number TEXT,
        address TEXT,
        municipality TEXT,
        activity TEXT,
        phone TEXT,
        website TEXT,
        email TEXT,
        revenue_2024 REAL,
        revenue_2025 REAL,
        employees_2024 REAL,
        employees_2025 REAL,
        collected_at TEXT
    )
    """)

    conn.commit()
    conn.close()


def save_company(company):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    INSERT OR IGNORE INTO companies(
        company_name,tax_number,registration_number,address,
        municipality,
        activity,phone,website,email,
        revenue_2024,revenue_2025,
        employees_2024,employees_2025,
        collected_at
    )
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        company["company_name"],
        company["tax_number"],
        company["registration_number"],
        company["address"],
        company["municipality"],
        company["activity"],
        company["phone"],
        company["website"],
        company["email"],
        company["revenue_2024"],
        company["revenue_2025"],
        company["employees_2024"],
        company["employees_2025"],
        datetime.now().isoformat()
    ))

    conn.commit()
    conn.close()
    print("✓ Company saved.")
