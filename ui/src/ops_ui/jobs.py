"""One background run per stage. The log is written into ui_jobs as it grows."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from pipeline_core.db import ensure_schema, engine, session
from pipeline_core.models import UiJob
from pipeline_core.queries import utcnow
from sqlalchemy import select

Work = Callable[["Log", threading.Event], int]


class Busy(Exception):
    """That stage already has a run that has not finished."""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(f"job {job_id} is already running")


class Log:
    def __init__(self, job_id: int, database: Path) -> None:
        self.job_id = job_id
        self.database = database
        self._lock = threading.Lock()

    def write(self, message: str) -> None:
        line = message if message.endswith("\n") else message + "\n"
        with self._lock:
            with session(engine(self.database)) as current:
                job = current.get(UiJob, self.job_id)
                if job is not None:
                    job.log = (job.log or "") + line


class CaptchaWait:
    def __init__(self, image: Path) -> None:
        self.image = image
        self.answer: str | None = None
        self.event = threading.Event()


_GUARD = threading.Lock()
_STOPS: dict[int, threading.Event] = {}
_CAPTCHA: dict[int, CaptchaWait] = {}
CAPTCHA_TIMEOUT_SECONDS = 180


def start_job(
    database: Path,
    stage: str,
    params: dict,
    work: Work,
) -> int:
    """Insert the run and start it. Raises Busy when this stage is already going."""
    ensure_schema(engine(database))
    with _GUARD:
        with session(engine(database)) as current:
            running = current.scalar(
                select(UiJob.job_id).where(
                    UiJob.stage == stage, UiJob.state.in_(("running", "waiting"))
                )
            )
            if running is not None:
                raise Busy(int(running))
            job = UiJob(
                stage=stage,
                state="running",
                operator="local",
                params_json=json.dumps(params, sort_keys=True),
                log="",
                started_at=utcnow(),
            )
            current.add(job)
            current.flush()
            job_id = int(job.job_id)
        stop = threading.Event()
        _STOPS[job_id] = stop

    threading.Thread(
        target=_execute,
        args=(database, job_id, work, stop),
        daemon=True,
    ).start()
    return job_id


def request_stop(database: Path, job_id: int) -> None:
    stop = _STOPS.get(job_id)
    if stop is not None:
        stop.set()
    waiting = _CAPTCHA.get(job_id)
    if waiting is not None:
        waiting.event.set()
    with session(engine(database)) as current:
        job = current.get(UiJob, job_id)
        if job is not None and job.state in {"running", "waiting"}:
            job.log = (job.log or "") + "Stop requested. Work already saved is kept.\n"


def begin_captcha(database: Path, job_id: int, image: Path) -> CaptchaWait:
    """Mark the run as waiting and hold the image until six characters arrive."""
    wait = CaptchaWait(image)
    _CAPTCHA[job_id] = wait
    _set_state(database, job_id, "waiting")
    return wait


def submit_captcha(job_id: int, code: str) -> bool:
    wait = _CAPTCHA.get(job_id)
    text = (code or "").strip()
    if wait is None or len(text) != 6:
        return False
    wait.answer = text
    wait.event.set()
    return True


def captcha_image(job_id: int) -> Path | None:
    wait = _CAPTCHA.get(job_id)
    if wait is None or not wait.image.is_file():
        return None
    return wait.image


def finish_captcha(job_id: int) -> None:
    _CAPTCHA.pop(job_id, None)


class CaptchaTimedOut(Exception):
    """Nobody typed the six characters before the wait ended."""


def wait_for_captcha(log: Log, stop: threading.Event, image: Path) -> str:
    """Pause this run until the page submits six characters, or the time runs out."""
    log.write(
        "The automatic reader gave up. Type the 6 characters shown on this page. "
        f"You have {CAPTCHA_TIMEOUT_SECONDS // 60} minutes."
    )
    wait = begin_captcha(log.database, log.job_id, image)
    try:
        wait.event.wait(CAPTCHA_TIMEOUT_SECONDS)
    finally:
        finish_captcha(log.job_id)
    if stop.is_set():
        raise CaptchaTimedOut("Stopped while waiting for the captcha.")
    if not wait.answer:
        raise CaptchaTimedOut("Nobody typed the captcha in time. The run ended.")
    _set_state(log.database, log.job_id, "running")
    log.write("Captcha received. The scrape continues.")
    return wait.answer


def _set_state(database: Path, job_id: int, state: str) -> None:
    with session(engine(database)) as current:
        job = current.get(UiJob, job_id)
        if job is not None and job.state in {"running", "waiting"}:
            job.state = state


def get_job(database: Path, job_id: int) -> UiJob | None:
    with session(engine(database)) as current:
        job = current.get(UiJob, job_id)
        if job is not None:
            current.expunge(job)
        return job


def recent_jobs(database: Path, limit: int = 20) -> list[UiJob]:
    ensure_schema(engine(database))
    with session(engine(database)) as current:
        rows = list(
            current.scalars(
                select(UiJob).order_by(UiJob.job_id.desc()).limit(limit)
            )
        )
        for row in rows:
            current.expunge(row)
        return rows


def _execute(database: Path, job_id: int, work: Work, stop: threading.Event) -> None:
    log = Log(job_id, database)
    code = 1
    state = "failed"
    try:
        code = int(work(log, stop) or 0)
        if stop.is_set():
            state = "stopped"
        elif code == 0:
            state = "done"
        else:
            state = "failed"
    except Exception as exc:
        log.write(f"{type(exc).__name__}: {exc}")
        state = "stopped" if stop.is_set() else "failed"
        code = 1
    finally:
        with session(engine(database)) as current:
            job = current.get(UiJob, job_id)
            if job is not None:
                job.state = state
                job.exit_code = code
                job.finished_at = utcnow()
        _STOPS.pop(job_id, None)
