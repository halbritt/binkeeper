"""RFC 0088 T3b — peripheral-OCR harvester: corroborate bin locations from photos.

The second passive-harvest source (after the photo-GPS backfill in ``bin_harvest``).
A single registration/drop photo often shows *more than one* labeled bin -- the one
it is about plus others on the same shelf -- so this harvester OCRs **every** bin-label
code visible in the frame and records a *corroborative-only* ``location_observation``
for each one it can trust, at the site the photo's GPS resolves to. Zero new sensing:
it reads bytes already in the A11 vault and GPS already riding in the capture.

Trust is bounded on three sides so a misread never poisons a location:

* **Known-bin gate** — an OCR'd code is recorded only if it is a bin the registry
  already knows (``load_known_bin_codes``); an invented or misread code corroborates
  nothing. Peripheral OCR can only *refresh* bins you already have, never mint one.
* **Geofence abstain** — the site is resolved from the photo's GPS with the same
  ambiguity/too-loose abstain as the photo-GPS harvester (``resolve_site``). No
  resolved site → the photo is not even OCR'd (a location signal with no location is
  worthless, and the vision call is expensive).
* **Learned reliability** — ``peripheral_ocr`` carries a middling Beta prior
  (``BIN_SOURCE_PRIORS``); like every source it must earn trust against the move
  ledger before it can lift confidence or (gated, default-off) emit a ``contradict``
  shock. An observation never relocates a bin (the T3b invariant).

Layering (RFC 0012 pure/IO split): the geofence + strength math is reused from
``bin_harvest`` (pure, already unit-tested); this module is the IO worker. The vision
read and the vault fetch are injected (``read_codes`` / ``open_photo``) so the worker
is unit-testable with no live model and no vault.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import psycopg

from binkeeper.bin_harvest import (
    BIN_HARVEST_EMIT_CONTRADICTIONS,
    SiteAnchor,
    gps_strength,
    load_sites,
    resolve_site,
)
from binkeeper.bin_inventory import (
    BIN_CONTRADICT_TRUST_THRESHOLD as _CONTRADICT_TRUST,
)
from binkeeper.bin_inventory import (
    DEFAULT_BIN_CORPUS_ID,
    DEFAULT_BIN_TENANT_ID,
    LocationObservation,
    bin_where,
    compute_source_reliabilities,
    observation_contradicts,
    record_event,
    record_observation,
    source_reliability,
)
from binkeeper.bin_passport import load_known_bin_codes

# --- tunables (BINKEEPER_-prefixed, read at module top per RFC 0012) -----------

PERIPHERAL_OCR_SOURCE: Final[str] = "peripheral_ocr"
BIN_CAPTURE_KIND: Final[str] = "bin_capture"
# Optional per-run cap on how many photos to OCR (0 = no cap). Vision calls are
# expensive and evict peecee's pinned model; a cap keeps a large backfill bounded.
BIN_OCR_MAX_PHOTOS: Final[int] = int(os.environ.get("BINKEEPER_BIN_OCR_MAX_PHOTOS", "0"))


class BinOcrHarvestError(Exception):
    """Domain root for peripheral-OCR harvester failures (RFC 0012 per-stage family)."""


class CodeReader(Protocol):
    """Reads every legible bin-label code from one photo (never raises; [] on failure)."""

    def __call__(self, image: bytes) -> list[str]:
        """Return the bin codes visible in ``image`` (empty when none/unreadable)."""
        ...


class PhotoOpener(Protocol):
    """Fetches one photo's plaintext bytes from the vault by plaintext sha256 (None if absent)."""

    def __call__(self, conn: psycopg.Connection, sha256: str) -> bytes | None:
        """Return the decrypted photo bytes, or None if it can't be fetched/decrypted."""
        ...


@dataclass
class OcrHarvestSummary:
    """The outcome of one peripheral-OCR harvest pass over the bin captures."""

    scanned: int = 0
    sites_configured: int = 0
    photos_read: int = 0
    codes_matched: int = 0
    unknown_codes_dropped: int = 0
    recorded: int = 0
    already: int = 0
    skipped_no_photo: int = 0
    skipped_no_gps: int = 0
    skipped_no_site: int = 0
    skipped_ambiguous: int = 0
    skipped_too_loose: int = 0
    skipped_photo_unreadable: int = 0
    skipped_malformed: int = 0
    contradictions_emitted: int = 0
    contradictions_suppressed: int = 0
    emit_contradictions: bool = False

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for the summary."""
        return {
            "scanned": self.scanned,
            "sites_configured": self.sites_configured,
            "photos_read": self.photos_read,
            "codes_matched": self.codes_matched,
            "unknown_codes_dropped": self.unknown_codes_dropped,
            "recorded": self.recorded,
            "already": self.already,
            "skipped_no_photo": self.skipped_no_photo,
            "skipped_no_gps": self.skipped_no_gps,
            "skipped_no_site": self.skipped_no_site,
            "skipped_ambiguous": self.skipped_ambiguous,
            "skipped_too_loose": self.skipped_too_loose,
            "skipped_photo_unreadable": self.skipped_photo_unreadable,
            "skipped_malformed": self.skipped_malformed,
            "contradictions_emitted": self.contradictions_emitted,
            "contradictions_suppressed": self.contradictions_suppressed,
            "emit_contradictions": self.emit_contradictions,
        }


def _parse_gps(meta: Mapping[str, Any]) -> tuple[float, float, float | None] | None:
    """Extract (lat, lon, accuracy_m) from a bin_capture metadata object, or None."""
    gps = meta.get("gps")
    if not isinstance(gps, dict):
        return None
    lat, lon = gps.get("lat"), gps.get("lon")
    if lat is None or lon is None:
        return None
    try:
        accuracy = gps.get("accuracy_m")
        return float(lat), float(lon), (float(accuracy) if accuracy is not None else None)
    except (TypeError, ValueError):
        return None


def _photo_sha(meta: Mapping[str, Any]) -> str | None:
    """The plaintext sha256 of the capture's photo blob (the bin_register contract)."""
    photo = meta.get("photo")
    if not isinstance(photo, dict):
        return None
    sha = photo.get("sha256") or photo.get("blob_ref")
    if isinstance(sha, str) and sha.strip():
        return sha.strip()
    return None


def _observed_at(meta: Mapping[str, Any], fallback: datetime) -> datetime:
    """The capture's observation time — metadata.captured_at if parseable, else fallback."""
    captured = meta.get("captured_at")
    if isinstance(captured, str):
        try:
            return datetime.fromisoformat(captured)
        except ValueError:
            return fallback
    return fallback


def _default_code_reader() -> CodeReader:
    """The live OCR reader: Qwen3-VL on peecee via the bin vision worker."""
    from binkeeper.bin_vision import OllamaVisionClient, read_visible_bin_codes

    client = OllamaVisionClient()

    def read(image: bytes) -> list[str]:
        return read_visible_bin_codes(client, image)

    return read


def _default_photo_opener() -> PhotoOpener:
    """The live vault opener: decrypt a photo blob from the configured A11 store."""
    from binkeeper.blob_vault import (
        BlobVaultError,
        blob_store_from_config,
        open_blob,
        vault_key_from_config,
    )

    store = blob_store_from_config()
    key, _key_ref = vault_key_from_config()

    def opener(conn: psycopg.Connection, sha256: str) -> bytes | None:
        try:
            return open_blob(conn, store, sha256, key=key)
        except BlobVaultError:
            return None

    return opener


def harvest_peripheral_ocr(
    conn: psycopg.Connection,
    *,
    sites: Mapping[str, SiteAnchor] | None = None,
    now: datetime | None = None,
    emit_contradictions: bool | None = None,
    read_codes: CodeReader | None = None,
    open_photo: PhotoOpener | None = None,
    max_photos: int = BIN_OCR_MAX_PHOTOS,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> OcrHarvestSummary:
    """OCR every bin label in each geolocated bin photo and record corroborations.

    For each ``bin_capture`` that carries both a photo blob and a usable GPS fix, the
    site is resolved server-side (abstaining on geofence ambiguity / a too-loose fix);
    only then is the photo OCR'd. Every OCR'd code that is a *known* bin gets one
    idempotent ``peripheral_ocr`` observation at that site. A gated ``contradict`` shock
    is written only when (a) the geofence was unambiguous, (b) ``peripheral_ocr`` has
    earned trust, and (c) emission is enabled — never a relocation. Idempotent: a second
    pass dedupes on (capture, code).
    """
    resolved_sites = dict(sites) if sites is not None else load_sites()
    do_contradict = (
        BIN_HARVEST_EMIT_CONTRADICTIONS if emit_contradictions is None else emit_contradictions
    )
    when = now or datetime.now(UTC)
    summary = OcrHarvestSummary(
        sites_configured=sum(1 for a in resolved_sites.values() if a.lat is not None),
        emit_contradictions=do_contradict,
    )

    reader = read_codes if read_codes is not None else _default_code_reader()
    if open_photo is not None:
        opener = open_photo
    else:
        try:
            opener = _default_photo_opener()
        except Exception as exc:  # a missing/invalid vault config -> a clear domain error
            raise BinOcrHarvestError(f"blob vault unavailable for peripheral OCR: {exc}") from exc

    # Known bins bound what OCR is allowed to corroborate (uppercased for a robust match
    # against the OCR output, which is already normalized to AGR-001 form).
    known = {
        code.strip().upper()
        for code in load_known_bin_codes(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    }

    # Reliability is a pre-existing track record; compute it once for the run's gating.
    reliabilities = compute_source_reliabilities(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    ocr_reliability = reliabilities.get(
        PERIPHERAL_OCR_SOURCE, source_reliability(PERIPHERAL_OCR_SOURCE, hits=0, misses=0)
    )

    rows = conn.execute(
        """
        SELECT external_id, raw_payload, observed_at, imported_at
        FROM captures
        WHERE raw_payload->'metadata'->>'kind' = %s
        ORDER BY imported_at
        """,
        (BIN_CAPTURE_KIND,),
    ).fetchall()

    for external_id, raw_payload, observed_at, imported_at in rows:
        summary.scanned += 1
        meta = raw_payload.get("metadata") if isinstance(raw_payload, dict) else None
        if not isinstance(meta, dict):
            summary.skipped_malformed += 1
            continue
        photo_sha = _photo_sha(meta)
        if photo_sha is None:
            summary.skipped_no_photo += 1
            continue
        fix = _parse_gps(meta)
        if fix is None:
            summary.skipped_no_gps += 1
            continue
        lat, lon, accuracy = fix
        resolution = resolve_site(lat, lon, resolved_sites, accuracy_m=accuracy)
        if not resolution.resolved:
            if resolution.ambiguous:
                summary.skipped_ambiguous += 1
            elif resolution.too_loose:
                summary.skipped_too_loose += 1
            else:
                summary.skipped_no_site += 1
            continue
        if max_photos and summary.photos_read >= max_photos:
            break
        image = opener(conn, photo_sha)
        if not image:
            summary.skipped_photo_unreadable += 1
            continue
        summary.photos_read += 1
        observed = _observed_at(meta, observed_at or imported_at or when)
        strength = gps_strength(accuracy)

        for code in reader(image):
            normalized = code.strip().upper()
            if normalized not in known:
                # A misread or a bin we don't know: corroborate nothing (the safety gate).
                summary.unknown_codes_dropped += 1
                continue
            summary.codes_matched += 1
            result = record_observation(
                conn,
                bin_code=normalized,
                observed_site=resolution.site,  # type: ignore[arg-type]
                source_kind=PERIPHERAL_OCR_SOURCE,
                observed_at=observed,
                observation_strength=strength,
                evidence_ref=external_id,
                idempotency_key=f"peripheral-ocr:{external_id}:{normalized}",
                metadata={
                    "lat": lat,
                    "lon": lon,
                    "accuracy_m": accuracy,
                    "photo_sha256": photo_sha,
                    "geofence_candidates": list(resolution.candidates),
                },
                tenant_id=tenant_id,
                corpus_id=corpus_id,
            )
            if result.already_existed:
                summary.already += 1
            else:
                summary.recorded += 1

            # A cross-site disagreement: the geofence (unambiguous here) places the OCR'd
            # bin at a different site than the move ledger. Escalate to a shock only when
            # emission is enabled AND the source has earned trust.
            location = bin_where(conn, normalized, tenant_id=tenant_id, corpus_id=corpus_id)
            observation = LocationObservation(
                seq=0,
                bin_code=normalized,
                observed_site=resolution.site,  # type: ignore[arg-type]
                source_kind=PERIPHERAL_OCR_SOURCE,
                observed_at=observed,
                observation_strength=strength,
            )
            if observation_contradicts(observation, location):
                if do_contradict and ocr_reliability >= _CONTRADICT_TRUST:
                    shock = record_event(
                        conn,
                        event_kind="contradict",
                        bin_code=normalized,
                        occurred_at=observed,
                        source_label=PERIPHERAL_OCR_SOURCE,
                        idempotency_key=f"peripheral-ocr-contradict:{external_id}:{normalized}",
                        metadata={
                            "observed_site": resolution.site,
                            "ledger_site": location.site,
                            "evidence_ref": external_id,
                            "reliability": ocr_reliability,
                        },
                        tenant_id=tenant_id,
                        corpus_id=corpus_id,
                    )
                    if not shock.already_existed:
                        summary.contradictions_emitted += 1
                else:
                    summary.contradictions_suppressed += 1

    return summary
