"""Virtual bins: saved queries over physical bins (BINK-34).

Reorganize the index, never the shelves. A virtual bin is an owner-defined
name plus a query ("soldering", "camping stove fuel"); membership is computed
from current passports at read time, so it can span physical bins and sites
and costs nothing to try. Definitions are append-only owner evidence (the
latest capture per name wins; an empty query retires the name), and no code
path here touches physical state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_passport import BinPassport

VIRTUAL_BIN_SCHEMA_VERSION = "virtual_bin.v1"


class BinVirtualError(ValueError):
    """Domain root for virtual-bin failures."""


@dataclass(frozen=True)
class VirtualBinMember:
    """One physical bin matched by a virtual bin's query."""

    bin_code: str
    site: str | None
    theme: str | None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one member."""
        return {"bin_code": self.bin_code, "site": self.site, "theme": self.theme}


@dataclass(frozen=True)
class VirtualBin:
    """One saved query with its computed members."""

    name: str
    query: str
    members: tuple[VirtualBinMember, ...]

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one virtual bin."""
        return {
            "name": self.name,
            "query": self.query,
            "member_count": len(self.members),
            "members": [member.to_json() for member in self.members],
        }


def define_virtual_bin(
    conn: psycopg.Connection,
    *,
    name: str,
    query: str,
    action_id: str,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> bool:
    """Append one virtual-bin definition snapshot. Idempotent per action.

    An empty query retires the name. Returns True when the capture is new.
    """
    from binkeeper.personal_memory import CaptureRequest, PersonalMemoryService

    slug = name.strip().lower()
    if not slug:
        raise BinVirtualError("a virtual bin needs a name")
    if not action_id.strip():
        raise BinVirtualError("action_id is required")
    idempotency_key = f"virtualbin:{slug}:{action_id.strip()}"
    result = PersonalMemoryService(conn).capture(
        CaptureRequest(
            text=f"virtual bin {slug}: {query.strip() or '(retired)'}",
            capture_type="observation",
            privacy_tier=2,
            observed_at=_stable_time(conn, idempotency_key, tenant_id, corpus_id),
            source_label="virtual-bin",
            idempotency_key=idempotency_key,
            metadata={
                "kind": "virtual_bin",
                "schema_version": VIRTUAL_BIN_SCHEMA_VERSION,
                "name": slug,
                "query": query.strip(),
            },
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
    )
    return not result.already_existed


def load_virtual_definitions(
    conn: psycopg.Connection,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> dict[str, str]:
    """Fold the latest non-retired definition per name. Read-only."""
    rows = conn.execute(
        """
        SELECT raw_payload->'metadata'->>'name', raw_payload->'metadata'->>'query'
        FROM captures
        WHERE tenant_id = %s AND corpus_id = %s
          AND raw_payload->'metadata'->>'kind' = 'virtual_bin'
        ORDER BY observed_at, external_id
        """,
        (tenant_id, corpus_id),
    ).fetchall()
    latest: dict[str, str] = {}
    for name, query in rows:
        if not name:
            continue
        latest[str(name)] = str(query or "")
    return {name: query for name, query in latest.items() if query.strip()}


def virtual_members(query: str, passports: list[BinPassport]) -> tuple[VirtualBinMember, ...]:
    """Match passports whose recorded text carries every query token. Pure."""
    from binkeeper.bin_route import _tokens_for

    query_tokens = _tokens_for(query)
    if not query_tokens:
        return ()
    members = []
    for passport in passports:
        text = " ".join(
            (
                passport.theme or "",
                passport.owner_phrase or "",
                *passport.accepts,
                *passport.examples,
                *passport.sibling_contents,
            )
        )
        if query_tokens <= _tokens_for(text):
            members.append(
                VirtualBinMember(
                    bin_code=passport.bin_code,
                    site=passport.current_site,
                    theme=passport.theme,
                )
            )
    return tuple(sorted(members, key=lambda member: member.bin_code))


def list_virtual_bins(
    conn: psycopg.Connection,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[VirtualBin]:
    """Compute every virtual bin's current membership. Read-only."""
    from binkeeper.bin_passport import load_bin_passports

    definitions = load_virtual_definitions(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    if not definitions:
        return []
    passports = list(load_bin_passports(conn, tenant_id=tenant_id, corpus_id=corpus_id))
    return [
        VirtualBin(name=name, query=query, members=virtual_members(query, passports))
        for name, query in sorted(definitions.items())
    ]


def matching_virtual_names(query: str, definitions: dict[str, str]) -> list[str]:
    """Names whose saved query shares a token with a search query. Pure."""
    from binkeeper.bin_route import _tokens_for

    tokens = _tokens_for(query)
    return sorted(
        name for name, saved in definitions.items() if tokens & _tokens_for(f"{name} {saved}")
    )


def _stable_time(
    conn: psycopg.Connection, idempotency_key: str, tenant_id: str, corpus_id: str
) -> datetime:
    """Use now() on first write and the stored time on a replay."""
    row = conn.execute(
        """
        SELECT observed_at FROM captures
        WHERE tenant_id = %s AND corpus_id = %s
          AND raw_payload->>'source_label' = 'virtual-bin'
          AND raw_payload->>'idempotency_key' = %s
        LIMIT 1
        """,
        (tenant_id, corpus_id, idempotency_key),
    ).fetchone()
    if row is not None and isinstance(row[0], datetime):
        return row[0]
    return datetime.now(UTC)
