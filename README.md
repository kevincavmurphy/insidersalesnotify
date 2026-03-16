# insidersalesnotify

A Python CLI tool that screens public companies for unusual insider selling patterns — specifically where sales in a recent window (default: 1 year) exceed sales in a longer baseline window (default: 10 years). Useful for surfacing short-selling ideas.

Data is pulled from [SEC EDGAR](https://www.sec.gov/cgi-bin/browse-edgar) Form 4 filings (free, official, no API key required).

---

## Setup

```bash
pip install -r requirements.txt
```

**Update your User-Agent** in `src/edgar.py` before running (SEC policy requires a contact email):
```python
HEADERS = {
    "User-Agent": "insidersalesnotify your@email.com",
    ...
}
```

---

## Usage

### 1. Sync data from EDGAR

Sync a single ticker (fast, good for testing):
```bash
python -m src.cli sync --ticker TSLA
```

Sync all companies (slow on first run; incremental thereafter):
```bash
python -m src.cli sync
```

### 2. Scan for alerts

Find companies where last 1 year of insider sales exceeds the prior 10 years:
```bash
python -m src.cli scan
```

Customize the windows and filters:
```bash
python -m src.cli scan --recent 2 --baseline 5 --min-value 1000000 --exclude-10b51
```

Options:
| Flag | Default | Description |
|---|---|---|
| `--recent` | `1` | Recent window in years |
| `--baseline` | `10` | Baseline (comparison) window in years |
| `--min-value` | `0` | Minimum recent sales in USD |
| `--exclude-10b51` | off | Exclude pre-scheduled 10b5-1 plan sales |
| `--limit` | `50` | Max results shown |
| `--sort` | `ratio` | Sort by `ratio` or `recent_sales` |

### 3. Deep-dive on a company

```bash
python -m src.cli company TSLA
python -m src.cli company TSLA --recent 2 --baseline 8
```

---

## Transaction types captured

| Code | Description |
|---|---|
| `S` | Open market sale |
| `AS` | Automatic sale (Rule 10b5-1 plan) |
| `OS` | Other sale (tender offer, private transaction) |
| `JS` | Sale from convertible/derivative instrument |

Acquisitions (`A`), option exercises (`M`), gifts, and inheritances are excluded.

---

## Notes

- **First sync is slow** — EDGAR rate-limits at ~10 req/sec. Full universe (~10K companies × 10+ years) takes several hours. Sync individual tickers while exploring.
- **Dollar value** is used for all comparisons (not share count).
- **Data is cached** in `data/insider.db` (SQLite). Subsequent syncs are incremental.
- **10b5-1 plans** are included by default (use `--exclude-10b51` to remove pre-scheduled sales).
