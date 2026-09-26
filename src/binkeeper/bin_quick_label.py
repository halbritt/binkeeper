"""Create and print a bin label without waiting for photo analysis."""

from __future__ import annotations

from dataclasses import dataclass
from subprocess import TimeoutExpired
from uuid import UUID

import psycopg

from binkeeper.bin_b1 import B1PrintUnknown
from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_label import BinLabelError, make_label_job, send_label_job
from binkeeper.bin_passport import load_known_bin_codes
from binkeeper.bin_register import QuickLabelIntent, RegisterResult, register_bin
from binkeeper.sites import SITE_PREFIXES


class QuickLabelError(ValueError):
    """A quick-label request cannot safely create a new bin."""


@dataclass(frozen=True)
class QuickLabelRequest:
    site: str
    theme: str
    contents: str
    printer: str
    copies: int
    action_id: str


@dataclass(frozen=True)
class QuickLabelResult:
    bin_code: str
    site: str
    already_existed: bool
    printed: bool
    print_target: str
    print_error: str | None
    print_unknown: bool = False


def create_quick_label(
    request: QuickLabelRequest,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> QuickLabelResult:
    """Atomically assign a code and register the bin, then attempt one local print."""
    site = request.site.strip()
    theme = request.theme.strip()
    contents = request.contents.strip()
    if site not in SITE_PREFIXES:
        raise QuickLabelError("Choose a site for the new bin.")
    if not theme or len(theme) > 120:
        raise QuickLabelError("Enter a bin theme of at most 120 characters.")
    if len(contents) > 1200:
        raise QuickLabelError("Keep contents to at most 1200 characters.")
    if request.printer not in {"cups", "niimbot-b1"}:
        raise QuickLabelError("Choose one of the available printers.")
    if request.copies not in {1, 2}:
        raise QuickLabelError("Choose one or two labels.")
    try:
        action_id = str(UUID(request.action_id))
    except (ValueError, AttributeError) as exc:
        raise QuickLabelError("Reload the form and try again.") from exc

    source_label = f"quick-label:{action_id}"
    result: RegisterResult
    from binkeeper.db import connect

    with connect() as conn, conn.transaction():
        _lock(conn, source_label)
        existing = conn.execute(
            """
            SELECT raw_payload->'metadata'
            FROM captures
            WHERE tenant_id = %s AND corpus_id = %s
              AND raw_payload->>'source_label' = %s
            LIMIT 1
            """,
            (tenant_id, corpus_id, source_label),
        ).fetchone()
        if existing is not None:
            metadata = existing[0]
            profile = metadata.get("bin_profile") if isinstance(metadata, dict) else None
            if (
                not isinstance(metadata, dict)
                or not isinstance(profile, dict)
                or metadata.get("site") != site
                or profile.get("theme") != theme
                or metadata.get("contents_text", "") != contents
                or metadata.get("quick_label")
                != {"action_id": action_id, "printer": request.printer, "copies": request.copies}
            ):
                raise QuickLabelError("This label request was already used with different details.")
            code = metadata.get("bin_code")
            if not isinstance(code, str) or not code:
                raise QuickLabelError("The existing label request has no bin code.")
            return QuickLabelResult(code, site, True, False, "", None)

        prefix = SITE_PREFIXES[site]
        _lock(conn, f"bin-code:{prefix}")
        code = _next_code(
            prefix, load_known_bin_codes(conn, tenant_id=tenant_id, corpus_id=corpus_id)
        )
        job = make_label_job(
            code,
            printer=request.printer,
            theme=theme,
            site=site,
            contents=contents,
            copies=request.copies,
        )
        result = register_bin(
            conn,
            bin_code=code,
            site=site,
            theme=theme,
            contents_text=contents,
            source_label=source_label,
            quick_label_intent=QuickLabelIntent(action_id, request.printer, request.copies),
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
    if result.already_existed:
        raise QuickLabelError("The assigned code was already registered. Reload and try again.")
    try:
        plan = send_label_job(job)
    except BinLabelError as exc:
        return QuickLabelResult(
            code,
            site,
            False,
            False,
            "",
            str(exc),
            isinstance(exc, B1PrintUnknown) or isinstance(exc.__cause__, TimeoutExpired),
        )
    return QuickLabelResult(code, site, False, True, plan.target, None)


def _lock(conn: psycopg.Connection, name: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (name,))


def _next_code(prefix: str, known_codes: list[str]) -> str:
    highest = 0
    for code in known_codes:
        if code.startswith(f"{prefix}-"):
            suffix = code[len(prefix) + 1 :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{prefix}-{highest + 1:03d}"
