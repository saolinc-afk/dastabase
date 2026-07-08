import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path("database") / "dastabase.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def initialize_database():

    DB_PATH.parent.mkdir(exist_ok=True)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS companies (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            company_name TEXT,
            tax_number TEXT UNIQUE,
            registration_number TEXT,

            address TEXT,

            activity TEXT,

            phone TEXT,
            website TEXT,
            email TEXT,

            collected_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def save_company(company):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR IGNORE INTO companies (

            company_name,
            tax_number,
            registration_number,
            address,
            activity,
            phone,
            website,
            email,
            collected_at

        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (

        company["company_name"],
        company["tax_number"],
        company["registration_number"],
        company["address"],
        company["activity"],
        company["phone"],
        company["website"],
        company["email"],
        datetime.now().isoformat()

    ))

    conn.commit()
    conn.close()

    print("✓ Company saved.")