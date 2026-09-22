# stage3-outreach

Mails the vendors the pipeline has found an address for, once each, through Gmail SMTP
or Microsoft Graph.

This is **stage 3 of three**:

```text
01-scrape  ──►  02-enrich  ──►  03-outreach
finds the       finds email      you are here
companies       and phone
```

Python **3.11+**. Scripts at the project root — this is a runner, not a library.

---

## How it works

```text
vendors WITH an email  MINUS  anyone already in outreach with status 'sent'
        │
        ▼
   render subject + body per company
        │
        ├─ SEND = False  -> previews/*.txt, nobody is mailed
        └─ SEND = True   -> transport.send(), then one outreach row per attempt
```

**The same database decides the queue and records the sends**, so the two cannot drift
apart. Each send is committed immediately, so an interrupted run resumes by being run
again — and nobody is mailed twice.

---

## Quick start

Do the one-time setup in the [root README](../README.md) first (one shared `.venv` for
the whole pipeline). Then, in this folder:

```powershell
copy .env.example .env             # then fill in SENDER_NAME, SENDER_ORG and a mailbox
uv run python main.py              # SEND = False by default: writes previews/, mails nobody
```

Then, in order, changing the `CONFIG` block in `main.py` each time:

1. **Previews** (the default) — writes `previews/*.txt`. Read a few.
2. `PREFLIGHT = True` — proves the mailbox login works. One mail, to you. Not logged as
   outreach.
3. `PREFLIGHT = False`, `SEND = True`, `LIMIT = 1`, `TO = "you@yourcompany.com"` — one real
   send, redirected to you.
4. `SEND = True`, `TO = None`, `LIMIT` to taste — the real thing.

A live send (steps 3 and 4) refuses to start until the pitch is written and the sender
is set — see [The message](#the-message).

See who is waiting before any of that:

```powershell
uv run pipeline-db status
```

---

## Download the queue first

Before sending anything, pull the exact list stage 3 would mail:

```powershell
uv run python get_data.py
```

Writes `exports/outreach_queue.csv` and `.xlsx` (both gitignored - they hold real
contact details). Reads only: nothing is sent and nothing in the database changes.

The rows come from `outreach_targets()`, the same query `campaign.py` sends from, so the
file cannot disagree with what a live run would do. Columns include where the address
came from (`source`), when stage 2 found it (`enriched_at`), and what the scraped work
order said (`pdf_email`) - so you can sanity-check an address before mailing it.

Its `CONFIG` block:

| Setting | Default | What it does |
| --- | --- | --- |
| `OUT_DIR` | `"exports"` | Where the two files land |
| `INCLUDE_SENT` | `False` | `True` also lists everyone already mailed, with their status |
| `ONLY_SOURCE` | `None` | `"scrape"` or `"bideasy"` to narrow by origin |
| `TIMESTAMP` | `False` | `True` dates the filenames, keeping past downloads |

**Read it before a live send.** A model-found address can still be wrong - one came back
as `...@gmal.com`, a typo for `gmail.com`, which would simply bounce.

---

## Configuration

Everything is in the `CONFIG` block at the top of `main.py`. There are no flags.

| Setting | Default | What it does |
| --- | --- | --- |
| `TRANSPORT` | `"gmail"` | `"gmail"` or `"graph"`. Only the one you name is imported |
| `SEND` | `False` | `False` writes previews and mails nobody |
| `PREFLIGHT` | `False` | `True` = one test mail to yourself, then stop. Overrides everything below |
| `LIMIT` | `None` | `None` = everyone remaining; an int caps the batch |
| `DELAY` | `5.0` | Seconds between sends. 5.0 ≈ 12/min, inside provider limits |
| `TO` | `None` | Redirect **every** message to one address, for live testing |

> `TO` redirects the delivery but still records the real vendor, so a redirected send
> marks that vendor as done. Use it with `LIMIT`.

### Environment

`.env` in this folder (never committed):

| For | Variables |
| --- | --- |
| every live send | `SENDER_NAME`, `SENDER_ORG` — the signature on every message |
| `gmail` | `GMAIL_USER`, `GMAIL_APP_PASSWORD` (an [App Password](https://myaccount.google.com/apppasswords), not your login; needs 2-Step Verification) |
| `graph` | `GRAPH_CLIENT_ID`, `GRAPH_TENANT_ID`, `OUTLOOK_USER` — see [Using Microsoft Graph](#using-microsoft-graph) |

---

## Transports

`transport.py` defines the contract, and is the only place that chooses between them:

```python
__init__(self)                         connect / authenticate
send(self, to, subject, body) -> None  deliver one message
close(self)                            tear down
```

A transport raises `TransportError` when one recipient fails. That is what lets the run
log the address and carry on to the next person instead of stopping.

Imports live **inside** `build_transport`, so choosing `"gmail"` never loads `msal`, and
choosing `"graph"` never opens `smtplib`.

---

## Using Microsoft Graph

Use this when the mailbox is Microsoft 365 and SMTP sign-in is disabled for the tenant,
as many organisations now do. It needs a one-time app registration by a Microsoft 365 /
Entra admin:

1. **Entra ID → App registrations → New registration.** Any name. Supported account
   types: *this organizational directory only*. Redirect URI: none.
2. **Authentication → Allow public client flows → Yes.** The script signs in with a
   device code, so no client secret is created or stored.
3. **API permissions → Add → Microsoft Graph → Delegated → `Mail.Send`**, then
   **Grant admin consent**. That is the only permission it asks for — it can send as
   the signed-in user, and cannot read any mailbox.
4. From the app's **Overview** page, copy the *Application (client) ID* and the
   *Directory (tenant) ID* into `.env` as `GRAPH_CLIENT_ID` and `GRAPH_TENANT_ID`. Set
   `OUTLOOK_USER` to the mailbox that sends.

Then set `TRANSPORT = "graph"` in `main.py` and run a preflight. The first run prints a
code to enter at <https://microsoft.com/devicelogin>. After that the sign-in is cached in
`.msal_token_cache.json` (never committed) and later runs are silent. To revoke access,
delete the app registration.

---

## The message

`campaign.py` holds `SUBJECT_TEMPLATE` and `build_body`. Two things must be done before
any real send, and a live send refuses to start until they are:

1. **The pitch.** The body contains a placeholder paragraph — replace it in
   `build_body()`:

   ```text
   <-- replace this paragraph with your actual pitch -->
   ```

2. **The signature.** Set `SENDER_NAME` and `SENDER_ORG` in `.env`. Until then,
   previews show `<SENDER_NAME from .env>` where the name will go.

Previews work either way, so you can review the text first. The footer offers an
unsubscribe reply; keep it.

---

## Layout

```text
.
├── main.py                     # runner: CONFIG block, no argparse
├── get_data.py                 # download the send queue as CSV + Excel
├── campaign.py                 # recipients, rendering, previews, send loop, logging
├── transport.py                # the contract + build_transport
├── gmail/send.py               # SMTP over TLS
├── graph/auth.py, send.py      # MSAL device-code + Graph sendMail
├── previews/                   # dry-run output (gitignored)
├── exports/                    # get_data.py output (gitignored)
└── tests/test_campaign.py      # a fake transport; nothing is ever sent
```

---

## Tests

```powershell
uv run python -m pytest -q
```

No network, no credentials, and **no mail**: the transport is replaced by a fake that
records deliveries. Each test gets its own throwaway database via `PIPELINE_DB`. The
tests pin the behaviour that matters — a sent vendor never reappears, a rejected address
stays in the queue, and the transport is closed even when a send fails.
