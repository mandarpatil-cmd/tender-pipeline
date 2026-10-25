# tender-pipeline

Finds companies that win Indian public tenders, looks up a public email and phone, and emails them once. Three stages share one database, `data/pipeline.sqlite3` (created by setup, never committed).

```text
  01-scrape          02-enrich            03-outreach
  ─────────          ─────────            ───────────
  eprocure.gov.in    company name     →   outlook / gmail / graph
      │              email + phone            │
      ▼                   │                   ▼
  tenders                 ▼                outreach
  vendors ──────────► vendors.email ─────►  (sent log)
  awards              llm_runs
```

| Path | What it is |
| --- | --- |
| [`01-scrape/`](01-scrape/) | Scrapes Award of Contract records from eprocure.gov.in |
| [`02-enrich/`](02-enrich/) | Asks a model (with web search) for each company's public email and phone |
| [`03-outreach/`](03-outreach/) | Emails every company that has an address, once |
| [`core/`](core/) | Shared database schema, and the `pipeline-db` command |
| [`ui/`](ui/) | Local browser window. Reads the database and can start a scrape or a contact lookup |

Each stage has its own README.

## What the counts are

The Awards page is one row per award. The pager (`1–50 of N`) is that same award count. Both are winning companies on tenders, so N matches the **awards** card and will not match **tenders** or **vendors**.

| Count | What it counts |
| --- | --- |
| tenders | One contract from the portal. Two winners on it are still one tender. |
| awards | One winning company on one tender. This is a table row. |
| vendors | The company, once, however many tenders it won. |
| enriched | Companies where a contact lookup found an email or a phone. |
| mailable | Companies with a contact email. These are the ones that can be mailed. |
| phone-only | A phone and no email. Counted as enriched, and never mailed. |
| sent | Companies that already have a successful send. |

A tender with two awarded bidders is two awards and two rows. A company that wins two tenders is two awards, two rows, and one vendor. The scraper keeps the portal's awarded-bid list for that tender. When that list is empty it keeps the L1 bid, then a bid marked Accepted. It does not keep every bidder.

A filter shrinks every card together. "Counts match the rows in this filter" describes the awards on screen, not the whole file. Tender status AOC plus enrichment pending is a common example: the file still holds the other statuses, and they stay off this view until the filter is cleared.

## Who the mail goes to

Mail uses every valid address for that company and tender. A PDF clue goes first, then the address the contact lookup stored, then an address a person typed. A government or buyer address is not mailed. A phone alone is not a recipient.

One letter goes to one address about one tender. The same company with two addresses gets two letters. The same company with two wins gets a letter for each tender. An address already mailed for that tender is skipped. A different address on the same tender is not. Filling "Send every message to" delivers the letter to that inbox and records a test, so a real send can still go out. Preview writes a file and sends nothing. Send is the action that mails the company.

## Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/). From this folder, uv reads `.python-version`, downloads Python 3.13 if needed, and installs the lockfile into `.venv`:

```powershell
uv sync --frozen
uv run pipeline-db init
```

`uv sync --frozen` installs `uv.lock`, including mail and spreadsheets. `.venv` stays off git.

Copy each example settings file and fill it in. `.env` files are secrets. Each stage reads only its own.

```powershell
copy 01-scrape\.env.example   01-scrape\.env
copy 02-enrich\.env.example   02-enrich\.env
copy 03-outreach\.env.example 03-outreach\.env
```

| File | Fill in |
| --- | --- |
| `01-scrape\.env` | `OPENROUTER_API_KEY`, used to read the portal captcha. <https://openrouter.ai/keys> |
| `02-enrich\.env` | `OPENROUTER_API_KEY` and `MODEL`, same account |
| `03-outreach\.env` | A mailbox: `OUTLOOK_USER` and `OUTLOOK_PASSWORD` (Microsoft 365, the default), `GMAIL_USER` and `GMAIL_APP_PASSWORD`, or Microsoft Graph as a fallback. The letter is edited on the Mail page. See [03-outreach/README.md](03-outreach/README.md) |

## Windows

[Git](https://git-scm.com/install/windows) and [uv](https://docs.astral.sh/uv/getting-started/installation/) must be installed. Three `.bat` files in this folder are the clickable form of the commands above. Each one changes to its own folder, so a desktop shortcut still runs this project. An editor does not need to be open.

| File | When |
| --- | --- |
| `setup.bat` | Once, after clone. Runs `uv sync --frozen` and `pipeline-db init`. |
| `start-ui.bat` | Opens the UI. Starts `ops-ui` in a window titled **Tender pipeline** and opens <http://127.0.0.1:8000> after a short wait. Leave that window open. Closing it stops the UI. |
| `update.bat` | Gets new code with `git pull origin main`, runs `uv sync --frozen`, saves a checked copy of the database with `pipeline-db backup --label update`, then runs `pipeline-db init`. Close the Tender pipeline window first. |

The first clone is still a command. The `.bat` files arrive with the repo:

```powershell
git clone <repository url>
cd tender-pipeline
```

Double-click `setup.bat`, fill in the `.env` files, then double-click `start-ui.bat`.

`update.bat` pulls the `main` branch from the remote named `origin`. To pull from somewhere else, point the remote there with `git remote set-url origin <url>`. Do not edit the file's first lines. A running copy carries on at the same place in the file the pull writes, so the lines up to the pull must stay the same in every release. `.env`, `.venv`, and `data/` are gitignored, so a pull leaves secrets and the database in place. If a step fails, the window stays open.

### What an update does to the data

New code never rewrites stored rows on a page load. The first time new code opens the database, through `pipeline-db init` or the window, it does three things:

1. Adds the tables, columns, and indexes the new code needs. It does not change existing rows.
2. Runs each data fix the database has not had yet, once, and records it in `data_migrations`. `init` prints each one, for example `data    2026-10-contacts-v1 (12 rows)`.
3. Writes `PRAGMA user_version`. A database that newer code has already updated is refused, with nothing changed.

When there is any work for steps 1 or 2, a checked copy goes to `data/backups/` first. Every copy is made with SQLite's backup API and must pass `PRAGMA integrity_check`. The newest 10 copies of each kind are kept:

| Copy | Made by |
| --- | --- |
| `pipeline.sqlite3.update-<time>` | `update.bat`, before `init` |
| `pipeline.sqlite3.auto-<time>` | `init` or the window, before a schema change or a data fix |
| `pipeline.sqlite3.manual-<time>` | `uv run pipeline-db backup` |
| `pipeline.sqlite3.before-recheck-<time>` | `recheck-contacts --apply`, with the list of changes in `recheck-<time>.txt` |

If the data looks wrong after an update, close the Tender pipeline window and put the copy from before it back:

```powershell
uv run pipeline-db restore data\backups\pipeline.sqlite3.update-<time>
```

`restore` first copies the database it replaces to `data\pipeline.sqlite3.replaced-<time>`, so nothing is lost either way.

A change to the address rules does not touch stored addresses by itself. The mail queue applies the current rules each time it reads, so a government address is never mailed. `uv run pipeline-db recheck-contacts` lists the saved lookup answers the current rules would drop. It changes nothing until it is run again with `--apply`.

## Run

Each stage is configured by the `SETTINGS` or `CONFIG` block at the top of its `main.py`.

```powershell
cd 01-scrape      ; uv run python main.py
cd ..\02-enrich   ; uv run python main.py
cd ..\03-outreach ; uv run python main.py
uv run pipeline-db status
uv run ops-ui
```

Then open <http://127.0.0.1:8000>. On Windows, `start-ui.bat` runs that command and opens the browser. The window listens on this computer only. It does not send mail.

A first run is cheap and sends nothing:

| Stage | Default | What it spends |
| --- | --- | --- |
| 01-scrape | `MAX_TENDERS = 1` | one OpenRouter call, to read the captcha |
| 02-enrich | `DRY_RUN = True` | nothing; it lists the queue and stops |
| 03-outreach | `SEND = False` | nothing; it writes `previews\*.txt` |

Raise those limits in the stage's `main.py`. Stage 2 with `DRY_RUN = False` spends one model call per company, up to `LIMIT`. Stage 3 with `SEND = True` emails real companies. Send only after the order in [03-outreach/README.md](03-outreach/README.md): previews, one message to yourself, then the real run. A live send refuses to start while the letter is incomplete.

`pipeline-db status` shows the queues. `import-xlsx` loads companies from a spreadsheet. `backup` and `restore` copy the database out and back. `prune` and `reset` delete data and ask first.

## Tests

No network, no API keys, no mail. Each suite uses its own throwaway database. A test that tries to reach the network fails, and the real `.env` values are blanked while the tests run.

```powershell
cd core           ; uv run python -m pytest -q
cd ..\01-scrape   ; uv run python -m pytest -q
cd ..\02-enrich   ; uv run python -m pytest -q
cd ..\03-outreach ; uv run python -m pytest -q
cd ..\ui          ; uv run python -m pytest -q
```

The browser tests drive the window in Chromium. They are skipped by the commands above. Playwright is not in `uv.lock`, so `--with` adds it for this command only. The install line downloads Chromium once.

```powershell
cd ui
uv run --with pytest-playwright python -m playwright install chromium
uv run --with pytest-playwright python -m pytest tests/e2e -q
```

If a command is using the wrong Python, `uv run python -c "import sys; print(sys.prefix)"` should end in `tender-pipeline\.venv`. Do not run `uv init` in this repo or in a parent folder.
