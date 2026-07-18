"""Owned exact and lexical inventory search over local BinKeeper evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

import psycopg

from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_passport import BinPassport, load_bin_passports

SearchStatus = Literal["ok", "no_results"]
MatchKind = Literal["exact", "lexical"]
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+", re.IGNORECASE)


class InventorySearchError(ValueError):
    """An inventory search request is invalid."""


@dataclass(frozen=True)
class InventorySearchHit:
    bin_code: str
    match_kind: MatchKind
    matched_fields: tuple[str, ...]
    is_current: bool
    evidence_refs: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "bin_code": self.bin_code,
            "match_kind": self.match_kind,
            "matched_fields": list(self.matched_fields),
            "is_current": self.is_current,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class InventorySearchResult:
    query: str
    status: SearchStatus
    hits: tuple[InventorySearchHit, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "query": self.query,
            "status": self.status,
            "hits": [hit.to_json() for hit in self.hits],
        }


def search_inventory(
    conn: psycopg.Connection,
    query: str,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
    limit: int = 20,
) -> InventorySearchResult:
    """Search current passports first, then clearly labeled historical evidence."""
    cleaned = query.strip()
    tokens = _tokens(cleaned)
    if not tokens:
        raise InventorySearchError("query must contain a searchable token")
    if limit < 1 or limit > 100:
        raise InventorySearchError("limit must be between 1 and 100")

    current_hits: list[InventorySearchHit] = []
    current_matches: set[str] = set()
    for passport in load_bin_passports(conn, tenant_id=tenant_id, corpus_id=corpus_id):
        matched = _current_fields(passport, cleaned, tokens)
        if not matched:
            continue
        current_matches.add(passport.bin_code)
        current_hits.append(
            InventorySearchHit(
                bin_code=passport.bin_code,
                match_kind="exact"
                if passport.bin_code.casefold() == cleaned.casefold()
                else "lexical",
                matched_fields=matched,
                is_current=True,
                evidence_refs=passport.provenance_refs or (f"bin:{passport.bin_code}",),
            )
        )

    historical_hits = _historical_hits(
        conn,
        cleaned,
        tokens,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
        exclude_bin_codes=current_matches,
    )
    ordered = sorted(
        [*current_hits, *historical_hits],
        key=lambda hit: (
            not hit.is_current,
            hit.match_kind != "exact",
            hit.bin_code,
            hit.evidence_refs,
        ),
    )[:limit]
    return InventorySearchResult(
        query=cleaned,
        status="ok" if ordered else "no_results",
        hits=tuple(ordered),
    )


def _current_fields(
    passport: BinPassport, query: str, query_tokens: frozenset[str]
) -> tuple[str, ...]:
    values: tuple[tuple[str, str], ...] = (
        ("bin_code", passport.bin_code),
        ("theme", passport.theme or ""),
        ("current_contents", " ".join(passport.sibling_contents[-1:])),
        ("accepts", " ".join(passport.accepts)),
        ("owner_phrase", passport.owner_phrase or ""),
    )
    return tuple(
        field
        for field, value in values
        if (field == "bin_code" and value.casefold() == query.casefold())
        or _lexical_match(value, query_tokens)
    )


def _historical_hits(
    conn: psycopg.Connection,
    query: str,
    query_tokens: frozenset[str],
    *,
    tenant_id: str,
    corpus_id: str,
    exclude_bin_codes: set[str],
) -> list[InventorySearchHit]:
    rows = conn.execute(
        """
        SELECT id::text, external_id, payload
        FROM capture_evidence
        WHERE tenant_id = %s AND corpus_id = %s
        ORDER BY captured_at DESC, seq DESC
        """,
        (tenant_id, corpus_id),
    ).fetchall()
    hits: list[InventorySearchHit] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for capture_id, _external_id, payload in rows:
        if not isinstance(payload, dict):
            continue
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            continue
        bin_code = metadata.get("bin_code")
        if not isinstance(bin_code, str) or bin_code in exclude_bin_codes:
            continue
        profile = metadata.get("bin_profile")
        profile = profile if isinstance(profile, dict) else {}
        values = (
            ("historical_theme", profile.get("theme")),
            ("historical_contents", metadata.get("contents_text")),
        )
        matched = tuple(
            field
            for field, value in values
            if isinstance(value, str) and _lexical_match(value, query_tokens)
        )
        if not matched:
            continue
        identity = (bin_code, matched)
        if identity in seen:
            continue
        seen.add(identity)
        hits.append(
            InventorySearchHit(
                bin_code=bin_code,
                match_kind="exact"
                if any(
                    isinstance(value, str) and value.casefold() == query.casefold()
                    for _field, value in values
                )
                else "lexical",
                matched_fields=matched,
                is_current=False,
                evidence_refs=(f"capture_evidence:{capture_id}",),
            )
        )
    return hits


def _tokens(value: str) -> frozenset[str]:
    return frozenset(match.group(0).casefold() for match in _TOKEN_RE.finditer(value))


def _lexical_match(value: str, query_tokens: frozenset[str]) -> bool:
    return bool(value.strip()) and query_tokens.issubset(_tokens(value))
