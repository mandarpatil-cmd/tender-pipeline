"""Add an older database's rows into the live one, keeping everything already there.

Used when an upgrade emptied the database and work then carried on in the empty
one: the old copy goes back in without losing what was done since. Ids restart
after a wipe, so old vendor 5 need not be new vendor 5. Companies match by
``name_norm``, tenders by id, awards by tender and bid, runs by stage and start
time. Every other id is given a new number and every reference is rewritten.

What the live database already holds wins. The old copy only fills what is
missing, with two exceptions: a company still ``pending`` takes the old
lookup's answer, and a do-not-mail mark from either side is kept.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .db import BUSY_TIMEOUT_MS, backup_copy, dispose_engines, engine, ensure_schema
from .models import STATUS_PENDING

#: The company's lookup result. Taken from the old copy as a whole, never mixed.
_CONTACT_BLOCK = ("email", "phone", "enrichment_status", "enriched_at", "contact_origin")


@dataclass
class MergeReport:
    #: Rows added per table, in the order they were merged.
    added: dict[str, int] = field(default_factory=dict)
    #: Rows that existed on both sides and took blanks or an answer from the old copy.
    filled: dict[str, int] = field(default_factory=dict)
    backup: str = ""
    letter: str = ""


def merge_backup(live: Path, old: Path, *, letter: str = "backup") -> MergeReport:
    """Merge ``old`` into ``live``. ``live`` is copied aside first. Safe to repeat.

    ``letter`` picks the saved mail letter: ``backup`` takes the old one,
    ``current`` keeps the live one. The other stays in the copy.
    """
    live = Path(live)
    old = Path(old)
    if live.resolve() == old.resolve():
        raise ValueError("The old copy and the live database are the same file.")
    report = MergeReport(letter=letter)
    ensure_schema(engine(live))
    report.backup = str(backup_copy(live, "before-merge"))
    with tempfile.TemporaryDirectory(prefix="pipeline-merge-") as hold:
        # The old copy is brought up to the current schema in a scratch file,
        # so its own file is never written.
        source = Path(hold) / "old.sqlite3"
        reader = sqlite3.connect(old)
        writer = sqlite3.connect(source)
        try:
            reader.backup(writer)
        finally:
            writer.close()
            reader.close()
        ensure_schema(engine(source))
        dispose_engines()
        _merge(live, source, report, letter)
        dispose_engines()
    ensure_schema(engine(live))
    dispose_engines()
    return report


def _rows(con: sqlite3.Connection, table: str) -> list[dict]:
    return [dict(row) for row in con.execute(f'SELECT * FROM "{table}"')]


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]


def _insert(con: sqlite3.Connection, table: str, values: dict, columns: list[str]) -> int:
    kept = [name for name in columns if name in values]
    cursor = con.execute(
        f'INSERT INTO "{table}" ({", ".join(kept)}) VALUES ({", ".join("?" for _ in kept)})',
        [values[name] for name in kept],
    )
    return int(cursor.lastrowid)


def _fill_blanks(
    con: sqlite3.Connection, table: str, where: dict, live: dict, old: dict, skip=()
) -> bool:
    """Copy each old value into a column the live row leaves empty."""
    changes = {
        name: value
        for name, value in old.items()
        if name in live
        and name not in where
        and name not in skip
        and value not in (None, "")
        and live[name] in (None, "")
    }
    if not changes:
        return False
    sets = ", ".join(f'"{name}" = ?' for name in changes)
    keys = " AND ".join(f'"{name}" = ?' for name in where)
    con.execute(
        f'UPDATE "{table}" SET {sets} WHERE {keys}',
        [*changes.values(), *where.values()],
    )
    return True


def _merge(live_path: Path, source_path: Path, report: MergeReport, letter: str) -> None:
    con = sqlite3.connect(live_path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    src = sqlite3.connect(source_path)
    src.row_factory = sqlite3.Row
    try:
        running = con.execute(
            "SELECT job_id FROM ui_jobs WHERE state IN ('running', 'waiting')"
        ).fetchone()
        if running is not None:
            raise RuntimeError(
                f"Run {running[0]} is still going. Close the window, then merge."
            )
        con.execute("BEGIN IMMEDIATE")
        jobs, added_jobs = _merge_jobs(con, src, report)
        vendors = _merge_vendors(con, src, report)
        _rewrite_job_params(con, added_jobs, vendors)
        _merge_tenders(con, src, report, jobs)
        _merge_awards(con, src, report, vendors)
        _merge_documents(con, src, report)
        runs = _merge_llm_runs(con, src, report, vendors, jobs)
        _merge_contacts(con, src, report, vendors, runs)
        _merge_outreach(con, src, report, vendors, jobs)
        _merge_job_tenders(con, src, report, jobs)
        _merge_letter(con, src, report, letter)
        con.execute("COMMIT")
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        src.close()
        con.close()


def _remap_vendor(row: dict, vendors: dict[int, int]) -> bool:
    """Point the row at the live company. False for an orphan the old copy carried."""
    vendor_id = vendors.get(int(row["vendor_id"]))
    if vendor_id is None:
        return False
    row["vendor_id"] = vendor_id
    return True


def _count(report: MergeReport, kind: str, table: str) -> None:
    bucket = report.added if kind == "added" else report.filled
    bucket[table] = bucket.get(table, 0) + 1


def _merge_jobs(con, src, report) -> tuple[dict[int, int], list[tuple[int, str | None]]]:
    """Old run id to live run id, and the runs added with their old parameters.

    A run is the same run when its stage and start time match.
    """
    columns = _columns(con, "ui_jobs")
    known = {
        (row["stage"], row["started_at"]): int(row["job_id"])
        for row in con.execute("SELECT job_id, stage, started_at FROM ui_jobs")
    }
    mapping: dict[int, int] = {}
    added: list[tuple[int, str | None]] = []
    for row in _rows(src, "ui_jobs"):
        key = (row["stage"], row["started_at"])
        if key in known:
            mapping[int(row["job_id"])] = known[key]
            continue
        values = {name: value for name, value in row.items() if name != "job_id"}
        new_id = _insert(con, "ui_jobs", values, columns)
        mapping[int(row["job_id"])] = new_id
        known[key] = new_id
        added.append((new_id, row.get("params_json")))
        _count(report, "added", "ui_jobs")
    return mapping, added


def _merge_vendors(con, src, report) -> dict[int, int]:
    columns = _columns(con, "vendors")
    live = {row["name_norm"]: dict(row) for row in con.execute("SELECT * FROM vendors")}
    mapping: dict[int, int] = {}
    for row in _rows(src, "vendors"):
        mine = live.get(row["name_norm"])
        if mine is None:
            values = {name: value for name, value in row.items() if name != "vendor_id"}
            new_id = _insert(con, "vendors", values, columns)
            mapping[int(row["vendor_id"])] = new_id
            live[row["name_norm"]] = {**values, "vendor_id": new_id}
            _count(report, "added", "vendors")
            continue
        vendor_id = int(mine["vendor_id"])
        mapping[int(row["vendor_id"])] = vendor_id
        changed = False
        if mine["enrichment_status"] == STATUS_PENDING and row["enrichment_status"] != STATUS_PENDING:
            block = {name: row[name] for name in _CONTACT_BLOCK if name in row}
            sets = ", ".join(f'"{name}" = ?' for name in block)
            con.execute(
                f'UPDATE vendors SET {sets} WHERE vendor_id = ?', [*block.values(), vendor_id]
            )
            changed = True
        stop = row.get("do_not_mail")
        if stop and not mine.get("do_not_mail"):
            con.execute("UPDATE vendors SET do_not_mail = ? WHERE vendor_id = ?", (stop, vendor_id))
            changed = True
        changed |= _fill_blanks(
            con,
            "vendors",
            {"vendor_id": vendor_id},
            mine,
            row,
            skip=(*_CONTACT_BLOCK, "name_norm", "do_not_mail"),
        )
        if changed:
            _count(report, "filled", "vendors")
    return mapping


def _rewrite_job_params(con, added: list[tuple[int, str | None]], vendors: dict[int, int]) -> None:
    """Point the vendor ids an added run's parameters name at the live companies."""
    for job_id, raw in added:
        params = _params(raw, vendors)
        if params is not None:
            con.execute("UPDATE ui_jobs SET params_json = ? WHERE job_id = ?", (params, job_id))


def _params(raw: str | None, vendors: dict[int, int]) -> str | None:
    """A run's parameters with old vendor ids replaced. None when nothing names a vendor."""
    try:
        params = json.loads(raw or "{}")
    except ValueError:
        return None
    if not isinstance(params, dict):
        return None
    changed = False
    if isinstance(params.get("awards"), list):
        rewritten = []
        for item in params["awards"]:
            vendor, separator, tender = str(item).partition(":")
            if separator and vendor.isdigit() and int(vendor) in vendors:
                rewritten.append(f"{vendors[int(vendor)]}:{tender}")
            else:
                rewritten.append(item)
        params["awards"] = rewritten
        changed = True
    if isinstance(params.get("vendor_ids"), list):
        params["vendor_ids"] = [
            vendors.get(int(item), int(item)) if str(item).isdigit() else item
            for item in params["vendor_ids"]
        ]
        changed = True
    return json.dumps(params, sort_keys=True) if changed else None


def _merge_tenders(con, src, report, jobs) -> None:
    columns = _columns(con, "tenders")
    live = {row["tender_id"]: dict(row) for row in con.execute("SELECT * FROM tenders")}
    for row in _rows(src, "tenders"):
        if row.get("scrape_job_id") is not None:
            row["scrape_job_id"] = jobs.get(int(row["scrape_job_id"]))
        mine = live.get(row["tender_id"])
        if mine is None:
            _insert(con, "tenders", row, columns)
            _count(report, "added", "tenders")
        elif _fill_blanks(con, "tenders", {"tender_id": row["tender_id"]}, mine, row):
            _count(report, "filled", "tenders")


def _merge_awards(con, src, report, vendors) -> None:
    columns = _columns(con, "awards")
    live = {
        (row["tender_id"], row["bid_number"]): dict(row)
        for row in con.execute("SELECT * FROM awards")
    }
    for row in _rows(src, "awards"):
        if not _remap_vendor(row, vendors):
            continue
        mine = live.get((row["tender_id"], row["bid_number"]))
        if mine is None:
            _insert(con, "awards", row, columns)
            _count(report, "added", "awards")
        elif _fill_blanks(
            con,
            "awards",
            {"tender_id": row["tender_id"], "bid_number": row["bid_number"]},
            mine,
            row,
            skip=("vendor_id",),
        ):
            _count(report, "filled", "awards")


def _merge_documents(con, src, report) -> None:
    columns = _columns(con, "tender_documents")
    live = {
        (row["tender_id"], row["filename"]): dict(row)
        for row in con.execute("SELECT * FROM tender_documents")
    }
    for row in _rows(src, "tender_documents"):
        key = (row["tender_id"], row["filename"])
        mine = live.get(key)
        if mine is None:
            _insert(con, "tender_documents", row, columns)
            _count(report, "added", "tender_documents")
        elif _fill_blanks(
            con,
            "tender_documents",
            {"tender_id": key[0], "filename": key[1]},
            mine,
            row,
        ):
            _count(report, "filled", "tender_documents")


def _merge_llm_runs(con, src, report, vendors, jobs) -> dict[int, int]:
    """Every old model call is history. One already present is not added twice."""
    columns = _columns(con, "llm_runs")
    known = {
        (int(row["vendor_id"]), row["created_at"], row["status"]): int(row["run_id"])
        for row in con.execute("SELECT run_id, vendor_id, created_at, status FROM llm_runs")
    }
    mapping: dict[int, int] = {}
    for row in _rows(src, "llm_runs"):
        if not _remap_vendor(row, vendors):
            continue
        if row.get("job_id") is not None:
            row["job_id"] = jobs.get(int(row["job_id"]))
        key = (row["vendor_id"], row["created_at"], row["status"])
        if key in known:
            mapping[int(row["run_id"])] = known[key]
            continue
        values = {name: value for name, value in row.items() if name != "run_id"}
        new_id = _insert(con, "llm_runs", values, columns)
        mapping[int(row["run_id"])] = new_id
        known[key] = new_id
        _count(report, "added", "llm_runs")
    return mapping


def _merge_contacts(con, src, report, vendors, runs) -> None:
    columns = _columns(con, "vendor_contacts")
    known = {
        (int(row["vendor_id"]), row["tender_id"], row["channel"], row["source"], row["value"].casefold())
        for row in con.execute(
            "SELECT vendor_id, tender_id, channel, source, value FROM vendor_contacts"
        )
    }
    for row in _rows(src, "vendor_contacts"):
        if not _remap_vendor(row, vendors):
            continue
        if row.get("llm_run_id") is not None:
            row["llm_run_id"] = runs.get(int(row["llm_run_id"]))
        key = (row["vendor_id"], row["tender_id"], row["channel"], row["source"], row["value"].casefold())
        if key in known:
            continue
        values = {name: value for name, value in row.items() if name != "contact_id"}
        _insert(con, "vendor_contacts", values, columns)
        known.add(key)
        _count(report, "added", "vendor_contacts")


def _merge_outreach(con, src, report, vendors, jobs) -> None:
    """Every old attempt comes back, so nobody already mailed is mailed again."""
    columns = _columns(con, "outreach")
    known = {
        (int(row["vendor_id"]), row["tender_id"], row["email"], row["status"], row["sent_at"])
        for row in con.execute(
            "SELECT vendor_id, tender_id, email, status, sent_at FROM outreach"
        )
    }
    for row in _rows(src, "outreach"):
        if not _remap_vendor(row, vendors):
            continue
        if row.get("job_id") is not None:
            row["job_id"] = jobs.get(int(row["job_id"]))
        key = (row["vendor_id"], row["tender_id"], row["email"], row["status"], row["sent_at"])
        if key in known:
            continue
        values = {name: value for name, value in row.items() if name != "outreach_id"}
        _insert(con, "outreach", values, columns)
        known.add(key)
        _count(report, "added", "outreach")


def _merge_job_tenders(con, src, report, jobs) -> None:
    for row in _rows(src, "job_tenders"):
        job_id = jobs.get(int(row["job_id"]))
        if job_id is None:
            continue
        cursor = con.execute(
            "INSERT OR IGNORE INTO job_tenders (job_id, tender_id) VALUES (?, ?)",
            (job_id, row["tender_id"]),
        )
        if cursor.rowcount:
            _count(report, "added", "job_tenders")


def _merge_letter(con, src, report, letter: str) -> None:
    old = src.execute("SELECT * FROM mail_letter WHERE letter_id = 1").fetchone()
    if old is None:
        report.letter = "current"
        return
    mine = con.execute("SELECT letter_id FROM mail_letter WHERE letter_id = 1").fetchone()
    if mine is not None and letter == "current":
        report.letter = "current"
        return
    columns = _columns(con, "mail_letter")
    values = dict(old)
    if mine is not None:
        con.execute("DELETE FROM mail_letter WHERE letter_id = 1")
    _insert(con, "mail_letter", values, columns)
    report.letter = "backup"
