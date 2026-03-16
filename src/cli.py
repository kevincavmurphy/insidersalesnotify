"""
CLI entrypoint for insidersalesnotify.

Usage:
    python -m src.cli sync [--ticker AAPL]
    python -m src.cli scan [--recent 1] [--baseline 10] [--min-value 1000000]
    python -m src.cli company TSLA [--recent 1] [--baseline 10]
"""
import sys
from math import inf, isinf

import click
from rich.console import Console
from rich.table import Table
from rich import box

from src import db, edgar, analyzer

console = Console()


def _fmt_usd(val: float | None) -> str:
    if val is None or val == 0:
        return "$0"
    if val >= 1_000_000_000:
        return f"${val/1_000_000_000:.1f}B"
    if val >= 1_000_000:
        return f"${val/1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val/1_000:.0f}K"
    return f"${val:,.0f}"


def _fmt_ratio(ratio: float) -> str:
    if isinf(ratio):
        return "∞"
    return f"{ratio:.2f}x"


# ── sync ──────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Insider sales screener — surfaces companies with unusual insider selling."""
    db.init_db()


@cli.command()
@click.option("--ticker", default=None, help="Sync a single ticker (e.g. TSLA). Omit to sync all.")
@click.option("--days-back", default=None, type=int,
              help="Only fetch filings from the last N days (all if omitted).")
def sync(ticker, days_back):
    """Download/update Form 4 data from SEC EDGAR."""
    db.init_db()

    if ticker:
        # Single-company sync
        company = db.get_company_by_ticker(ticker)
        if company is None:
            # Try fetching company list first
            console.print(f"[yellow]Ticker {ticker!r} not in DB — refreshing company list…[/yellow]")
            _refresh_companies()
            company = db.get_company_by_ticker(ticker)

        if company is None:
            console.print(f"[red]Ticker {ticker!r} not found in SEC EDGAR company list.[/red]")
            sys.exit(1)

        console.print(f"Syncing [bold]{ticker}[/bold] ({company['name']})…")
        filings, txs = edgar.sync_company(company["cik"], ticker, verbose=True)
        console.print(
            f"  [green]Done.[/green] Processed [bold]{filings}[/bold] filings, "
            f"added [bold]{txs}[/bold] sale transactions."
        )
    else:
        # Full universe sync
        companies = db.get_all_companies()
        if not companies:
            console.print("Fetching full company list from EDGAR…")
            _refresh_companies()
            companies = db.get_all_companies()

        total = len(companies)
        console.print(f"Syncing [bold]{total:,}[/bold] companies. This may take a while…\n")

        with console.status("") as status:
            for i, company in enumerate(companies, 1):
                status.update(
                    f"[{i}/{total}] {company['ticker']} — {company['name'][:40]}"
                )
                try:
                    edgar.sync_company(company["cik"], company["ticker"])
                except Exception as exc:
                    console.print(f"  [dim red]Error syncing {company['ticker']}: {exc}[/dim red]")

        console.print("\n[green]Sync complete.[/green]")


def _refresh_companies():
    console.print("Downloading company list from EDGAR…")
    rows = edgar.fetch_all_companies()
    db.upsert_companies(rows)
    console.print(f"  Loaded [bold]{len(rows):,}[/bold] companies.")


# ── scan ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--recent", default=1, show_default=True, type=int,
              help="Length of the recent window in years.")
@click.option("--baseline", default=10, show_default=True, type=int,
              help="Length of the baseline (comparison) window in years.")
@click.option("--min-value", default=0.0, show_default=True, type=float,
              help="Minimum recent sales value in USD to include.")
@click.option("--exclude-10b51", is_flag=True, default=False,
              help="Exclude sales filed under a Rule 10b5-1 plan.")
@click.option("--limit", default=50, show_default=True, type=int,
              help="Maximum number of results to display.")
@click.option("--sort", default="ratio", show_default=True,
              type=click.Choice(["ratio", "recent_sales"]),
              help="Sort results by ratio or absolute recent sales value.")
def scan(recent, baseline, min_value, exclude_10b51, limit, sort):
    """
    Screen all synced companies. Lists those where insider sales in the
    recent window exceed the baseline window.
    """
    console.print(
        f"\nScanning: last [bold]{recent}y[/bold] vs prior [bold]{baseline}y[/bold]"
        + (f" · min ${min_value:,.0f}" if min_value else "")
        + (" · excl. 10b5-1" if exclude_10b51 else "")
        + "\n"
    )

    results = analyzer.scan_all(
        recent_years=recent,
        baseline_years=baseline,
        min_value=min_value,
        exclude_10b51=exclude_10b51,
        sort_by=sort,
        limit=limit,
    )

    if not results:
        console.print("[yellow]No companies matched the criteria.[/yellow]")
        console.print(
            "\n[dim]Tip: run [bold]sync[/bold] first to load data, "
            "or broaden your filter parameters.[/dim]"
        )
        return

    table = Table(
        title=f"Insider Sales Alert — {len(results)} companies",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
    )
    table.add_column("Ticker", style="bold cyan", no_wrap=True)
    table.add_column("Company", max_width=35)
    table.add_column(f"Last {recent}y Sales", justify="right", style="red")
    table.add_column(f"Prior {baseline}y Sales", justify="right")
    table.add_column("Ratio", justify="right", style="bold yellow")
    table.add_column("Sellers", justify="right")
    table.add_column("Last Sale", no_wrap=True)

    for r in results:
        table.add_row(
            r["ticker"],
            r["name"][:35],
            _fmt_usd(r["recent_sales"]),
            _fmt_usd(r["baseline_sales"]),
            _fmt_ratio(r["ratio"]),
            str(r["unique_sellers"]),
            r["last_sale_date"] or "—",
        )

    console.print(table)
    console.print(
        f"\n[dim]Tip: run [bold]company TICKER[/bold] for a full transaction breakdown.[/dim]"
    )


# ── company ───────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("ticker")
@click.option("--recent", default=1, show_default=True, type=int,
              help="Recent window in years (for summary stats).")
@click.option("--baseline", default=10, show_default=True, type=int,
              help="Baseline window in years (for summary stats).")
@click.option("--exclude-10b51", is_flag=True, default=False,
              help="Exclude 10b5-1 plan sales from summary.")
def company(ticker, recent, baseline, exclude_10b51):
    """Deep-dive on a single company — show summary + all insider sale transactions."""
    row = db.get_company_by_ticker(ticker)
    if row is None:
        console.print(
            f"[red]Ticker {ticker!r} not found. Run [bold]sync --ticker {ticker}[/bold] first.[/red]"
        )
        sys.exit(1)

    cik = row["cik"]
    name = row["name"]

    summary = analyzer.compare_windows(
        cik=cik,
        ticker=ticker,
        name=name,
        recent_years=recent,
        baseline_years=baseline,
        exclude_10b51=exclude_10b51,
    )

    # Header
    status_color = "red" if summary["triggered"] else "green"
    status_label = "ALERT" if summary["triggered"] else "Normal"
    console.print(f"\n[bold]{ticker}[/bold] — {name}")
    console.print(
        f"Status: [{status_color}][bold]{status_label}[/bold][/{status_color}]  |  "
        f"Recent {recent}y: [red]{_fmt_usd(summary['recent_sales'])}[/red]  |  "
        f"Prior {baseline}y: {_fmt_usd(summary['baseline_sales'])}  |  "
        f"Ratio: [yellow]{_fmt_ratio(summary['ratio'])}[/yellow]  |  "
        f"Unique sellers (recent): {summary['unique_sellers']}\n"
    )

    # Transaction table
    txs = analyzer.company_transactions(cik, since_days=(recent + baseline) * 365)
    if not txs:
        console.print("[dim]No sale transactions found in DB for this company.[/dim]")
        return

    table = Table(
        title=f"Insider Sales — {ticker}",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
    )
    table.add_column("Date", no_wrap=True)
    table.add_column("Insider", max_width=30)
    table.add_column("Role", max_width=25)
    table.add_column("Type", justify="center")
    table.add_column("Shares", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Value", justify="right", style="red")
    table.add_column("10b5-1", justify="center")

    for tx in txs:
        value = _fmt_usd(tx["value"]) if tx["value"] else "—"
        price = f"${tx['price']:.2f}" if tx["price"] else "—"
        shares = f"{int(tx['shares']):,}" if tx["shares"] else "—"
        is_plan = "✓" if tx["is_10b51_plan"] else ""

        table.add_row(
            tx["transaction_date"],
            (tx["insider_name"] or "")[:30],
            (tx["insider_role"] or "")[:25],
            tx["transaction_type"],
            shares,
            price,
            value,
            is_plan,
        )

    console.print(table)


# ── companies (utility) ───────────────────────────────────────────────────────

@cli.command("refresh-companies")
def refresh_companies():
    """Re-download the full list of companies from SEC EDGAR."""
    _refresh_companies()


if __name__ == "__main__":
    cli()
