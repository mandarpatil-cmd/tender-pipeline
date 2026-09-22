# tender-pipeline

Finds companies that win Indian public tenders, works out how to contact them, and emails
them. Three stages share one database.

```text
  01-scrape          02-enrich            03-outreach
  ─────────          ─────────            ───────────
  eprocure.gov.in    company name     →   gmail / graph
      │              email + phone            │
      ▼                   │                   ▼
  tenders                 ▼                outreach
  vendors ──────────► vendors.email ─────►  (sent log)
  awards              llm_runs
      │                   │                   │
      └───────────────────┴───────────────────┘
                 data/pipeline.sqlite3
```

| Path | What it is |
| --- | --- |
| [`01-scrape/`](01-scrape/) | Scrapes Award of Contract records from eprocure.gov.in |
| [`02-enrich/`](02-enrich/) | Asks a model (with web search) for each company's public email and phone |
| [`03-outreach/`](03-outreach/) | Emails every company that has an address, once |
| [`core/`](core/) | The shared database schema and the `pipeline-db` command |
| `data/` | Created by setup: `pipeline.sqlite3`, the shared database (never committed) |
| `requirements.txt` | Every dependency, for the one shared virtualenv |

Each stage has its own README with the details.

---

## 1. Setup (once)

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/). It downloads Python
3.13 by itself if you don't have it. From this folder:

```powershell
uv venv --python 3.13
uv pip install -r requirements.txt
uv run pipeline-db init
```

This creates one `.venv` here that all three stages share, and an empty database in
`data\`.

Then give each stage its settings file:

```powershell
copy 01-scrape\.env.example   01-scrape\.env
copy 02-enrich\.env.example   02-enrich\.env
copy 03-outreach\.env.example 03-outreach\.env
```

and fill them in:

| File | Fill in | Get it from |
| --- | --- | --- |
| `01-scrape\.env` | `OPENROUTER_API_KEY` (reads the portal's captcha) | <https://openrouter.ai/keys> |
| `02-enrich\.env` | `OPENROUTER_API_KEY`, `MODEL` | the same account |
| `03-outreach\.env` | `SENDER_NAME`, `SENDER_ORG`, and a mailbox: `GMAIL_USER` + `GMAIL_APP_PASSWORD`, or the Microsoft 365 values | [03-outreach/README.md](03-outreach/README.md) |

`.env` files hold secrets and are never committed. Each stage reads only its own.

---

## 2. Run

Each stage is one command. It's configured by a `SETTINGS` / `CONFIG` block at the top of
its `main.py`, so there are no flags to remember.

```powershell
cd 01-scrape     ; uv run python main.py   # scrape the portal
cd ..\02-enrich  ; uv run python main.py   # find contacts for new companies
cd ..\03-outreach; uv run python main.py   # preview (or send) the emails
```

Check the database at any point, from any folder:

```powershell
uv run pipeline-db status
```

### What the first run does

Everything is set so that a first run is cheap and sends nothing:

| Stage | Default | Costs / reaches |
| --- | --- | --- |
| 01-scrape | fetches **1** tender (`MAX_TENDERS = 1`) | one OpenRouter call, to read the captcha |
| 02-enrich | `DRY_RUN = True`: lists who is queued and stops | nothing |
| 03-outreach | `SEND = False`: writes `previews\*.txt` and mails nobody | nothing |

### Going further

| To... | Change |
| --- | --- |
| Scrape more | `01-scrape/main.py`: raise `MAX_TENDERS` / `MAX_PAGES`, or set `FROM_DATE` / `TO_DATE` |
| Research companies | `02-enrich/main.py`: `DRY_RUN = False`. Keep `LIMIT = 1` for the first real run, then raise it |
| Send the emails | See **Before the first real send** below |

### What costs money or reaches people

| Action | Cost |
| --- | --- |
| Any stage 1 run | one OpenRouter call to read the captcha |
| Stage 2 with `DRY_RUN = False` | one paid model call (plus web search) **per company**, up to `LIMIT` |
| Stage 3 `PREFLIGHT = True` | one real email, to your own mailbox |
| Stage 3 `SEND = True` | real email to real companies |
| `pipeline-db reset` / `prune` | deletes data (asks first) |

---

## 3. Before the first real send

1. **Write the pitch.** The body in `03-outreach/campaign.py` (`build_body`) still has a
   placeholder paragraph: `<-- replace this paragraph with your actual pitch -->`.
2. **Set the signature.** Put `SENDER_NAME` and `SENDER_ORG` in `03-outreach\.env`.
3. **Test on yourself.** Follow the order in [03-outreach/README.md](03-outreach/README.md).
   It goes preflight, then previews, then one message redirected to you, then the real
   thing.

A live send refuses to start until 1 and 2 are done. Previews work regardless, so you can
review the text first.

**Live status:** stages 1 and 2 have been run against the real portal and OpenRouter.
Stage 3 has not sent a real campaign yet: its mail transports are tested against a fake,
so do step 3 carefully the first time.

---

## How the stages share the database

Five tables. Each one has a single writer, except `vendors`, and there the two writers
touch different columns:

```text
                 tenders  vendors  awards  llm_runs  outreach
01-scrape  read     *        .        .        .         .
           write    *      * (id)     *        .         .
02-enrich  read     .        *        *        .         .
           write    .    * (contact)  .        *         .
03-outreach read    .        *        .        .         *
           write    .        .        .        .         *
```

- **Stage 1 owns a company's identity** (`name_raw`, `name_norm`, `legal_form`, `city`,
  `state`, `buyer_hint`, `source`). It also owns the *evidence* it finds in work-order
  PDFs (`pdf_email`, `pdf_phone`).
- **Stage 2 owns the contact block** (`email`, `phone`, `enrichment_status`,
  `enriched_at`). Re-scraping a tender can never wipe a contact that cost money to find.
- **Evidence is not an answer.** Work orders are scans, and OCR mangles characters. A
  PDF can read `acmctechworks0l@example.com` when the real address is
  `acmetechworks01@example.com`. So stage 2 hands PDF contacts to the model as a lead to
  confirm, and stage 3 only ever mails `vendors.email`.
- **`name_norm` is the unique key**, from `pipeline_core.naming.normalize_name`. Every
  stage uses that one function. Change its rules and one company becomes two rows.

`vendors.enrichment_status` is stage 2's work queue:

```text
pending ──► done       an email and/or phone was found
        ├─► not_found  the call worked; nothing is public. Not retried
        └─► failed     the call errored. Safe to retry
```

`pipeline-db` commands (run as `uv run pipeline-db <command>`):

```text
init          create the schema, or bring it up to date
status        row counts, the work queues, and what each stage would do next
import-xlsx   insert companies from a spreadsheet as vendors
adopt <file>  take an existing SQLite file as the shared database
prune         delete every vendor from one source (asks first)
reset         DROP everything and recreate it empty (asks first)
```

---

## Tests

No network, no API keys, no mail. Each suite runs against its own throwaway database, so
it can never touch real data.

```powershell
cd core          ; uv run python -m pytest -q
cd ..\01-scrape  ; uv run python -m pytest -q
cd ..\02-enrich  ; uv run python -m pytest -q
cd ..\03-outreach; uv run python -m pytest -q
```
