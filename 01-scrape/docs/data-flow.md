# Data flow

The portal is GePNIC/Tapestry. Hidden fields and cookies matter as much as the visible form. Confirmed against live HTML on 25 Aug 2026.

## Stop 0 — Search page

`GET /eprocure/app?page=WebTenderStatusLists&service=page`

- Parse **only** `#frmSearchFilter`. Ignore `#WebBorder_0`.
- Keep cookies (`JSESSIONID`).
- Hidden fields: `formids`, `seedids`, `tokenSecret`, `If_*`, `component`, `page`, `service`, `session`.
- Captcha is `img#captchaImage` (inline PNG) + `input#captchaText` (**6 characters**).
- `tokenSecret` looks like `S….` + `DDMMYYYYHHMMSS`. It is **not** the captcha.

Command: `python -m stage1_scrape probe --out data`

## Stop 1 — Search AOC

`POST /eprocure/app` with every field from Stop 0, plus:

| Field | Value |
| --- | --- |
| `tenderStatus` | `6` (AOC; not the text `"AOC"`) |
| `captchaText` | what you typed |
| `Search` | `Search` |

If the response still shows `#captchaImage` and no `title="View Tender Status"` links, the captcha was rejected. A new image is on that page.

`--from-probe` reuses `data/session.pkl` + `data/debug/search_form.html` so the captcha matches the form (probe and scrape must not be different sessions).

## Stop 2 — Listing

Ten rows. Each row: S.No, Tender ID, Title/Ref, Organisation, Stage, Status, plus an image-only link:

`a[title="View Tender Status"]` — plain GET, unique `sp=` per row.

`iterRows_0` appears here (duplicate hidden inputs). Extraction keeps them in order.

**Next** is still unconfirmed. If it is missing, markup is saved to `data/debug/pager_unconfirmed.html`.

## Stop 3 — Tender status

`GET` the listing href. Find:

`a[title="View the all stage summary Details"]`

`onclick` only affects the popup. The `href` is a working GET.

## Stop 4 — Stage summary (payoff page)

One table body, repeating:

`section_head` → header row → data rows. Bids List uses `tr.td_field`. Financial Evaluation Bid List and Awarded Bids List on the live portal often omit that class (`tr#informal_*` / `tr.even`); the parser still takes rows whose first cell is a serial number.

Sections: Bids List, Financial Evaluation Bid List, Awarded Bids List, plus key/value blocks (header, bid opening, tech/finance eval, AOC).

PDF links: label in `td.td_caption`, filename in nested `<b>`, href `FrontEndViewBidSummaryDetailsPrint` + `sp=`. Skip gzip `sp=ZH4sI…` blobs (Tapestry state, not files).

## Stop 5 — PDFs

`GET` each document URL. Body must start with `%PDF` (or Content-Type pdf). Expired `sp=` tokens return HTML.

## Output per tender

Listing fields, bids, financial ranking, awarded bids, AOC block, local PDF paths — written to `data/json/<tender_id>.json` and indexed in the shared database (`../data/pipeline.sqlite3`) as `tenders`, `vendors` and `awards` rows.

## Stop 6 — Spreadsheet

One export, written from the database rather than from the portal:

| File | Columns | Written by |
| --- | --- | --- |
| `data/exports/vendors.csv` / `.xlsx` | 15 — scrape + PDF facts | `main.py`, and `run` |

The sheet is a convenience view, not a handoff. Stage 2 reads the database, not this
file, and writes each contact it finds straight back to the `vendors` row.
