# stage1-scrape

Collects **Award of Contract (AOC)** records from India's public procurement portal
[eprocure.gov.in](https://eprocure.gov.in) and writes the **awarded vendor** (the winner,
not the buyer) into the pipeline's shared database.

This is **stage 1 of three**:

```text
01-scrape  ──►  02-enrich  ──►  03-outreach
you are here    finds email      sends the mail
                and phone
```

It stops at the portal. Looking up a company's contact details is stage 2's job — see
the [root README](../README.md) for the pipeline as a whole.

There is no public API. The tool walks the same Tender Status pages a person would:
search form → captcha → AOC listing → status page → stage summary → PDFs.

Python **3.11+**. Package: `stage1_scrape`. Command: `stage1-scrape`.

Design notes: [docs/architecture.md](docs/architecture.md) ·
portal steps: [docs/data-flow.md](docs/data-flow.md) ·
scaling: [docs/scaling.md](docs/scaling.md)

### Start here

Do the one-time setup in the [root README](../README.md) first (one shared `.venv` for
the whole pipeline). Then, in this folder:

```powershell
copy .env.example .env             # then set OPENROUTER_API_KEY (captcha OCR only)
uv run python main.py              # scrape 1 tender, then write vendors.csv
```

`main.py` is the whole product. Change limits in the `SETTINGS` block at the top of
`main.py` (default: 1 tender). Do not commit `.env` or `data/`.

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Environment variables](#environment-variables)
- [Quick start: `main.py`](#quick-start-mainpy)
- [Where the data goes](#where-the-data-goes)
- [Commands](#commands)
- [Repository layout](#repository-layout)
- [Runtime output](#runtime-output)
- [Tests](#tests)

---

## What it does

1. **Scrape** AOC tenders from GePNIC/Tapestry (`tenderStatus=6`).
2. **Parse** listing, bids, awarded rows, contract value, PDF links.
3. **Save** JSON and PDFs under `data/` (gitignored), and `tenders` / `vendors` /
   `awards` rows into the shared database.
4. **Export** every stored vendor to `data/exports/vendors.csv` / `.xlsx`.

The buyer field (`organisation_chain`, e.g. VNIT) is context only — never treat the buyer
as the vendor.

Captcha is 6 characters from `img#captchaImage`. `tokenSecret` is a hidden session field
— **not** the captcha.

---

## Requirements

- The pipeline's shared `.venv`, from the [root README](../README.md) setup. It installs
  this package and `pipeline-core` (`../core`) in editable mode, so a change to either is
  picked up immediately.
- An [OpenRouter](https://openrouter.ai/keys) key, used **only** to read the captcha.
  Set `OCR_ATTEMPTS = 0` in `main.py` to type it yourself and need no key at all.

---

## Environment variables

Copy `.env.example` to `.env`. Stage 1 talks to exactly one external service.

| Variable | Used for | Required |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | Captcha OCR | Yes, unless you type the captcha |
| `OPENROUTER_OCR_MODEL` | Vision model for the captcha | No (`google/gemini-2.5-flash`) |
| `OPENROUTER_BASE_URL` | Override the API host | No |
| `OPENROUTER_HTTP_REFERER` / `OPENROUTER_APP_TITLE` | OpenRouter usage attribution | No |

Stage 2's model keys and stage 3's mail credentials live in **their own** `.env` files.
Do not add them here.

The database path is not an env var by default — `pipeline_core` finds
`<pipeline root>/data/pipeline.sqlite3` on its own. Set `PIPELINE_DB` to point somewhere
else (the test suite does exactly that).

---

## Quick start: `main.py`

```powershell
uv run python main.py
```

Four steps, printed as they run:

| Step | What happens |
| --- | --- |
| 1 | Preflight — is the key present, which OCR model |
| 2 | Scrape the portal (OCR the captcha, fall back to typing it) |
| 3 | Store what was scraped |
| 4 | Export `data/exports/vendors.csv` / `.xlsx` |

Tuning, all in the `SETTINGS` block at the top of the file:

| Goal | Change |
| --- | --- |
| Fetch more tenders | `MAX_TENDERS = 5` |
| Look further back | `MAX_PAGES = 2` |
| Only a date window | `FROM_DATE = "01/07/2025"`, `TO_DATE = "31/12/2025"` (dd/MM/yyyy) |
| Check a date window took, fetching nothing | `PROBE = True` (same one captcha call) |
| Re-fetch something already saved | `REFRESH = True` |
| Skip PDF downloads (faster, loses PDF contacts) | `DOWNLOAD_PDFS = False` |
| Type the captcha yourself, no API key | `OCR_ATTEMPTS = 0` |

---

## Where the data goes

Rows go to the pipeline's shared database at `../data/pipeline.sqlite3`, defined once in
[`../core`](../core). This project owns:

| Table | Stage 1 writes |
| --- | --- |
| `tenders` | every column |
| `awards` | every column |
| `vendors` | identity: `name_raw`, `name_norm`, `legal_form`, `city`, `state`, `buyer_hint`, `source` |
| `vendors` | evidence: `pdf_email`, `pdf_phone`, lifted out of the work-order PDF |

It deliberately **never** writes `vendors.email`, `phone`, `enrichment_status` or
`enriched_at`. Those belong to stage 2, and overwriting them would throw away results
that cost real money. New vendors are left `enrichment_status = 'pending'`, which is
exactly how stage 2 finds its work.

`pdf_email` and `pdf_phone` are the exception, and they are **evidence, not answers**.
Work orders are scans, so OCR mangles them — a scan can read
`acmctechworks0l@example.com` for `acmetechworks01@example.com`. Stage 2 passes them to the
model to confirm or correct; nothing mails them directly. They are only attributed when
a tender had exactly one winner, since a work order names one firm.

`name_norm` is the unique key. It comes from `pipeline_core.naming.normalize_name`,
shared by every stage — change those rules and one company becomes two rows.

Inspect the database with:

```powershell
uv run pipeline-db status
```

---

## Commands

All of these need the pipeline's shared venv. Either activate it once
(`..\.venv\Scripts\Activate.ps1`) and use `python` as shown, or prefix each with
`uv run`.

### Help

```powershell
python -m stage1_scrape --help
```

### Full run (1 tender)

```powershell
python -m stage1_scrape run --out data --max-tenders 1
```

OCRs the captcha, scrapes, then rewrites `data/exports/vendors.csv`.

### Step by step (type the captcha)

```powershell
python -m stage1_scrape probe --out data
# read the 6 characters from data\captcha.png
python -m stage1_scrape scrape --out data --from-probe --captcha ABC123 --max-tenders 1
```

`--from-probe` reuses the probe's session so the captcha matches the form. A fresh
session would invalidate it.

### Re-index saved HTML (no portal, no network)

```powershell
python -m stage1_scrape index --out data
```

Re-parses everything under `data/debug/` back through the parsers and into the database.
The best way to check a parser change against real markup.

### Useful scrape flags

| Flag | Effect |
| --- | --- |
| `--max-pages N` | walk N listing pages (10 rows each) |
| `--max-tenders N` | stop after N tenders |
| `--refresh` | re-fetch tenders already in the database |
| `--no-pdfs` | skip PDF downloads |
| `--from-date` / `--to-date` | `dd/MM/yyyy` filters |
| `--keyword` | portal keyword filter |
| `--delay` | seconds between requests — do not lower |

---

## Repository layout

```text
.
├── main.py                     # one-command runner (constants block, no argparse)
├── pyproject.toml              # package metadata; dependencies install from ../requirements.txt
├── .env.example                # secret template (commit this)
├── AGENTS.md                   # notes for coding agents
├── docs/
│   ├── architecture.md
│   ├── data-flow.md
│   └── scaling.md
├── src/stage1_scrape/
│   ├── __main__.py             # python -m stage1_scrape
│   ├── cli.py                  # probe / scrape / run / index
│   ├── config.py               # portal constants
│   ├── settings.py             # .env loaders
│   ├── app/
│   │   ├── pipeline.py         # scrape_aoc, probe_search_form, reindex_saved
│   │   └── run.py              # run_scrape: scrape then export
│   ├── domain/                 # models, errors, winner_rows
│   ├── scraping/               # HTTP client, forms, captcha, OCR
│   ├── parse/                  # listing / status / summary HTML
│   ├── persist/                # JSON, PDFs, the shared database
│   └── export/                 # vendor rows -> CSV/Excel
└── tests/
    ├── fixtures/               # frozen portal HTML
    └── test_*.py
```

---

## Runtime output

Created locally; **not** source, and gitignored:

```text
data/
  captcha.png
  session.pkl                 # cookies; do not commit
  json/<tender_id>.json
  pdfs/<tender_id>/*.pdf
  debug/                      # search / listing / status / summary HTML
  exports/vendors.csv         # 15 cols, scrape + PDF facts only
  exports/vendors.xlsx
```

The database tables themselves are in `../data/pipeline.sqlite3`, shared with the other
stages.

Do not invent emails, phone numbers or GSTINs. If a PDF does not contain one, the cell
stays `NA`.

---

## Tests

No live portal, no API keys, no network:

```powershell
uv run python -m pytest -q
```

Every test runs against its own throwaway database — `tests/conftest.py` redirects
`PIPELINE_DB` per test, so the suite can never touch real scraped data.

Fixtures live in `tests/fixtures/`. If portal markup drifts, save HTML under
`data/debug/` and promote a slice into a fixture.
