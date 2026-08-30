"""
backup_scheduler.py — the timer half of automatic backups.

One daemon thread, started from `app/main.py`'s lifespan, waking on each minute boundary and
firing every `BackupConfig` whose cron expression came due. Deliberately in-process rather
than a systemd timer: it needs no install/update surface, it behaves identically in dev, and
it dies with the service that owns the jobs it starts.

Due-ness is decided against `BackupConfig.last_run_at`, not against "is it exactly now":
`croniter(expr, now).get_prev()` gives the most recent fire time at or before now, and the
backup runs when that is newer than the last run. That both makes a double-tick within one
minute harmless and makes a window missed while the server was down fire **once** on the next
boot instead of being skipped silently — which is what you want from a backup and not what a
plain "does the current minute match" check gives you.

A schedule with **no** `last_run_at` is a schedule that has never run, which is not the same
as one that missed a window: catching up there would mean that saving "daily at 03:00" at two
in the afternoon immediately takes a backup, because 03:00 today is technically in the past.
So the first tick that sees such a row only stamps a baseline, and the next real window is
the one that fires.

Every row is evaluated inside its own try/except: a typo'd cron expression or an unreachable
destination must never take the thread down and stop backups for every other project.
"""

import logging
import threading
from datetime import datetime, timedelta

from croniter import croniter

from app.models.database import SessionLocal
from app.models.orm import BackupConfig, Project
from app.services import backup_service, docker_service

TICK_SECONDS = 60
# How far back a missed window is still worth catching up on. Beyond this the server was
# down long enough that firing every skipped nightly at once would be noise, not safety.
CATCHUP_WINDOW = timedelta(hours=25)

_log = logging.getLogger("uvicorn")
_thread: threading.Thread | None = None
_stop = threading.Event()


def is_due(expression: str, last_run_at: datetime | None, now: datetime) -> bool:
    """Whether `expression` came due since `last_run_at`.

    `last_run_at is None` means the schedule has never run, so there is no missed window to
    catch up on — the caller stamps a baseline instead of firing (see the module docstring).
    """
    if last_run_at is None:
        return False
    previous = croniter(expression, now).get_prev(datetime)
    if now - previous > CATCHUP_WINDOW:
        return False
    return previous > last_run_at


def _scope_label(project: Project | None) -> str:
    return project.name if project is not None else "the database"


def _tick(now: datetime) -> int:
    """Fire every due schedule. Returns how many backups were started."""
    started = 0
    db = SessionLocal()
    try:
        configs = (db.query(BackupConfig)
                   .filter(BackupConfig.enabled == True,                      # noqa: E712
                           BackupConfig.schedule_cron.isnot(None))
                   .all())
        for config in configs:
            try:
                expression = (config.schedule_cron or "").strip()
                if not expression or not croniter.is_valid(expression):
                    continue
                if config.last_run_at is None:
                    # First sight of this schedule: record where the clock is, so the next
                    # window fires and the past one is not mistaken for a missed run.
                    config.last_run_at = now
                    config.last_message = (
                        f"schedule armed at {now.isoformat(timespec='seconds')} — "
                        f"first backup at the next scheduled time"
                    )
                    db.commit()
                    continue
                if not is_due(expression, config.last_run_at, now):
                    continue
                project = config.project
                scope = backup_service.scope_name(project)
                job = docker_service.get_job(backup_service.create_job_key(scope))
                if job is not None and job.status == "running":
                    # Still busy with the previous one — try again next tick rather than
                    # clobbering its log by re-registering the same key.
                    continue
                config.last_run_at = now
                config.last_status = "ok"
                config.last_message = f"scheduled backup started at {now.isoformat(timespec='seconds')}"
                db.commit()
                backup_service.create_backup(config.project_id, kind="scheduled")
                started += 1
                _log.info("Scheduled backup started for %s", _scope_label(project))
            except Exception as exc:       # one bad row must not stop the others
                db.rollback()
                _log.warning("Scheduled backup for config %s failed to start: %s",
                             config.id, exc)
                try:
                    config.last_status = "error"
                    config.last_message = str(exc)[:2000]
                    db.commit()
                except Exception:
                    db.rollback()
    finally:
        db.close()
    return started


def _run() -> None:
    while not _stop.is_set():
        # Sleep to the next minute boundary so a schedule fires close to its stated minute
        # regardless of when the server started.
        now = datetime.utcnow()
        delay = TICK_SECONDS - (now.second + now.microsecond / 1_000_000)
        if _stop.wait(max(1.0, delay)):
            return
        try:
            _tick(datetime.utcnow())
        except Exception as exc:           # the loop outlives anything a tick can raise
            _log.warning("Backup scheduler tick failed: %s", exc)


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="backup-scheduler", daemon=True)
    _thread.start()
    _log.info("Backup scheduler started (tick %ds)", TICK_SECONDS)


def stop(timeout: float = 2.0) -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
