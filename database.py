import sqlite3
from datetime import datetime
from settings import DATABASE_PATH

DB_PATH = DATABASE_PATH


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
        revenue_2022 REAL,
        revenue_2023 REAL,
        revenue_2024 REAL,
        revenue_2025 REAL,
        employees_2022 REAL,
        employees_2023 REAL,
        employees_2024 REAL,
        employees_2025 REAL,
        owners TEXT,
        representatives TEXT,
        collected_at TEXT
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS website_cache(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER UNIQUE,
        url TEXT,
        cache_file TEXT,
        http_status INTEGER,
        page_title TEXT,
        content_type TEXT,
        crawl_duration_ms INTEGER,
        crawled_at TEXT,
        FOREIGN KEY(company_id) REFERENCES companies(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS website_intelligence(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER UNIQUE,
        website_detected TEXT,
        website_status TEXT,
        languages TEXT,
        language_count INTEGER DEFAULT 0,
        international INTEGER DEFAULT 0,
        family_business INTEGER,
        family_confidence TEXT,
        family_reason TEXT,
        linkedin_url TEXT,
        facebook_url TEXT,
        instagram_url TEXT,
        youtube_url TEXT,
        enriched_version INTEGER DEFAULT 1,
        last_enriched TEXT,
        FOREIGN KEY(company_id) REFERENCES companies(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        source TEXT,
        status TEXT,
        companies_total INTEGER DEFAULT 0,
        companies_processed INTEGER DEFAULT 0,
        started_at TEXT,
        finished_at TEXT,
        notes TEXT
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
        revenue_2022,revenue_2023,
        revenue_2024,revenue_2025,
        employees_2022,employees_2023,
        employees_2024,employees_2025,
        owners,
        representatives,
        collected_at
    )
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        company.get("revenue_2022", 0),
        company.get("revenue_2023", 0),
        company.get("revenue_2024", 0),
        company.get("revenue_2025", 0),
        company.get("employees_2022", 0),
        company.get("employees_2023", 0),
        company.get("employees_2024", 0),
        company.get("employees_2025", 0),
        company.get("owners", ""),
        company.get("representatives", ""),
        datetime.now().isoformat()
    ))

    conn.commit()
    conn.close()
    print(f"✓ {company['company_name']}")

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_companies_for_enrichment(limit=None):
    conn=get_connection()
    sql="""
    SELECT c.*
    FROM companies c
    LEFT JOIN website_intelligence wi ON c.id=wi.company_id
    WHERE wi.company_id IS NULL
    ORDER BY c.id
    """
    if limit:
        sql+=f" LIMIT {int(limit)}"
    rows=conn.execute(sql).fetchall()
    conn.close()
    return rows

def save_website_intelligence(data):
    conn=get_connection()
    conn.execute("""
    INSERT OR REPLACE INTO website_intelligence(
    company_id,website_detected,website_status,languages,
    language_count,international,family_business,
    family_confidence,family_reason,
    linkedin_url,facebook_url,instagram_url,youtube_url,
    enriched_version,last_enriched)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,(
    data["company_id"],
    data.get("website_detected"),
    data.get("website_status"),
    data.get("languages",""),
    data.get("language_count",0),
    data.get("international",0),
    data.get("family_business"),
    data.get("family_confidence"),
    data.get("family_reason"),
    data.get("linkedin_url"),
    data.get("facebook_url"),
    data.get("instagram_url"),
    data.get("youtube_url"),
    data.get("enriched_version",1),
    datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()
