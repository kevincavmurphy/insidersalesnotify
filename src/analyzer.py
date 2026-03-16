"""
Core analysis logic: compare insider sales across two configurable time windows.
"""
from datetime import date, timedelta
from math import inf

from src import db


def _date_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def compare_windows(
    cik: str,
    ticker: str,
    name: str,
    recent_years: int = 1,
    baseline_years: int = 10,
    min_value: float = 0.0,
    exclude_10b51: bool = False,
) -> dict:
    """
    Compare insider sales in the most recent N years vs. the prior M years.

    Returns a dict with:
      triggered       bool   — True if recent > baseline
      ticker          str
      name            str
      recent_sales    float  — USD value of sales in recent window
      baseline_sales  float  — USD value of sales in baseline window
      ratio           float  — recent / baseline (inf if baseline == 0)
      unique_sellers  int    — distinct insiders in recent window
      last_sale_date  str|None
    """
    today = date.today()
    recent_start = today - timedelta(days=recent_years * 365)
    baseline_start = today - timedelta(days=(recent_years + baseline_years) * 365)

    recent = db.get_sale_totals(
        cik,
        _date_str(recent_start),
        _date_str(today),
        exclude_10b51=exclude_10b51,
    )
    baseline = db.get_sale_totals(
        cik,
        _date_str(baseline_start),
        _date_str(recent_start),
        exclude_10b51=exclude_10b51,
    )

    recent_val = recent["total_value"] or 0.0
    baseline_val = baseline["total_value"] or 0.0

    ratio = (recent_val / baseline_val) if baseline_val > 0 else (inf if recent_val > 0 else 0.0)

    return {
        "ticker": ticker,
        "name": name,
        "cik": cik,
        "recent_sales": recent_val,
        "baseline_sales": baseline_val,
        "ratio": ratio,
        "unique_sellers": recent["unique_sellers"] or 0,
        "last_sale_date": recent["last_sale_date"],
        "triggered": recent_val > baseline_val and recent_val >= min_value,
    }


def scan_all(
    recent_years: int = 1,
    baseline_years: int = 10,
    min_value: float = 0.0,
    exclude_10b51: bool = False,
    sort_by: str = "ratio",
    limit: int = 50,
) -> list[dict]:
    """
    Run compare_windows for every company in the DB.
    Returns only companies where triggered=True, sorted by sort_by descending.
    """
    companies = db.get_all_companies()
    results = []

    for company in companies:
        result = compare_windows(
            cik=company["cik"],
            ticker=company["ticker"],
            name=company["name"],
            recent_years=recent_years,
            baseline_years=baseline_years,
            min_value=min_value,
            exclude_10b51=exclude_10b51,
        )
        if result["triggered"]:
            results.append(result)

    # Sort
    def sort_key(r: dict):
        val = r[sort_by]
        return val if val != inf else float("1e18")

    results.sort(key=sort_key, reverse=True)
    return results[:limit]


def company_transactions(
    cik: str,
    since_days: int = 3650,
) -> list[dict]:
    """
    Return all sale transactions for a company, newest first.
    since_days defaults to ~10 years.
    """
    since = _date_str(date.today() - timedelta(days=since_days))
    rows = db.get_transactions_for_company(cik, since)
    return [dict(r) for r in rows]
