"""`pipeline-db` — create, inspect and seed the shared database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import settings
from .db import (
    SchemaTooNew,
    backup_copy,
    backup_dir,
    dispose_engines,
    drop_all,
    engine,
    ensure_schema,
    rolling_backup,
    session,
    table_counts,
)
from .importer import ImportError_, import_workbook
from .models import SOURCE_BIDEASY, STATUS_PENDING, TABLES
from .queries import (
    contact_breakdown,
    delete_vendors_by_source,
    enrichment_breakdown,
    enrichment_by_source,
    last_scraped_at,
    outreach_breakdown,
    outreach_queue_counts,
    pipeline_funnel,
    recheck_model_contacts,
)


def _target(args: argparse.Namespace) -> Path:
    return Path(args.db).expanduser() if args.db else settings.db_path()


def _print_report(report) -> None:
    if report.backup:
        print(f"  backup  {report.backup}")
    for line in report.converted:
        print(f"  amounts {line} converted, every row kept")
    if report.created_tables:
        print(f"  tables  + {', '.join(report.created_tables)}")
    if report.added_columns:
        print(f"  columns + {', '.join(report.added_columns)}")
    if report.created_indexes:
        print(f"  indexes + {', '.join(report.created_indexes)}")
    for line in report.migrated:
        print(f"  data    {line}")


def cmd_init(args: argparse.Namespace) -> int:
    path = _target(args)
    existed = path.is_file()
    try:
        report = ensure_schema(engine(path))
    except SchemaTooNew as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"database  {path}")
    print(f"          {'updated' if existed else 'created'}")
    _print_report(report)
    if not report.changed:
        print("          no changes - already up to date")

    print(f"\n{len(TABLES)} tables:")
    for name, count in table_counts(engine(path)).items():
        print(f"  {name:<10} {count:>6} rows")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = _target(args)
    if not path.is_file():
        print(f"database  {path}")
        print("          MISSING - run: pipeline-db init", file=sys.stderr)
        return 1

    size_kb = path.stat().st_size / 1024
    print(f"database  {path}")
    print(f"          {size_kb:,.0f} KB")

    bind = engine(path)
    print("\ntables")
    for name, count in table_counts(bind).items():
        shown = "missing" if count < 0 else f"{count} rows"
        print(f"  {name:<10} {shown:>12}")

    with session(bind) as current:
        funnel = pipeline_funnel(current)
        enrichment = enrichment_breakdown(current)
        by_source = enrichment_by_source(current)
        contacts = contact_breakdown(current)
        outreach = outreach_breakdown(current)
        queue = outreach_queue_counts(current)
        scraped_at = last_scraped_at(current)
        tenders = funnel[0][1]

    print("\nfunnel")
    for label, count, note in funnel:
        print(f"  {label:<12} {count:>6}   {note}")

    if enrichment:
        print("\nvendors.enrichment_status   (stage 2 work queue)")
        for status, count in sorted(enrichment.items()):
            print(f"  {status:<12} {count:>6}")

    # Stage 2 runs with an ONLY_SOURCE set, so the total pending count is not
    # the count it will work through. Show the split whenever there is one.
    if len(by_source) > 1:
        print("\n  by source   (stage 2's ONLY_SOURCE picks one of these)")
        for source, statuses in sorted(by_source.items()):
            parts = ", ".join(f"{s} {n}" for s, n in sorted(statuses.items()))
            print(f"    {source:<10} {parts}")

    print("\ncontacts")
    print(f"  {'mailable':<12} {contacts['mailable']:>6}   has an email address")
    print(
        f"  {'phone_only':<12} {contacts['phone_only']:>6}   "
        "answered, but stage 3 can never reach these"
    )
    print(f"  {'no_contact':<12} {contacts['no_contact']:>6}")

    print("\noutreach")
    if outreach:
        for status, count in sorted(outreach.items()):
            print(f"  {status:<12} {count:>6}")
    else:
        print("  (nothing sent yet)")
    print(f"  {'remaining':<12} {queue['remaining']:>6}   with an address, not yet sent")

    _print_stages(
        pending=enrichment.get(STATUS_PENDING, 0),
        by_source=by_source,
        remaining=queue["remaining"],
        tenders=tenders,
        scraped_at=scraped_at,
    )
    return 0


def _print_stages(
    *,
    pending: int,
    by_source: dict[str, dict[str, int]],
    remaining: int,
    tenders: int,
    scraped_at: str | None,
) -> None:
    """What each stage would do if you ran it now.

    Everything above is organised by table. This is the same numbers organised
    by the thing you are actually deciding: which command to run next.
    """
    print("\nby stage   (what each one would do if you ran it now)")

    # Stage 1's backlog is on the portal, not in here, so there is no count to
    # give -- only how stale what we hold is.
    when = (scraped_at or "")[:10] or "never"
    print(f"  01-scrape    {tenders} tender(s) stored, last scrape {when}")
    print("               always has work: the portal is the only source of new vendors")

    if pending:
        print(f"  02-enrich    {pending} vendor(s) pending  <- one paid model call each")
        # Stage 2 runs with an ONLY_SOURCE set, so the total is not necessarily
        # what it would work through.
        pending_sources = {
            source: statuses[STATUS_PENDING]
            for source, statuses in by_source.items()
            if statuses.get(STATUS_PENDING)
        }
        if len(pending_sources) > 1:
            parts = ", ".join(f"{s} {n}" for s, n in sorted(pending_sources.items()))
            print(f"               by source: {parts}")
            print("               check ONLY_SOURCE in 02-enrich/main.py before running")
    else:
        print("  02-enrich    nothing pending - run 01-scrape first to create work")

    if remaining:
        print(f"  03-outreach  {remaining} recipient(s) queued")
    else:
        print("  03-outreach  nothing to send - run 02-enrich to find more addresses")


def cmd_reset(args: argparse.Namespace) -> int:
    path = _target(args)
    print(f"This DROPS every table in {path} and recreates them empty.")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing: not a terminal. Re-run with --yes.", file=sys.stderr)
            return 1
        reply = input("Type 'reset' to confirm: ").strip()
        if reply != "reset":
            print("Cancelled. Nothing was changed.")
            return 1

    bind = engine(path)
    drop_all(bind)
    ensure_schema(bind)
    print("Done - schema recreated, 0 rows.")
    for name, count in table_counts(bind).items():
        print(f"  {name:<10} {count:>6} rows")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete every vendor from one origin. The counterpart to `import-xlsx`."""
    path = _target(args)
    if not path.is_file():
        print(f"{path} does not exist - run: pipeline-db init", file=sys.stderr)
        return 1

    bind = engine(path)
    with session(bind) as current:
        doomed = enrichment_breakdown(current, source=args.source)

    total = sum(doomed.values())
    if not total:
        print(f"No vendors with source={args.source!r}. Nothing to do.")
        return 0

    print(f"This DELETES {total} vendor(s) with source={args.source!r} from {path}:")
    for status, count in sorted(doomed.items()):
        print(f"  {status:<12} {count:>6}")
    print("\nThe source spreadsheets are unaffected - re-insert with:")
    print("  pipeline-db import-xlsx <file.xlsx>")

    if not args.yes:
        if not sys.stdin.isatty():
            print("\nRefusing: not a terminal. Re-run with --yes.", file=sys.stderr)
            return 1
        reply = input("\nType 'prune' to confirm: ").strip()
        if reply != "prune":
            print("Cancelled. Nothing was changed.")
            return 1

    try:
        with session(bind) as current:
            deleted = delete_vendors_by_source(current, args.source)
    except ValueError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    print(f"\nDone - {deleted} vendor(s) deleted.")
    for name, count in table_counts(bind).items():
        print(f"  {name:<10} {count:>6} rows")
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    """Take an existing SQLite file as the shared database, then migrate it.

    This is also how a backup is put back. With ``--force`` the database it
    replaces is first copied to ``<name>.replaced-<time>``, so nothing is lost.
    Close the window first: the copy must not race a running job.
    """
    legacy = Path(args.source).expanduser()
    path = _target(args)

    if not legacy.is_file():
        print(f"No such file: {legacy}", file=sys.stderr)
        return 1
    if legacy.resolve() == path.resolve():
        print(f"{legacy} is already the shared database.", file=sys.stderr)
        return 1
    if path.is_file() and not args.force:
        print(
            f"{path} already exists. Move it aside, or pass --force to overwrite it.",
            file=sys.stderr,
        )
        return 1

    dispose_engines()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        kept = backup_copy(path, "replaced")
        print(f"kept      {path}")
        print(f"      ->  {kept}")
    # SQLite's backup, not a file copy: it reads the source's log and rewrites
    # the target's, so a -wal left beside either file cannot mix the two.
    source = sqlite3.connect(legacy)
    target = sqlite3.connect(path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    print(f"copied    {legacy}")
    print(f"      ->  {path}")

    report = ensure_schema(engine(path))
    _print_report(report)

    print("\nrows carried over:")
    for name, count in table_counts(engine(path)).items():
        print(f"  {name:<10} {count:>6}")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    """Add an older copy's rows into the live database. Nothing already there is lost.

    For a database an upgrade emptied after work carried on in it. Close the
    window first. The live file is copied aside before anything is written.
    """
    from .merge import merge_backup

    old = Path(args.source).expanduser()
    path = _target(args)
    if not old.is_file():
        print(f"No such file: {old}", file=sys.stderr)
        return 1
    if not path.is_file():
        print(f"{path} does not exist. Use: pipeline-db adopt {old}", file=sys.stderr)
        return 1

    dispose_engines()
    try:
        report = merge_backup(path, old, letter=args.letter)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"database  {path}")
    print(f"  backup  {report.backup}")
    print(f"  merged  {old}")
    print("\nrows added from the old copy:")
    for name, count in report.added.items():
        print(f"  {name:<16} {count:>6}")
    if not report.added:
        print("  none - everything in it is already here")
    if report.filled:
        print("\nrows already here that took missing values from it:")
        for name, count in report.filled.items():
            print(f"  {name:<16} {count:>6}")
    print(f"\nmail letter: the {'old copy' if report.letter == 'backup' else 'current'} one")
    print("\nrows now:")
    for name, count in table_counts(engine(path)).items():
        print(f"  {name:<16} {count:>6}")
    return 0


def cmd_import_xlsx(args: argparse.Namespace) -> int:
    path = _target(args)
    if not path.is_file():
        print(f"{path} does not exist - run: pipeline-db init", file=sys.stderr)
        return 1

    bind = engine(path)
    failures = 0
    if args.dry_run:
        print("DRY RUN - nothing is written\n")

    for item in args.files:
        try:
            with session(bind) as current:
                report = import_workbook(
                    current, item, source=args.source, dry_run=args.dry_run
                )
                if args.dry_run:
                    current.rollback()
        except ImportError_ as exc:
            print(f"FAILED  {exc}", file=sys.stderr)
            failures += 1
            continue
        print(report.render())
        if report.samples:
            print(f"  e.g.               {', '.join(report.samples[:3])}")
        print()

    if not args.dry_run:
        with session(bind) as current:
            for status, count in sorted(enrichment_breakdown(current).items()):
                print(f"  {status:<12} {count:>6}")
    return 1 if failures else 0


def cmd_backup(args: argparse.Namespace) -> int:
    """A checked copy in ``backups/`` beside the database. Keeps the newest few per label."""
    path = _target(args)
    print(f"database  {path}")
    if not path.is_file():
        print("          not created yet - nothing to back up")
        return 0
    print(f"  backup  {rolling_backup(path, args.label)}")
    return 0


def cmd_recheck_contacts(args: argparse.Namespace) -> int:
    """Drop saved model answers the address rule would not keep. Dry run unless --apply.

    With ``--apply`` the database is copied to ``backups/`` first and the list of
    changes is written beside the copy, so every dropped value can be found again.
    """
    path = _target(args)
    if not path.is_file():
        print(f"{path} does not exist - run: pipeline-db init", file=sys.stderr)
        return 1

    with session(engine(path)) as current:
        rows = recheck_model_contacts(current, apply=False)
        current.rollback()
    copy = None
    if rows and args.apply:
        copy = rolling_backup(path, "before-recheck")
        with session(engine(path)) as current:
            rows = recheck_model_contacts(current, apply=True)

    if not rows:
        print("Every saved model answer passes the address rule. Nothing to do.")
        return 0
    verb = "Changed" if args.apply else "Would change"
    lines = [f"{verb} {len(rows)} company(ies):"]
    for row in rows:
        lines.append(f"  {row.vendor_id:>5}  {row.name}")
        for item in row.dropped:
            lines.append(f"         drop {item}")
        if row.status_after != row.status_before:
            lines.append(f"         status {row.status_before} -> {row.status_after}")
    print("\n".join(lines))
    if copy is None:
        print("\nDry run. Nothing was changed. Run again with --apply to make these changes.")
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    listing = backup_dir(path) / f"recheck-{stamp}.txt"
    listing.write_text(
        f"database  {path}\nbackup    {copy}\n\n" + "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(f"\n  backup  {copy}")
    print(f"  list    {listing}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline-db",
        description="Create, inspect and seed the shared tender-pipeline database.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Database file to act on (default: PIPELINE_DB, else data/pipeline.sqlite3)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create the schema, or bring it up to date")
    init.set_defaults(func=cmd_init)

    backup = sub.add_parser("backup", help="Write a checked copy of the database to backups/")
    backup.add_argument(
        "--label",
        choices=("manual", "update"),
        default="manual",
        help="Name part of the copy (default: manual). The newest 10 of each are kept.",
    )
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser(
        "restore", help="Put a backup back as the database. The current one is copied aside first"
    )
    restore.add_argument("source", help="The backup, e.g. data/backups/pipeline.sqlite3.update-...")
    restore.set_defaults(func=cmd_adopt, force=True)

    status = sub.add_parser("status", help="Row counts and the two work queues")
    status.set_defaults(func=cmd_status)

    reset = sub.add_parser("reset", help="DROP every table and recreate them empty")
    reset.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    reset.set_defaults(func=cmd_reset)

    prune = sub.add_parser("prune", help="Delete every vendor from one source")
    prune.add_argument(
        "--source",
        required=True,
        help=f"Value of vendors.source to delete (e.g. {SOURCE_BIDEASY})",
    )
    prune.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    prune.set_defaults(func=cmd_prune)

    adopt = sub.add_parser(
        "adopt", help="Copy an existing SQLite file in as the shared database"
    )
    adopt.add_argument("source", help="Path to the SQLite file to adopt")
    adopt.add_argument(
        "--force", action="store_true", help="Overwrite the shared database if it exists"
    )
    adopt.set_defaults(func=cmd_adopt)

    merge = sub.add_parser(
        "merge", help="Add an older copy's rows into the database, keeping what is here"
    )
    merge.add_argument("source", help="Path to the older SQLite file, e.g. data/pipeline.sqlite3.bak")
    merge.add_argument(
        "--letter",
        choices=("backup", "current"),
        default="backup",
        help="Which saved mail letter to keep (default: the one in the older copy)",
    )
    merge.set_defaults(func=cmd_merge)

    imp = sub.add_parser("import-xlsx", help="Insert companies from spreadsheets as vendors")
    imp.add_argument("files", nargs="+", help="One or more .xlsx files")
    imp.add_argument(
        "--source",
        default=SOURCE_BIDEASY,
        help=f"Value for vendors.source (default: {SOURCE_BIDEASY})",
    )
    imp.add_argument(
        "--dry-run", action="store_true", help="Report what would change, write nothing"
    )
    imp.set_defaults(func=cmd_import_xlsx)

    recheck = sub.add_parser(
        "recheck-contacts",
        help="Drop saved model answers that are government addresses or bad phones",
    )
    recheck.add_argument(
        "--apply", action="store_true", help="Back up, then make the changes (default: report only)"
    )
    recheck.set_defaults(func=cmd_recheck_contacts)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    finally:
        dispose_engines()


if __name__ == "__main__":
    raise SystemExit(main())
