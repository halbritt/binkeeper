"""Run the advisory-vision benchmark over the owner's photographed bins.

Usage (owner-run, results stay local -- they name real bin contents):

    .venv/bin/python scripts/vision_bench.py \
        --model gemini:gemini-3.5-flash \
        --model local:qwen3-vl:8b@http://peecee:11434/v1 \
        --repeats 2 --out ~/binkeeper-bench

Model specs are ``gemini:<model>`` or ``local:<model>@<openai-endpoint>``.
Needs BINKEEPER_GEMINI_API_KEY (or GEMINI_API_KEY) for gemini specs and a
readable blob vault + binkeeper database for the photos.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

import psycopg

from binkeeper.bin_vision import GeminiVisionClient, OllamaVisionClient, VisionClient
from binkeeper.bin_vision_bench import (
    BenchPhoto,
    parse_owner_items,
    render_summary,
    reports_to_json,
    run_model,
)
from binkeeper.blob_vault import blob_store_from_config, open_blob, vault_key_from_config

_PHOTO_QUERY = """
SELECT raw_payload->'metadata'->>'bin_code',
       raw_payload->'metadata'->'photo'->>'sha256',
       content_text
FROM captures
WHERE raw_payload->'metadata'->>'kind' = 'bin_capture'
  AND NULLIF(btrim(raw_payload->'metadata'->'photo'->>'sha256'), '') IS NOT NULL
ORDER BY observed_at
"""


def _load_photos(
    conn: psycopg.Connection, bins: list[str] | None
) -> list[tuple[BenchPhoto, bytes]]:
    from binkeeper.bin_passport import load_bin_passports

    themes = {p.bin_code: p.theme for p in load_bin_passports(conn) if p.theme}
    store = blob_store_from_config()
    key, _ = vault_key_from_config()
    photos: list[tuple[BenchPhoto, bytes]] = []
    for bin_code, digest, content_text in conn.execute(_PHOTO_QUERY):
        if bins and bin_code not in bins:
            continue
        photo = BenchPhoto(
            bin_code=str(bin_code),
            photo_sha256=str(digest),
            owner_theme=str(themes.get(bin_code, "")),
            owner_items=parse_owner_items(str(content_text or "")),
        )
        photos.append((photo, open_blob(conn, store, str(digest), key=key)))
    return photos


def _build_client(spec: str, timeout_s: float) -> tuple[str, VisionClient]:
    provider, _, rest = spec.partition(":")
    if provider == "gemini" and rest:
        return spec, GeminiVisionClient(model=rest, timeout_s=timeout_s)
    if provider == "local" and rest:
        model, separator, endpoint = rest.partition("@")
        if not separator or not model:
            raise SystemExit(f"local spec needs model@endpoint: {spec!r}")
        return spec, OllamaVisionClient(endpoint=endpoint, model=model, timeout_s=timeout_s)
    raise SystemExit(f"unsupported model spec {spec!r} (use gemini:<model> or local:<m>@<url>)")


def _warmup_image() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), (128, 128, 128)).save(buffer, format="JPEG")
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", required=True, dest="models")
    parser.add_argument("--bin", action="append", dest="bins")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout-s", type=float, default=180.0)
    parser.add_argument("--dsn", default="dbname=binkeeper")
    parser.add_argument("--out", type=Path, default=Path.home() / "binkeeper-bench")
    args = parser.parse_args()

    with psycopg.connect(args.dsn) as conn:
        photos = _load_photos(conn, args.bins)
    if not photos:
        raise SystemExit("no bin photos matched")
    print(f"{len(photos)} photos: {', '.join(photo.bin_code for photo, _ in photos)}")

    warmup = _warmup_image()
    reports = []
    for spec in args.models:
        model_id, client = _build_client(spec, args.timeout_s)
        print(f"--- {model_id}")
        reports.append(
            run_model(
                model_id,
                client,
                photos,
                repeats=args.repeats,
                warmup_image=warmup,
                on_progress=print,
            )
        )

    run_dir = args.out / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(reports_to_json(reports), encoding="utf-8")
    summary = render_summary(reports)
    (run_dir / "summary.md").write_text(summary + "\n", encoding="utf-8")
    print()
    print(summary)
    print(f"\nraw results: {run_dir} (owner data -- do not commit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
