"""
SEC EDGAR client — fetches company universe, Form 4 filing lists, and parses Form 4 XML.

EDGAR rate limit: 10 requests/second (we stay at ~8 to be safe).
User-Agent header is required per SEC fair-access policy.
"""
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

from src import db

# SEC requires a descriptive User-Agent identifying the requester.
# Update with your contact email before running.
HEADERS = {
    "User-Agent": "insidersalesnotify research@example.com",
    "Accept-Encoding": "gzip, deflate",
}

SALE_TYPES = {"S", "AS", "OS", "JS"}  # all variants captured
_last_request_at: float = 0.0


def _get(url: str, **kwargs) -> requests.Response:
    """Throttled GET — never exceeds ~8 req/sec."""
    global _last_request_at
    gap = time.monotonic() - _last_request_at
    if gap < 0.125:
        time.sleep(0.125 - gap)
    resp = requests.get(url, headers=HEADERS, timeout=30, **kwargs)
    _last_request_at = time.monotonic()
    resp.raise_for_status()
    return resp


# ── Company universe ──────────────────────────────────────────────────────────

def fetch_all_companies() -> list[dict]:
    """
    Download the full list of SEC-registered companies (CIK + ticker + name).
    Returns a list of {cik, ticker, name} dicts ready for db.upsert_companies().
    """
    url = "https://www.sec.gov/files/company_tickers.json"
    data = _get(url).json()
    rows = []
    for entry in data.values():
        cik = str(entry["cik_str"]).zfill(10)
        rows.append({
            "cik": cik,
            "ticker": entry["ticker"],
            "name": entry["title"],
        })
    return rows


# ── Filing index ──────────────────────────────────────────────────────────────

def fetch_form4_filings(cik: str) -> list[dict]:
    """
    Return all Form 4 / Form 4/A filing metadata for a company.
    Each item: {accession_no, filed_date}
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _get(url).json()

    filings = data.get("filings", {}).get("recent", {})
    form_types = filings.get("form", [])
    accessions = filings.get("accessionNumber", [])
    dates = filings.get("filingDate", [])

    primary_docs = filings.get("primaryDocument", [])

    def _clean_doc(doc: str) -> str:
        # Strip XSL stylesheet prefix (e.g. "xslF345X05/filename.xml" → "filename.xml")
        return doc.split("/")[-1] if "/" in doc else doc

    results = []
    _pad = [""] * len(form_types)
    for form_type, acc, date, doc in zip(form_types, accessions, dates,
                                          primary_docs if primary_docs else _pad):
        if form_type in ("4", "4/A"):
            results.append({
                "accession_no": acc.replace("-", ""),
                "filed_date": date,
                "primary_doc": _clean_doc(doc) if doc else None,
            })

    # Also check older filings in paginated "files" array if present
    for file_set in data.get("filings", {}).get("files", []):
        file_data = _get(
            f"https://data.sec.gov/submissions/{file_set['name']}"
        ).json()
        f = file_data
        f_docs = f.get("primaryDocument", [])
        _fpad = [""] * len(f.get("form", []))
        for form_type, acc, date, doc in zip(
            f.get("form", []), f.get("accessionNumber", []),
            f.get("filingDate", []), f_docs if f_docs else _fpad
        ):
            if form_type in ("4", "4/A"):
                results.append({
                    "accession_no": acc.replace("-", ""),
                    "filed_date": date,
                    "primary_doc": _clean_doc(doc) if doc else None,
                })

    return results


# ── Form 4 XML parsing ────────────────────────────────────────────────────────

def _xml_url(cik: str, accession_no: str, primary_doc: str | None) -> str | None:
    """Build the direct URL for a Form 4 XML document."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_no}/"
    if primary_doc and primary_doc.endswith(".xml"):
        return base + primary_doc
    # Fallback: the full submission text file (contains the XML)
    acc_dashed = f"{accession_no[:10]}-{accession_no[10:12]}-{accession_no[12:]}"
    return base + acc_dashed + ".txt"


def _safe_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _text(elem, path: str) -> str | None:
    node = elem.find(path)
    return node.text.strip() if node is not None and node.text else None


def _is_10b51(root: ET.Element) -> bool:
    """Return True if the filing indicates a Rule 10b5-1 plan."""
    # Check both non-derivative and derivative transaction tables
    for tag in (
        ".//nonDerivativeTransaction/planAdoptionDate",
        ".//derivativeTransaction/planAdoptionDate",
        ".//nonDerivativeTransaction/transactionTimingCodeCode",
        ".//derivativeTransaction/transactionTimingCodeCode",
    ):
        node = root.find(tag)
        if node is not None and node.text:
            return True

    # Alternative field in newer filings
    for tag in (
        ".//rule10b51PlanFlag",
        ".//rule10b5-1PlanFlag",
    ):
        node = root.find(tag)
        if node is not None and node.text and node.text.strip() == "1":
            return True

    return False


def parse_form4(cik: str, accession_no: str, primary_doc: str | None = None) -> list[dict]:
    """
    Fetch and parse a Form 4 XML, returning a list of transaction dicts.
    Only sale types (S, AS, OS, JS) are returned.
    """
    xml_url = _xml_url(cik, accession_no, primary_doc)
    if not xml_url:
        return []

    try:
        resp = _get(xml_url)
        content = resp.text
    except Exception:
        return []

    # Strip any leading non-XML content (some filings have SGML headers)
    xml_start = content.find("<?xml")
    if xml_start == -1:
        xml_start = content.find("<ownershipDocument")
    if xml_start == -1:
        return []
    content = content[xml_start:]

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []

    is_plan = _is_10b51(root)

    # Insider identity (reporting owner)
    insider_name = _text(root, ".//reportingOwner/reportingOwnerId/rptOwnerName")
    insider_role_parts = []
    if _text(root, ".//reportingOwner/reportingOwnerRelationship/isOfficer") == "1":
        title = _text(root, ".//reportingOwner/reportingOwnerRelationship/officerTitle")
        insider_role_parts.append(title or "Officer")
    if _text(root, ".//reportingOwner/reportingOwnerRelationship/isDirector") == "1":
        insider_role_parts.append("Director")
    if _text(root, ".//reportingOwner/reportingOwnerRelationship/isTenPercentOwner") == "1":
        insider_role_parts.append("10% Owner")
    insider_role = ", ".join(insider_role_parts) or "Other"

    transactions = []

    def _extract_tx(tx_elem: ET.Element):
        tx_type = _text(tx_elem, "transactionAmounts/transactionAcquiredDisposedCode/value")
        if not tx_type:
            tx_type = _text(tx_elem, "transactionCoding/transactionCode")
        if not tx_type:
            return

        # Normalize: D = Disposed (sale), A = Acquired
        # The transactionCode field (S, AS, OS, JS, etc.) is what we want
        code = _text(tx_elem, "transactionCoding/transactionCode") or ""
        code = code.strip().upper()

        if code not in SALE_TYPES:
            return

        date_str = _text(tx_elem, "transactionDate/value")
        if not date_str:
            return

        shares_raw = _text(tx_elem, "transactionAmounts/transactionShares/value")
        price_raw = _text(tx_elem, "transactionAmounts/transactionPricePerShare/value")

        shares = _safe_float(shares_raw)
        price = _safe_float(price_raw)
        value = (shares * price) if (shares is not None and price is not None) else None

        # For derivative transactions, value from exercise price may differ
        if value is None and shares is not None:
            # Estimate value from market value field if present
            mkt_val = _safe_float(
                _text(tx_elem, "transactionAmounts/transactionTotalValue/value")
            )
            value = mkt_val

        transactions.append({
            "accession_no": accession_no,
            "cik": cik,
            "insider_name": insider_name,
            "insider_role": insider_role,
            "transaction_date": date_str,
            "transaction_type": code,
            "shares": shares,
            "price": price,
            "value": value,
            "is_10b51_plan": 1 if is_plan else 0,
        })

    for tx in root.findall(".//nonDerivativeTransaction"):
        _extract_tx(tx)
    for tx in root.findall(".//derivativeTransaction"):
        _extract_tx(tx)

    return transactions


# ── High-level sync ───────────────────────────────────────────────────────────

def sync_company(cik: str, ticker: str, verbose: bool = False) -> tuple[int, int]:
    """
    Sync all unprocessed Form 4 filings for one company.
    Returns (filings_checked, transactions_added).
    """
    # Register any new filings we haven't seen before
    try:
        all_filings = fetch_form4_filings(cik)
    except Exception as exc:
        if verbose:
            print(f"  [warn] Could not fetch filing list for {ticker}: {exc}")
        return 0, 0

    new_filings = 0
    for f in all_filings:
        if not db.filing_exists(f["accession_no"]):
            db.upsert_filing(f["accession_no"], cik, f["filed_date"], f.get("primary_doc"))
            new_filings += 1

    # Process unprocessed filings
    unprocessed = db.get_unprocessed_filings(cik)
    tx_added = 0
    for filing in unprocessed:
        acc = filing["accession_no"]
        txs = parse_form4(cik, acc, filing["primary_doc"])
        if txs:
            db.insert_transactions(txs)
            tx_added += len(txs)
        db.mark_filing_processed(acc)

    # Update sync timestamp
    db.mark_company_synced(cik, datetime.utcnow().isoformat())
    return len(unprocessed), tx_added
