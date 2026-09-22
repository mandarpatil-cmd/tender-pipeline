# Agent instructions — stage1-scrape (pipeline stage 1)

This project scrapes Award of Contract records from eprocure.gov.in and writes the
awarded **vendors** (not the buyer) into the pipeline's shared database.

It is stage 1 of three. Finding contact details for those vendors is stage 2
(`../02-enrich`); mailing them is stage 3 (`../03-outreach`). Nothing here calls a
model except captcha OCR — if a change wants to research a company, it belongs in
stage 2.

## How to work

1. Read `.cursor/rules/`.
2. Prefer existing packages: `scraping`, `parse`, `persist`, `export`. Do not scrape
   HTML outside `scraping/`, and do not write spreadsheets outside `export/`.
3. Keys live in `.env`, loaded through `stage1_scrape.settings`. Never in source.
4. Do not invent emails or phone numbers. A vendor with nothing found stays
   `enrichment_status = 'pending'` for stage 2 to pick up.

## Commands

All four projects share one `.venv` at the repo root (`../requirements.txt`). Run these
with it activated, or prefix each with `uv run`. Never create a venv in this folder.

```powershell
python main.py                 # scrape -> SQLite -> data/exports/vendors.csv
python -m stage1_scrape probe --out data
python -m stage1_scrape scrape --out data --from-probe --captcha <6-chars> --max-tenders 1
python -m stage1_scrape index --out data     # re-parse saved HTML, no network
python -m stage1_scrape run --out data --max-tenders 1
python -m pytest -q
```

`main.py` is a thin runner at the project root: a constants block, no argparse. It
imports `app.pipeline.scrape_aoc` and `export.vendor_sheet.write_vendor_sheet`. Put new
orchestration in `app/`, not in `main.py`.

## The shared database

Vendors, tenders and awards live in the pipeline's database, not in a private SQLite
file — see `../core/` for the schema and `../README.md` for the pipeline as a whole.

Stage 1 owns `tenders` and `awards` outright, and owns two groups of `vendors` columns:
the *identity* (`name_raw`, `name_norm`, `legal_form`, `city`, `state`, `buyer_hint`,
`source`) and the *evidence* (`pdf_email`, `pdf_phone`). It must never write `email`,
`phone`, `enrichment_status` or `enriched_at` — those belong to stage 2, and overwriting
them would throw away paid-for results.

`pdf_email` / `pdf_phone` come out of the scanned work order, so OCR mangles them. They
are a lead for stage 2 to confirm, never an address to mail, and they are only attributed
when a tender had exactly one winner — a work order names one firm.

`name_norm` is the unique key on `vendors`. It comes from
`pipeline_core.naming.normalize_name`, which every stage shares; changing those rules
splits one company into two rows.
