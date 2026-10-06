# tender-pipeline

Finds companies that win Indian public tenders, looks up a public email and phone, and emails them once. Three stages share one database, `data/pipeline.sqlite3` (created by setup, never committed).

```text
  01-scrape          02-enrich            03-outreach
  ─────────          ─────────            ───────────
  eprocure.gov.in    company name     →   gmail / graph
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

## Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/). From this folder, uv reads `.python-version`, downloads Python 3.13 if needed, and installs the lockfile into `.venv`:

```powershell
uv sync --all-packages --frozen
uv run --all-packages pipeline-db init
```

`--all-packages` installs every stage, including stage 3's libraries. `--frozen` installs `uv.lock` as committed. `.venv` stays off git. `requirements.txt` is the old install list; sync does not read it.

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
| `03-outreach\.env` | A mailbox: Microsoft 365 (Outlook, the default) or `GMAIL_USER` and `GMAIL_APP_PASSWORD`. The letter is edited on the Mail page. See [03-outreach/README.md](03-outreach/README.md) |

## Windows

[Git](https://git-scm.com/install/windows) and [uv](https://docs.astral.sh/uv/getting-started/installation/) must be installed. Three `.bat` files in this folder are the clickable form of the commands above. Each one changes to its own folder, so a desktop shortcut still runs this project. An editor does not need to be open.

| File | When |
| --- | --- |
| `setup.bat` | Once, after clone. Runs `uv sync --all-packages --frozen` and `pipeline-db init`. |
| `start-ui.bat` | Opens the UI. Starts `ops-ui` in a window titled **Tender pipeline** and opens <http://127.0.0.1:8000> after a short wait. Leave that window open. Closing it stops the UI. |
| `update.bat` | Gets new code with `git pull origin main`, then runs the same install and database update as `setup.bat`. Close the Tender pipeline window first. |

The first clone is still a command. The `.bat` files arrive with the repo:

```powershell
git clone <repository url>
cd tender-pipeline
```

Double-click `setup.bat`, fill in the `.env` files, then double-click `start-ui.bat`.

`update.bat` pulls the `main` branch from the remote named `origin`. Change that line in the file if this checkout uses another remote or branch. `.env`, `.venv`, and `data/` are gitignored, so a pull leaves secrets and the database in place. `pipeline-db init` on an existing database updates the schema and keeps the rows. If a step fails, the window stays open.

## Run

Each stage is configured by the `SETTINGS` or `CONFIG` block at the top of its `main.py`.

```powershell
cd 01-scrape      ; uv run --all-packages python main.py
cd ..\02-enrich   ; uv run --all-packages python main.py
cd ..\03-outreach ; uv run --all-packages python main.py
uv run --all-packages pipeline-db status
uv run --all-packages ops-ui
```

Then open <http://127.0.0.1:8000>. On Windows, `start-ui.bat` runs that command and opens the browser. The window listens on this computer only. It does not send mail.

A first run is cheap and sends nothing:

| Stage | Default | What it spends |
| --- | --- | --- |
| 01-scrape | `MAX_TENDERS = 1` | one OpenRouter call, to read the captcha |
| 02-enrich | `DRY_RUN = True` | nothing; it lists the queue and stops |
| 03-outreach | `SEND = False` | nothing; it writes `previews\*.txt` |

Raise those limits in the stage's `main.py`. Stage 2 with `DRY_RUN = False` spends one model call per company, up to `LIMIT`. Stage 3 with `SEND = True` emails real companies. Send only after the order in [03-outreach/README.md](03-outreach/README.md): previews, one message to yourself, then the real run. A live send refuses to start while the letter is incomplete.

`pipeline-db status` shows the queues. `import-xlsx` loads companies from a spreadsheet. `prune` and `reset` delete data and ask first.

## Tests

No network, no API keys, no mail. Each suite uses its own throwaway database.

```powershell
cd core           ; uv run --all-packages python -m pytest -q
cd ..\01-scrape   ; uv run --all-packages python -m pytest -q
cd ..\02-enrich   ; uv run --all-packages python -m pytest -q
cd ..\03-outreach ; uv run --all-packages python -m pytest -q
cd ..\ui          ; uv run --all-packages python -m pytest -q
```

If a command is using the wrong Python, `uv run python -c "import sys; print(sys.prefix)"` should end in `tender-pipeline\.venv`. Do not run `uv init` in this repo or in a parent folder.
