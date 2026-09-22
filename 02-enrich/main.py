"""Look up public email/phone for vendors waiting in the pipeline database.

    python main.py

Everything is configured in the CONFIG block below. There are no flags.

Safe by default: DRY_RUN = True prints the queue and spends nothing, and
LIMIT = 1 keeps the first real run to one model call. Raise it once you have
seen a result you believe.
"""

from __future__ import annotations

import sys
import textwrap
import time
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


# ============================== CONFIG ==============================
# Edit these, save, then run:  python main.py

# How many vendors to research this run. Each one is a paid model call.
#   1     -> smoke test
#   None  -> every pending vendor (check the count first!)
LIMIT = 1

# Seconds to wait between calls. Keeps you inside provider rate limits.
DELAY = 1.0

# True  -> print the queue and stop. Spends nothing.
# False -> research the vendors, up to LIMIT.
DRY_RUN = False

# Restrict the queue to one origin:
#   "scrape"  -> only vendors the portal scraper found
#   "bideasy" -> only the imported spreadsheet rows
#   None      -> everything pending
# Vendors already answered are skipped either way -- this narrows which of
# the remaining ones you pay for.
ONLY_SOURCE = "scrape"

# True -> put previously failed vendors back on the queue before starting.
#         Failures are provider errors, so they are safe to retry.
RETRY_FAILED = True

# True -> also requeue 'not_found' vendors. Normally WRONG: that status
#         means the lookup worked and nothing public exists, so asking
#         again the same way just pays for the same answer.
#         Turn it on once after changing the *method* -- enabling web
#         search, switching model, adding evidence to the prompt --
#         because those verdicts were reached without the thing that
#         makes an answer possible.
RETRY_NOT_FOUND = True
# ====================================================================


_require(
    ("langchain_core", "langchain"),
    ("langchain_openai", "langchain-openai"),
    ("pydantic", "pydantic"),
    ("dotenv", "python-dotenv"),
    ("sqlalchemy", "SQLAlchemy"),
)

from pipeline_core.db import session                       # noqa: E402
from pipeline_core.queries import (                        # noqa: E402
    enrichment_breakdown,
    pending_vendors,
    reset_failed,
    reset_not_found,
)
from stage2_enrich import settings                         # noqa: E402
from stage2_enrich.runner import enrich_pending, queue     # noqa: E402


def say(message: str) -> None:
    print(f"      {message}")


def banner() -> None:
    print("=" * 62)
    print("  stage2-enrich  --  public contact lookup  (stage 2 of 3)")
    print("=" * 62)
    print(f"  limit={LIMIT}  source={ONLY_SOURCE or 'all'}  delay={DELAY}s  dry_run={DRY_RUN}"
          f"  web={'on' if settings.web_search() else 'OFF'}")


def show_queue() -> int:
    print("\n[1/3] The work queue")
    with session() as current:
        breakdown = enrichment_breakdown(current)
    for status, count in sorted(breakdown.items()):
        say(f"{status:<12} {count:>6}")
    pending = breakdown.get("pending", 0)
    if not breakdown:
        say("no vendors yet -- run 01-scrape first to find some")
    elif not pending:
        say("nothing pending -- stage 2 has already seen every vendor")
    return pending


def main() -> int:
    started = time.time()
    banner()

    if RETRY_FAILED:
        with session() as current:
            moved = reset_failed(current)
        print(f"\n      requeued {moved} previously failed vendor(s)")

    if RETRY_NOT_FOUND:
        with session() as current:
            moved = reset_not_found(current)
        print(f"      requeued {moved} previously not_found vendor(s)")

    pending = show_queue()
    if not pending:
        return 0
    if ONLY_SOURCE:
        with session() as current:
            scoped = len(pending_vendors(current, source=ONLY_SOURCE))
        say(f"of those, source={ONLY_SOURCE}: {scoped}")

    print("\n[2/3] Preflight")
    try:
        settings.require_env()
    except RuntimeError as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1
    say(f"model       {settings.model()}")
    if settings.web_search():
        say(f"web search  on, up to {settings.WEB_SEARCH_RESULTS} results per call")
    else:
        say("web search  OFF -- the model answers from memory only, which")
        say("            returns nothing for small regional firms")

    work = queue(LIMIT, source=ONLY_SOURCE)
    say(f"{len(work)} vendor(s) this run" + (" (LIMIT reached)" if LIMIT else ""))

    if DRY_RUN:
        print("\n[3/3] Dry run -- nothing sent, nothing spent")
        for vendor in work:
            say(f"{vendor.vendor_id:>5}  {vendor.name_raw}")
        return 0

    print("\n[3/3] Researching")

    def report(vendor, status, detail) -> None:
        say(f"{vendor.vendor_id:>5}  {vendor.name_raw[:34]:<34} {status:<10} {detail[:40]}")

    try:
        summary = enrich_pending(
            limit=LIMIT, delay=DELAY, vendors=work, source=ONLY_SOURCE, report=report
        )
    except KeyboardInterrupt:
        print("\n\nStopped. Everything already answered is saved -- re-run to resume.")
        return 130
    except RuntimeError as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1

    print("\n" + "-" * 62)
    print(f"Done in {time.time() - started:.0f}s")
    print(f"  {summary.render()}")
    if summary.errors:
        print("\n  Failures. Nothing was lost -- set RETRY_FAILED = True to requeue them.")
        for vendor_id, detail in summary.errors[:3]:
            print(f"\n  vendor {vendor_id}:")
            for line in textwrap.wrap(detail, 72):
                print(f"    {line}")
        if len(summary.errors) > 3:
            print(f"\n  ...and {len(summary.errors) - 3} more.")
        print("\n  Every attempt is in the llm_runs table, with the full error text.")
    print("  Next: stage 3 (03-outreach) mails everyone with an address.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
