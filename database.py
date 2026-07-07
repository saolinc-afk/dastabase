import sqlite3
from pathlib import Path

DB_PATH = Path("database") / "dastabase.db"


def initialize_database():
    """Create the database and companies table if they don't exist."""

    DB_PATH.parent.mkdir(exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            company_name TEXT,
            registration_number TEXT,
            tax_number TEXT,

            employees INTEGER,
            revenue REAL,
            profit REAL,

            activity TEXT,

            street TEXT,
            postal_code TEXT,
            city TEXT,

            website TEXT,
            email TEXT,
            phone TEXT,

            director TEXT,

            source TEXT,
            source_url TEXT,

            collected_at TEXT,
            updated_at TEXT
        )
    """)

    conn.commit()
    conn.close()

    print(f"✅ Database initialized: {DB_PATH}")


if __name__ == "__main__":
    initialize_database()
