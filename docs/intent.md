# intent.md — Business Requirements Document

**Project:** tender-pipeline — Operations UI
**Version:** 0.3
**Status:** For approval

---

## 1. Purpose

The pipeline works today and is operated from a terminal: changing a run means
editing a Python file. The stakeholder wants a web UI so a non-technical
operator can see every tender and vendor in one table, filter and export it,
run each stage on demand, add records by hand, and send outreach in bulk or to
one recipient at a time.

This states those requirements and what "done" means. It is not a design.

---

## 2. What exists today

Three stages share one SQLite database at `data/pipeline.sqlite3`.

```text
  01-scrape          02-enrich            03-outreach
  eprocure.gov.in    email + phone        gmail / graph
       │                  │                    │
       ▼                  ▼                    ▼
    tenders            vendors.email        outreach
    vendors            llm_runs
    awards
```

| Table | Written by |
| --- | --- |
| `tenders`, `awards` | stage 1 |
| `vendors` identity and PDF evidence | stage 1 |
| `vendors` contact block (`email`, `phone`, status) | stage 2 |
| `llm_runs` | stage 2 |
| `outreach` | stage 3 |

Each stage is one command, configured by a settings block at the top of its
`main.py`. `pdf_email` and `pdf_phone` are read from the work order's embedded
text, not verified, and are often wrong. Stage 3 only ever mails `vendors.email`.

---

## 3. Business requirements

| ID | Requirement |
| --- | --- |
| **BR-1** | One screen of all tender details combined, exportable to Excel at any filter |
| **BR-2** | Buttons that trigger scrape, enrich and outreach, with their parameters in a form |
| **BR-3** | Add a tender by hand, and a contact by hand or by automatic lookup |
| **BR-4** | Search tender and vendor details |
| **BR-5** | Send mail to everyone in the current view, and to one recipient at a time |

---

## 4. Functional requirements

### 4.1 Unified view and Excel export (BR-1)

**FR-101.** One row per award (tender × vendor). A toggle switches to one row
per vendor, with that vendor's tenders aggregated, matching today's
`vendors.xlsx`.

**FR-102.** Columns, at minimum:

| Group | Columns |
| --- | --- |
| Tender | `tender_id`, `title`, `organisation`, `status`, `contract_date`, `contract_value`, `scraped_at` |
| Award | `bid_number`, `rank`, `quoted_value`, `awarded_value`, `awarded_currency`, `work_title` |
| Vendor | `vendor_id`, `name_raw`, `legal_form`, `city`, `state`, `buyer_hint`, `source` |
| PDF evidence | `pdf_email`, `pdf_phone`, GSTIN, source document filenames |
| Contact | `email`, `phone`, `enrichment_status`, `enriched_at` |
| Outreach | `outreach_status`, `last_attempt_at`, `attempts`, `transport`, `error` |

GSTIN and the document filenames are read from the stored tender files, not
from a database column.

**FR-103.** Evidence is visually distinct from the contact. The UI never offers
to mail a `pdf_email`.

**FR-104.** Every column is filterable: free text for names and titles, a picker
for `enrichment_status`, `source`, `outreach_status` and tender `status`, a date
range for dates, a numeric range for values. Values that cannot be interpreted
stay visible rather than being dropped.

**FR-105.** Export writes `.xlsx` of exactly the rows, columns, sort and filters
on screen. The workbook's row and column counts match the screen. CSV is offered
too. A blank source value renders as `NA`. Export changes nothing and sends
nothing.

**FR-106.** A header strip shows tenders, awards, vendors, enriched, mailable,
sent, and the phone-only count. A vendor with a phone but no email counts as
answered and can never be mailed. The UI shows that gap.

### 4.2 Workflow buttons (BR-2)

**FR-201.** One button per workflow, each opening a form:

| Button | Parameters |
| --- | --- |
| Scrape portal | `MAX_TENDERS`, `MAX_PAGES`, `REFRESH`, `DOWNLOAD_PDFS`, `DELAY`, `FROM_DATE`, `TO_DATE`, `DATE_FIELD`, `PROBE`, captcha attempts |
| Find contacts | `LIMIT`, `DELAY`, `DRY_RUN`, `ONLY_SOURCE`, `RETRY_FAILED`, `RETRY_NOT_FOUND` |
| Preview outreach | `LIMIT`, selection |
| Send outreach | `SEND`, `TRANSPORT`, `LIMIT`, `DELAY`, `TO` |
| Mail preflight | `TRANSPORT` |
| Database status | — |
| Import spreadsheet | file, `source`, dry-run |

**FR-202.** The UI sets its own safe defaults and does not copy them from the
`main.py` files: one tender, a dry run, and a preview. A form never defaults to
a live send.

**FR-203.** Before a run, the form states in plain language what it will cost
and who it will touch, counted from the actual queue.

**FR-204.** A triggered workflow returns immediately and runs as a job. The
operator sees the log as it is produced. One run per stage at a time; a second
is refused. Every run is recorded — who, when, parameters, exit status, log —
and can be reviewed afterwards.

**FR-205.** A running job can be stopped. Work already committed is kept, and
running again resumes. The UI says so.

**FR-206.** Captcha: OCR first. When OCR gives up, the image is shown in the
browser and the operator types the six characters into the same run. If nobody
answers in time, the job ends with a clear status. It never waits on a terminal
prompt.

**FR-207.** `reset` and `prune` delete data. If shown at all, they sit behind an
admin screen and a typed confirmation.

### 4.3 Manual entry (BR-3)

**FR-301.** A form adds a tender the scraper cannot reach, with the same fields
the scraper writes: tender identifier, title, organisation, status, contract
date, contract value, and for each winner the bidder name, bid number, rank,
quoted and awarded value, currency, and work title.

**FR-302.** A hand-entered vendor gets its own `source`, so it can be filtered
and enriched on its own. Every name goes through the shared normalisation. If
the name already exists, the UI shows that vendor and offers to attach the new
award to it.

**FR-303.** A manual tender writes identity only and leaves any existing contact
untouched.

**FR-304.** For any vendor the operator can type an email and/or phone. Saving
marks it answered, so stage 2 does not pay to look it up again. From the grid,
the operator can also run the automatic lookup on the selected rows only, with
the count and cost shown first.

**FR-305.** A typed contact and a model-found contact stay distinguishable. An
existing address can be corrected in place. Emails are format-checked on entry,
with the same check the mailer already uses.

**FR-306.** `failed` vendors can be put back on the queue. Re-queueing
`not_found` warns that it only makes sense after the lookup method itself has
changed.

### 4.4 Search (BR-4)

**FR-401.** One box searches tender identifier, title, organisation, work title,
vendor name, city, state, email and phone. Partial and case-insensitive. It
narrows the current filters rather than replacing them, and Export (FR-105)
exports that result.

**FR-402.** The same criteria are also available as explicit filters: date
range, contract value range, enrichment status, outreach status, source, state,
organisation.

**FR-403.** A filter and search combination can be named and recalled.

**FR-404.** Selecting a row opens everything held about that tender and vendor:
awards, PDF evidence, model-call history, and every send attempt with its
status and error.

### 4.5 Outreach (BR-5)

**FR-501.** Send to everyone in the current selection or filtered view. The
confirmation states the recipient count, the transport, the delay, and the
estimated duration.

**FR-502.** Each row has its own Send, for that one recipient. Who is eligible —
has an address, not already successfully sent to — comes from the pipeline's
existing definition of the queue. The UI does not write its own.

**FR-503.** A vendor already sent to is excluded from every later send, bulk or
single. Each attempt is recorded immediately, so a crash or a cancel mid-batch
cannot cause a re-send. A rejected address is logged and the batch continues. A
failed attempt can be retried, one row or every failure in the current view.

**FR-504.** The rendered subject and body can be previewed without sending.
A preflight sends one message to the operator's own mailbox and is not recorded
as outreach. The operator can redirect every message to their own address; the
UI warns that this still marks the real vendor as done.

**FR-505.** A live send is refused while the pitch still contains its
placeholder, or while the sender name and organisation are unset. Previews
still work. The UI says why, and what to fix.

**FR-506.** The operator chooses Gmail SMTP or Microsoft Graph. A missing
credential is reported before the run starts. A delay between sends is
enforced and cannot be set below a safe floor.

**FR-507.** Every attempt is visible: vendor, address actually used, subject,
transport, status, error, timestamp. The unsubscribe footer cannot be removed
from the UI.

---

## 5. Non-functional requirements

| ID | Requirement |
| --- | --- |
| **NFR-1** | Every in-scope task is done from the browser. No Python file is edited to change a run |
| **NFR-2** | The grid pages and filters on the server. An unfiltered portal search matches roughly 159,000 records |
| **NFR-3** | Secrets stay in the per-stage `.env` files. The UI reports whether a credential is present, never its value |
| **NFR-4** | Exports hold real contact details and are treated as sensitive |
| **NFR-5** | Viewing, running a workflow, sending mail, and destructive commands are separately controlled |
| **NFR-6** | The UI runs on the same machine and the same virtualenv as the pipeline, started with one command |

---

## 6. Acceptance

Using only a browser, an operator can:

1. Open one table of tender, award, vendor, contact and outreach data, filter
   it, and export that view to Excel, with the workbook's counts matching the
   screen. *(BR-1)*
2. Trigger a scrape, a contact lookup and an outreach preview from a form,
   watch the log, and review the run afterwards — with no Python file edited.
   *(BR-2)*
3. When captcha OCR fails, read the image in the browser, type the answer, and
   have the scrape continue. *(BR-2)*
4. Add a tender and its winner by hand, have a same-named company recognised
   rather than duplicated, type that vendor's email, and trigger an automatic
   lookup for a selected vendor. *(BR-3)*
5. Find a tender and a company by part of an identifier or name, combine that
   with a status filter and a date range, and export the result. *(BR-4)*
6. Preview a message, send a preflight to themselves, send to one recipient,
   then send to a filtered view — with no already-mailed company mailed again,
   a failure logged without stopping the batch, and a live send refused while
   the pitch placeholder or the sender signature is missing. *(BR-5)*

---

## 7. Open questions

1. **Default grid (FR-101):** one row per award, or one row per company?
2. **Roles (NFR-5):** should "can send mail" be a separate permission from "can run a workflow"?
3. **Destructive commands (FR-207):** expose reset and prune, or keep them terminal-only?
4. **Volume (NFR-2):** size the grid for thousands of tenders, or the full portal?
5. **Manual tenders (FR-301):** what identifier does a hand-entered tender use when it has no portal id?
6. **Deployment (NFR-6):** this machine only, or a shared internal server?
7. **Excel (FR-105):** match today's sheet columns, or use the combined column set?
