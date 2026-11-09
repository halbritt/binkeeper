"""Local, replaceable health status for the optional nightly contents review.

Systemd records both lease failures and child exits, including termination before
the inventory CLI starts. This file is operational status, never inventory evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

Status = Literal["current", "running", "degraded", "stale", "unknown"]


@dataclass(frozen=True)
class AdvisoryStatus:
    state: Status
    reason: str
    last_success: str | None = None
    last_failure: str | None = None

    @property
    def label(self) -> str:
        return {
            "current": "up to date",
            "running": "running",
            "degraded": "unavailable",
            "stale": "overdue",
            "unknown": "status unavailable",
        }[self.state]

    @property
    def success_time(self) -> str:
        return _display_time(self.last_success)

    @property
    def failure_time(self) -> str:
        return _display_time(self.last_failure)


def _display_time(value: str | None) -> str:
    return (
        datetime.fromisoformat(value).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        if value
        else "not recorded"
    )


def status_path() -> Path:
    return Path(
        os.environ.get(
            "BINKEEPER_LABEL_DRIFT_STATUS_PATH", "/var/lib/binkeeper-advisory/label-drift.json"
        )
    )


def _read(path: Path) -> dict[str, str | None]:
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("state"), str)
        or value["state"] not in {"running", "success", "failed"}
    ):
        raise ValueError("invalid advisory status")
    for field in ("started_at", "last_success", "last_failure"):
        timestamp = value.get(field)
        if timestamp is not None and (
            not isinstance(timestamp, str) or datetime.fromisoformat(timestamp).tzinfo is None
        ):
            raise ValueError("invalid advisory time")
    return value


def _previous(path: Path) -> dict[str, str | None]:
    try:
        return _read(path)
    except (OSError, ValueError):
        return {}


def _write(path: Path, record: dict[str, str | None]) -> None:
    # The service's StateDirectory owns this directory. Never create arbitrary
    # parent paths or silently turn a failed status write into a successful run.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
        try:
            json.dump(record, tmp, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def record_start(path: Path, *, now: datetime | None = None) -> None:
    previous = _previous(path)
    _write(
        path,
        {
            "state": "running",
            "started_at": (now or datetime.now(UTC)).isoformat(),
            "last_success": previous.get("last_success"),
            "last_failure": previous.get("last_failure"),
            "reason": "The local photo review is running.",
        },
    )


def record_finish(
    path: Path,
    *,
    service_result: str,
    exit_code: str,
    exit_status: str,
    now: datetime | None = None,
) -> None:
    previous = _previous(path)
    when = (now or datetime.now(UTC)).isoformat()
    success = service_result == "success" and exit_code == "exited" and exit_status == "0"
    reason = "The last local photo review completed."
    if not success:
        reason = {
            "75": "The local model could not be leased or its lease was lost. Check GPU capacity.",
            "5": "Some photos could not be analyzed. Check the local model before the next run.",
            "124": "The local review timed out. Check the model service before the next run.",
        }.get(exit_status, "The local review did not complete. Check its service journal.")
    _write(
        path,
        {
            "state": "success" if success else "failed",
            "started_at": previous.get("started_at"),
            "last_success": when if success else previous.get("last_success"),
            "last_failure": previous.get("last_failure") if success else when,
            "reason": reason,
        },
    )


def load_status(path: Path | None = None, *, now: datetime | None = None) -> AdvisoryStatus:
    try:
        record = _read(path or status_path())
    except (OSError, ValueError):
        return AdvisoryStatus(
            "unknown", "No readable review status is available. Check the nightly service."
        )
    clock = now or datetime.now(UTC)
    last_success = record.get("last_success")
    last_failure = record.get("last_failure")
    state: Status
    if record["state"] == "failed":
        state = "degraded"
    elif record["state"] == "running":
        started = record.get("started_at")
        if started and clock - datetime.fromisoformat(started) < timedelta(hours=4):
            state = "running"
        else:
            return AdvisoryStatus(
                "stale",
                "The last run has no completion record. Check its service journal.",
                last_success,
                last_failure,
            )
    elif not last_success or clock - datetime.fromisoformat(last_success) > timedelta(hours=28):
        return AdvisoryStatus(
            "stale",
            "No completed review in the last 28 hours. Check the nightly timer.",
            last_success,
            last_failure,
        )
    else:
        state = "current"
    return AdvisoryStatus(
        state, record.get("reason") or "Review status unavailable.", last_success, last_failure
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=("start", "finish"))
    args = parser.parse_args()
    try:
        if args.event == "start":
            record_start(status_path())
        else:
            record_finish(
                status_path(),
                service_result=os.environ.get("SERVICE_RESULT", ""),
                exit_code=os.environ.get("EXIT_CODE", ""),
                exit_status=os.environ.get("EXIT_STATUS", ""),
            )
    except OSError:
        print("Could not record nightly advisory status.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
