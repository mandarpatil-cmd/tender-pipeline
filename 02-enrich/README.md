# stage2-enrich

Finds a public **email and phone** for each company waiting in the pipeline database,
and writes the answer back to that company's row.

This is **stage 2 of three**:

```text
01-scrape  ──►  02-enrich  ──►  03-outreach
finds the       you are here     sends the mail
companies
```

One model call per company, through OpenRouter, **with web search on**. Python
**3.11+**. Package: `stage2_enrich`.

---

## How it works

```text
vendors WHERE enrichment_status = 'pending'
        │
        ├─ company name + the work_title of a tender they won (context)
        ├─ pdf_email / pdf_phone, if the scraper found any (a lead to verify)
        ▼
   OpenRouter (LangChain, structured output -> ContactInfo)
        │
        ├─ email and/or phone  -> vendors.email / .phone, status 'done'
        ├─ both null           -> status 'not_found'   (never retried)
        └─ call errored        -> status 'failed'      (safe to retry)
        │
        └─ every call, win or lose, is appended to llm_runs
```

**Each vendor is committed on its own, the moment its answer arrives.** A crash, a rate
limit, or Ctrl-C at company 300 of 500 costs one call, not the run. Re-running simply
resumes — the queue *is* the state, so there is no checkpoint file to keep in sync.

---

## Quick start

Do the one-time setup in the [root README](../README.md) first (one shared `.venv` for
the whole pipeline). Then, in this folder:

```powershell
copy .env.example .env             # then set OPENROUTER_API_KEY and MODEL
uv run python main.py              # DRY_RUN = True by default: lists the queue, spends nothing
```

Check the queue at any time:

```powershell
uv run pipeline-db status
```

---

## Configuration

Everything is in the `CONFIG` block at the top of `main.py`. There are no flags.

| Setting | Default | What it does |
| --- | --- | --- |
| `DRY_RUN` | `True` | Print the queue and stop. Spends nothing. `False` = research for real |
| `LIMIT` | `1` | Companies to research this run. **Each one is a paid call.** `None` = every pending vendor |
| `DELAY` | `1.0` | Seconds between calls, to stay inside rate limits |
| `ONLY_SOURCE` | `"scrape"` | Only vendors the scraper found. `"bideasy"` = only imported spreadsheet rows; `None` = all |
| `RETRY_FAILED` | `True` | Put previously `failed` vendors back on the queue first |
| `RETRY_NOT_FOUND` | `False` | Also requeue `not_found`. Only after changing the method — new model, web search on, better evidence |

It starts as a dry run. When the queue looks right, set `DRY_RUN = False` with
`LIMIT = 1`, check that one answer, then raise `LIMIT`.

### Environment

`.env` in this folder (never committed):

| Variable | Example | |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | from <https://openrouter.ai/keys> | required |
| `MODEL` | `google/gemini-2.5-flash` | required |
| `WEB_SEARCH` | `1` | `0` turns searching off — see below |

### Web search is what makes this work

These are small regional contractors. A model has not memorised them, and their
details live on IndiaMART, JustDial, GST portals or their own sites. With search
off, a trial run returned null for **every** vendor — except the one that happened
to have a work-order PDF attached.

Searching is billed per result, on top of the model's tokens.
`settings.WEB_SEARCH_RESULTS` (default 3) caps results per call and is the main
cost dial; it multiplies across every vendor in the queue.

Each answer also carries the URL it came from, stored in `llm_runs.output_json`, so an
address can be traced back to where it was found.

Each stage keeps its own `.env`. The scraper's captcha key and the mailer's credentials
are not read from here.

---

## The four statuses

`vendors.enrichment_status` is the work queue, and the distinction between the last
three is what stops this stage burning money re-asking questions it already answered.

| Status | Meaning | Retried? |
| --- | --- | --- |
| `pending` | not looked at yet | — it *is* the queue |
| `done` | an email and/or phone was found | no |
| `not_found` | the call worked; the answer was "nothing public" | **no** — unless the *method* changed (see `RETRY_NOT_FOUND`) |
| `failed` | the call itself errored (timeout, 429, bad shape) | yes, while `RETRY_FAILED = True` (the default) |

A `failed` vendor keeps its blank contact columns, so a retry can fill them.

---

## What it does not do

- **It does not read spreadsheets.** Companies from a spreadsheet get into `vendors`
  with `uv run pipeline-db import-xlsx <file>`, after which they queue like any other.
- **It does not write spreadsheets.** Results go to the database, which is what stage 3
  reads.
- **It does not cache per company.** It cannot need to: the queue comes from `vendors`,
  which is unique on `name_norm`, so a company cannot appear twice.
- **It does not trust `pdf_email`.** Stage 1 lifts contacts out of scanned work orders,
  and OCR mangles them — `acmctechworks0l@` for `acmetechworks01@`. They go into the
  prompt as a lead to confirm or correct, never straight into `vendors.email`.

---

## Layout

```text
.
├── main.py                     # runner: CONFIG block, no argparse
├── pyproject.toml
├── .env.example
├── src/stage2_enrich/
│   ├── settings.py             # key, model, prompt version
│   ├── llm.py                  # ContactInfo, the prompt, build_chain
│   └── runner.py               # the queue loop, one commit per vendor
└── tests/
    └── test_runner.py          # a fake chain; no network, no keys, no spend
```

---

## Tests

```powershell
uv run python -m pytest -q
```

No network and no API key: the model is replaced by a fake chain, and each test gets its
own throwaway database via `PIPELINE_DB`. The tests pin the behaviour that matters —
commit-per-vendor, resume after an interrupt, and a failed call never taking the run
down with it.
