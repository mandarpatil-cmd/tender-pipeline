"""Preview or send outreach to vendors in the pipeline database.

Everything is configured in the CONFIG block below. There are no flags:

    python main.py

Safe by default: SEND = False writes previews/ and mails nobody.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running this file directly does not put these on sys.path: this project's own
# `src/`, and the shared `core` package, which lives in a sibling folder.
_HERE = Path(__file__).resolve().parent
for _path in (_HERE / "src", _HERE.parent / "core" / "src"):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _importable(name: str) -> bool:
    from importlib.util import find_spec

    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _require(*modules: "tuple[str, str]") -> None:
    """Fail with the command that fixes it, not with an import traceback."""
    missing = [package for module, package in modules if not _importable(module)]
    if not missing:
        return
    raise SystemExit(
        "Missing: " + ", ".join(missing) + "\n\n"
        "The pipeline's shared virtualenv has them; your current interpreter does not.\n"
        "Run it with:\n"
        f"    cd {_HERE}\n"
        "    uv run python main.py\n\n"
        "or call the venv interpreter directly:\n"
        "    ..\\.venv\\Scripts\\python.exe main.py\n\n"
        "If there is no .venv yet, create it from the repo root:\n"
        "    uv venv --python 3.13\n"
        "    uv pip install -r requirements.txt"
    )


_require(
    ("requests", "requests"),
    ("dotenv", "python-dotenv"),
    ("sqlalchemy", "SQLAlchemy"),
)
# msal is only needed by the graph transport, which transport.py imports lazily.
# Requiring it here would block anyone sending through gmail.

from campaign import run as run_campaign  # noqa: E402
from transport import TRANSPORTS  # noqa: E402

# ============================== CONFIG ==============================
# Edit these, save, then run:  python main.py

# Which mailer to use. Only the one you name here is ever loaded.
#   "gmail" -> Gmail SMTP, needs GMAIL_USER + GMAIL_APP_PASSWORD in .env
#   "graph" -> Microsoft 365, needs GRAPH_CLIENT_ID + GRAPH_TENANT_ID in .env
TRANSPORT = "gmail"

# False -> dry run: write previews/*.txt and send nothing.
# True  -> actually transmit to every vendor with an address who has not
#          already been sent to.
SEND = False

# True -> send ONE test mail to yourself and stop. Proves auth works.
#         Recipient is GMAIL_USER (gmail) or OUTLOOK_USER (graph).
#         Overrides everything below, and is not logged as outreach.
PREFLIGHT = False

# None -> every remaining recipient. Or an int: 1 for a smoke test, 5 for a batch.
LIMIT = None

# Seconds to wait between live sends. 5.0 = 12/min, inside provider limits.
DELAY = 5.0

# None -> each row's real address.
# "you@company.com" -> redirect EVERY message to you instead. Live testing.
# The outreach row still records the vendor, so a redirected test send will
# still mark that vendor as done. Use LIMIT with it.
TO = None

# ====================================================================


def main() -> int:
    if TRANSPORT not in TRANSPORTS:
        print(
            f"TRANSPORT is {TRANSPORT!r} - it must be one of {TRANSPORTS}. "
            "Fix it in the CONFIG block at the top of main.py.",
            file=sys.stderr,
        )
        return 1

    # There are no flags to show what this run will do, so say it here.
    mode = "PREFLIGHT" if PREFLIGHT else ("SEND" if SEND else "DRY RUN")
    print(
        f"mode={mode}  transport={TRANSPORT}  "
        f"limit={LIMIT}  delay={DELAY}  to={TO or 'each row'}"
    )

    if PREFLIGHT:
        return run_campaign(do_preflight=True, transport=TRANSPORT)

    return run_campaign(
        send=SEND,
        limit=LIMIT,
        delay=DELAY,
        to=TO,
        transport=TRANSPORT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
