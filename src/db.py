"""
SQLite database layer for insider sales data.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "insider.db"


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS companies (
                cik         TEXT PRIMARY KEY,
                ticker      TEXT,
                name        TEXT,
                last_synced TEXT  -- ISO timestamp
            );

            CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker);

            CREATE TABLE IF NOT EXISTS filings (
                accession_no    TEXT PRIMARY KEY,
                cik             TEXT NOT NULL,
                filed_date      TEXT NOT NULL,   -- YYYY-MM-DD
                primary_doc     TEXT,            -- filename of primary XML document
                processed       INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (cik) REFERENCES companies(cik)
            );

            CREATE INDEX IF NOT EXISTS idx_filings_cik ON filings(cik);

            CREATE TABLE IF NOT EXISTS transactions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                accession_no        TEXT NOT NULL,
                cik                 TEXT NOT NULL,
                insider_name        TEXT,
                insider_role        TEXT,
                transaction_date    TEXT NOT NULL,   -- YYYY-MM-DD
                transaction_type    TEXT NOT NULL,   -- S, AS, OS, JS, etc.
                shares              REAL,
                price               REAL,
                value               REAL,            -- shares * price
                is_10b51_plan       INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (accession_no) REFERENCES filings(accession_no)
            );

            CREATE INDEX IF NOT EXISTS idx_tx_cik_date
                ON transactions(cik, transaction_date);
        """)


# ── Company helpers ───────────────────────────────────────────────────────────

def upsert_companies(rows: list[dict]):
    """Insert or update companies. rows: [{cik, ticker, name}]"""
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO companies (cik, ticker, name)
            VALUES (:cik, :ticker, :name)
            ON CONFLICT(cik) DO UPDATE SET
                ticker = excluded.ticker,
                name   = excluded.name
            """,
            rows,
        )


def mark_company_synced(cik: str, timestamp: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE companies SET last_synced = ? WHERE cik = ?", (timestamp, cik)
        )


def get_all_companies() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM companies ORDER BY ticker").fetchall()


def get_company_by_ticker(ticker: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM companies WHERE ticker = ? COLLATE NOCASE", (ticker,)
        ).fetchone()


# ── Filing helpers ────────────────────────────────────────────────────────────

def upsert_filing(accession_no: str, cik: str, filed_date: str, primary_doc: str | None = None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO filings (accession_no, cik, filed_date, primary_doc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(accession_no) DO NOTHING
            """,
            (accession_no, cik, filed_date, primary_doc),
        )


def mark_filing_processed(accession_no: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE filings SET processed = 1 WHERE accession_no = ?", (accession_no,)
        )


def get_unprocessed_filings(cik: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM filings WHERE cik = ? AND processed = 0 ORDER BY filed_date",
            (cik,),
        ).fetchall()


def filing_exists(accession_no: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM filings WHERE accession_no = ?", (accession_no,)
        ).fetchone()
        return row is not None


# ── Transaction helpers ───────────────────────────────────────────────────────

def insert_transactions(rows: list[dict]):
    """Bulk insert transactions. Existing accession_no rows are deleted first."""
    if not rows:
        return
    accession_no = rows[0]["accession_no"]
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM transactions WHERE accession_no = ?", (accession_no,)
        )
        conn.executemany(
            """
            INSERT INTO transactions
                (accession_no, cik, insider_name, insider_role,
                 transaction_date, transaction_type, shares, price, value, is_10b51_plan)
            VALUES
                (:accession_no, :cik, :insider_name, :insider_role,
                 :transaction_date, :transaction_type, :shares, :price, :value, :is_10b51_plan)
            """,
            rows,
        )


def get_transactions_for_company(cik: str, since_date: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM transactions
            WHERE cik = ? AND transaction_date >= ?
            ORDER BY transaction_date DESC
            """,
            (cik, since_date),
        ).fetchall()


def get_sale_totals(cik: str, start_date: str, end_date: str,
                    exclude_10b51: bool = False) -> dict:
    """Return aggregated sale stats for a company between two dates."""
    filters = "cik = ? AND transaction_date >= ? AND transaction_date < ?"
    params: list = [cik, start_date, end_date]

    if exclude_10b51:
        filters += " AND is_10b51_plan = 0"

    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
                COALESCE(SUM(value), 0)            AS total_value,
                COALESCE(SUM(shares), 0)           AS total_shares,
                COUNT(DISTINCT insider_name)       AS unique_sellers,
                MAX(transaction_date)              AS last_sale_date
            FROM transactions
            WHERE {filters}
              AND transaction_type IN ('S','AS','OS','JS')
            """,
            params,
        ).fetchone()
    return dict(row)
