"""RFC 0088 T4 / RFC 0093 P5 vision worker for BinKeeper.

Turns photos of a bin's contents (plus the owner's free-text notes) into a
PROVISIONAL bin-label proposal. Vision output is advisory: it never mutates
inventory, never writes location, and the owner confirms before anything is
captured or printed.

Three backends implement the same ``VisionClient`` seam (ADR 0004 authorized
the cloud export scope; ADR 0005 selected the default by benchmark): hosted
``qwen/qwen3-vl-32b-instruct`` via OpenRouter (default), the Gemini API
(first fallback), and the local Qwen3-VL model on peecee (Ollama,
OpenAI-compatible), the no-cloud rollback. Cloud backends receive only the
downscaled inference JPEG and prompt text. Each photo is analyzed in its own
call and the results are merged.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

DEFAULT_VISION_ENDPOINT: Final[str] = os.environ.get(
    "BINKEEPER_BIN_VISION_ENDPOINT", "http://peecee:11434/v1"
)
DEFAULT_VISION_MODEL: Final[str] = os.environ.get("BINKEEPER_BIN_VISION_MODEL", "qwen3-vl:8b")
DEFAULT_VISION_MAX_TOKENS: Final[int] = int(
    os.environ.get("BINKEEPER_BIN_VISION_MAX_TOKENS", "3072")
)
DEFAULT_VISION_RETRY_TOKENS: Final[int] = int(
    os.environ.get("BINKEEPER_BIN_VISION_RETRY_TOKENS", "4096")
)
DEFAULT_VISION_TIMEOUT_S: Final[float] = float(
    os.environ.get("BINKEEPER_BIN_VISION_TIMEOUT_S", "60")
)
DEFAULT_ITEM_CONFIDENCE_FLOOR: Final[float] = float(
    os.environ.get("BINKEEPER_BIN_VISION_CONFIDENCE_FLOOR", "0.35")
)
# Full-resolution phone photos (often 24 MP) can blow past the local model's
# context on peecee's 24 GiB card, causing empty output or a 400; downscaling to
# a bounded long edge for inference keeps OCR legible while staying well inside
# it. The ORIGINAL bytes are preserved by the caller as evidence.
DEFAULT_VISION_MAX_EDGE: Final[int] = int(os.environ.get("BINKEEPER_BIN_VISION_MAX_EDGE", "1280"))
DEFAULT_VISION_JPEG_QUALITY: Final[int] = int(
    os.environ.get("BINKEEPER_BIN_VISION_JPEG_QUALITY", "85")
)
DEFAULT_VISION_PROVIDER: Final[str] = os.environ.get("BINKEEPER_BIN_VISION_PROVIDER", "openrouter")
DEFAULT_OPENROUTER_ENDPOINT: Final[str] = os.environ.get(
    "BINKEEPER_BIN_VISION_OPENROUTER_ENDPOINT", "https://openrouter.ai/api/v1"
)
DEFAULT_OPENROUTER_MODEL: Final[str] = os.environ.get(
    "BINKEEPER_BIN_VISION_OPENROUTER_MODEL", "qwen/qwen3-vl-32b-instruct"
)
DEFAULT_GEMINI_ENDPOINT: Final[str] = os.environ.get(
    "BINKEEPER_BIN_VISION_GEMINI_ENDPOINT",
    "https://generativelanguage.googleapis.com/v1beta",
)
# Model names retire under callers (gemini-2.5-flash 404'd during the ADR 0004
# evaluation), so the served model is configuration, never code.
DEFAULT_GEMINI_MODEL: Final[str] = os.environ.get(
    "BINKEEPER_BIN_VISION_GEMINI_MODEL", "gemini-3.6-flash"
)
_JSON_OBJECT_RE: Final[re.Pattern[str]] = re.compile(r"\{.*\}", re.DOTALL)
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+", re.IGNORECASE)


class BinVisionError(RuntimeError):
    """Domain root for vision-worker failures (RFC 0012 family)."""


@dataclass(frozen=True)
class DetectedItem:
    """One item a photo shows, with the model's confidence. Provisional."""

    label: str
    traits: tuple[str, ...]
    confidence: float

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a detected item."""
        return {
            "label": self.label,
            "traits": list(self.traits),
            "confidence": round(self.confidence, 3),
        }


@dataclass(frozen=True)
class BinLabelProposal:
    """A provisional label proposal for a bin, merged across photos + notes."""

    theme: str
    accepts: tuple[str, ...]
    owner_phrase: str | None
    summary: str
    items: tuple[DetectedItem, ...]
    model_version: str
    photo_count: int

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a label proposal."""
        return {
            "theme": self.theme,
            "accepts": list(self.accepts),
            "owner_phrase": self.owner_phrase,
            "summary": self.summary,
            "items": [item.to_json() for item in self.items],
            "model_version": self.model_version,
            "photo_count": self.photo_count,
        }


class VisionClient(Protocol):
    """A vision-language client that returns the model's raw text for a prompt+image."""

    model: str

    def analyze(self, prompt: str, image: bytes) -> str:
        """Return the model's raw text answer for one image and prompt."""
        ...


class OllamaVisionClient:
    """OpenAI-compatible vision client (local Ollama on peecee by default).

    An ``api_key`` sends a bearer token, which also fits hosted
    OpenAI-compatible gateways (the benchmark uses this for OpenRouter).
    """

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_VISION_ENDPOINT,
        model: str = DEFAULT_VISION_MODEL,
        max_tokens: int = DEFAULT_VISION_MAX_TOKENS,
        timeout_s: float = DEFAULT_VISION_TIMEOUT_S,
        max_edge: int = DEFAULT_VISION_MAX_EDGE,
        jpeg_quality: int = DEFAULT_VISION_JPEG_QUALITY,
        api_key: str | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.max_edge = max_edge
        self.jpeg_quality = jpeg_quality
        self.api_key = api_key

    def analyze(self, prompt: str, image: bytes) -> str:
        prepared, mime = _prepare_image(image, max_edge=self.max_edge, quality=self.jpeg_quality)
        data_uri = f"data:{mime};base64," + base64.b64encode(prepared).decode("ascii")
        deadline = time.monotonic() + self.timeout_s
        output = self._request(
            prompt,
            data_uri,
            max_tokens=self.max_tokens,
            timeout_s=self.timeout_s,
        )
        try:
            _parse_vision_json(output)
        except BinVisionError:
            pass
        else:
            return output

        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return output
        retry_tokens = max(self.max_tokens, DEFAULT_VISION_RETRY_TOKENS)
        return self._request(
            prompt,
            data_uri,
            max_tokens=retry_tokens,
            timeout_s=remaining_s,
        )

    def _request(
        self,
        prompt: str,
        data_uri: str,
        *,
        max_tokens: int,
        timeout_s: float,
    ) -> str:
        """Make one bounded local-model request and return its best output channel."""
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
            # Send both Ollama's top-level hint and the chat-template form. The
            # current Qwen3-VL build may still expose reasoning, so the smaller
            # model and bounded output remain the actual latency controls.
            "think": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTPError is a URLError subclass and must be caught first; its body
            # carries the model's real complaint (e.g. a context-overflow 400).
            raise BinVisionError(
                f"vision model returned HTTP {exc.code}: {_http_error_detail(exc)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise BinVisionError(
                f"vision endpoint {self.endpoint} unreachable: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            # A read timeout raises the builtin TimeoutError, which is an OSError
            # but NOT a URLError, so it is not covered above -- urllib only wraps
            # connect-time failures. Left uncaught it escaped the advisory-vision
            # lane entirely and 500'd the photo-drop page (2026-07-29). The usual
            # cause is a cold model load on the vision host, not an outage.
            raise BinVisionError(
                f"vision model {self.model} at {self.endpoint} did not respond "
                f"within {timeout_s:g}s (it may still be loading; try again)"
            ) from exc
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BinVisionError("vision response missing message content") from exc
        if not isinstance(message, dict):
            raise BinVisionError("vision response missing message content")
        content = str(message.get("content") or "")
        reasoning = str(message.get("reasoning") or "")
        # Ollama may put a JSON answer in either channel, including while leaving
        # non-JSON boilerplate in content. Prefer whichever channel actually
        # satisfies the worker's JSON contract.
        for candidate in (content, reasoning):
            try:
                _parse_vision_json(candidate)
            except BinVisionError:
                continue
            return candidate
        return content or reasoning


class GeminiVisionClient:
    """Gemini generateContent vision client (ADR 0004 cloud backend).

    Sends only the downscaled inference JPEG and the lane's prompt text. The
    key is read from the service environment at request time so a missing key
    surfaces as a BinVisionError inside the advisory lane, which degrades
    instead of failing the owner surface.
    """

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_GEMINI_ENDPOINT,
        model: str = DEFAULT_GEMINI_MODEL,
        api_key: str | None = None,
        timeout_s: float = DEFAULT_VISION_TIMEOUT_S,
        max_edge: int = DEFAULT_VISION_MAX_EDGE,
        jpeg_quality: int = DEFAULT_VISION_JPEG_QUALITY,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_edge = max_edge
        self.jpeg_quality = jpeg_quality

    def _key(self) -> str:
        key = (
            self.api_key
            or os.environ.get("BINKEEPER_GEMINI_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
        )
        if not key or not key.strip():
            raise BinVisionError(
                "no Gemini API key configured; set BINKEEPER_GEMINI_API_KEY (or "
                "GEMINI_API_KEY), or set BINKEEPER_BIN_VISION_PROVIDER=local"
            )
        return key.strip()

    def analyze(self, prompt: str, image: bytes) -> str:
        prepared, mime = _prepare_image(image, max_edge=self.max_edge, quality=self.jpeg_quality)
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": mime,
                                "data": base64.b64encode(prepared).decode("ascii"),
                            }
                        },
                        {"text": prompt},
                    ]
                }
            ],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        request = urllib.request.Request(
            f"{self.endpoint}/models/{self.model}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self._key()},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise BinVisionError(
                f"vision model returned HTTP {exc.code}: {_http_error_detail(exc)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise BinVisionError(
                f"vision endpoint {self.endpoint} unreachable: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise BinVisionError(
                f"vision model {self.model} at {self.endpoint} did not respond "
                f"within {self.timeout_s:g}s"
            ) from exc
        try:
            parts = body["candidates"][0]["content"]["parts"]
            text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
        except (KeyError, IndexError, TypeError) as exc:
            raise BinVisionError("vision response missing message content") from exc
        if not text.strip():
            raise BinVisionError("vision response missing message content")
        return text


def default_vision_client(provider: str | None = None) -> VisionClient:
    """Build the configured vision backend (ADR 0005): openrouter, gemini, or local."""
    selected = (provider or DEFAULT_VISION_PROVIDER).strip().lower()
    if selected == "openrouter":
        key = os.environ.get("BINKEEPER_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        if not key or not key.strip():
            raise BinVisionError(
                "no OpenRouter API key configured; set BINKEEPER_OPENROUTER_API_KEY (or "
                "OPENROUTER_API_KEY), or set BINKEEPER_BIN_VISION_PROVIDER=gemini|local"
            )
        return OllamaVisionClient(
            endpoint=DEFAULT_OPENROUTER_ENDPOINT,
            model=DEFAULT_OPENROUTER_MODEL,
            api_key=key.strip(),
        )
    if selected == "gemini":
        return GeminiVisionClient()
    if selected == "local":
        return OllamaVisionClient()
    raise BinVisionError(
        f"unsupported vision provider {selected!r} (use 'openrouter', 'gemini', or 'local')"
    )


def propose_bin_label(
    client: VisionClient,
    images: Sequence[bytes],
    *,
    notes: str | None = None,
    confidence_floor: float = DEFAULT_ITEM_CONFIDENCE_FLOOR,
) -> BinLabelProposal:
    """Analyze each photo, merge the results, and propose a provisional label."""
    if not images:
        raise BinVisionError("at least one photo is required")
    prompt = _build_prompt(notes)
    merged: dict[str, DetectedItem] = {}
    themes: list[str] = []
    summaries: list[str] = []
    failures: list[str] = []
    parsed_any = False
    for image in images:
        # One bad photo (unreadable, model hiccup) must not sink the batch: skip
        # it and keep going, raising only if NOT ONE photo yielded usable output.
        try:
            parsed = _parse_vision_json(client.analyze(prompt, image))
        except BinVisionError as exc:
            failures.append(str(exc))
            continue
        parsed_any = True
        for item in _items_from(parsed):
            key = _normalize(item.label)
            if key and (key not in merged or item.confidence > merged[key].confidence):
                merged[key] = item
        theme = _clean(parsed.get("theme"))
        if theme:
            themes.append(theme)
        summary = _clean(parsed.get("summary"))
        if summary:
            summaries.append(summary)
    if not parsed_any:
        reason = "; ".join(dict.fromkeys(failures)) or "no usable vision output"
        raise BinVisionError(reason[:400])
    kept = tuple(
        sorted(
            (item for item in merged.values() if item.confidence >= confidence_floor),
            key=lambda item: (-item.confidence, item.label),
        )
    )
    return BinLabelProposal(
        theme=_pick_theme(themes, notes, kept),
        accepts=tuple(item.label for item in kept),
        owner_phrase=_clean(notes),
        summary=summaries[0] if summaries else "",
        items=kept,
        model_version=getattr(client, "model", DEFAULT_VISION_MODEL),
        photo_count=len(images),
    )


def _prepare_image(image: bytes, *, max_edge: int, quality: int) -> tuple[bytes, str]:
    """Downscale (EXIF-aware) to a bounded long edge and re-encode JPEG.

    Returns ``(bytes, mime)``. Reduction is for inference only -- the caller
    stores the original bytes as evidence. If the image can't be decoded (HEIC
    without a decoder, Pillow absent, corrupt data), the original bytes are
    returned unchanged for the endpoint to attempt.
    """
    try:
        import io

        from PIL import Image, ImageOps
    except Exception:  # Pillow is a serve-extra; degrade to sending as-is
        return image, _image_mime(image)
    try:
        with Image.open(io.BytesIO(image)) as opened:
            oriented = ImageOps.exif_transpose(opened) or opened
            rgb = oriented.convert("RGB")
            rgb.thumbnail((max_edge, max_edge))
            buffer = io.BytesIO()
            rgb.save(buffer, format="JPEG", quality=quality)
            return buffer.getvalue(), "image/jpeg"
    except Exception:  # undecodable/corrupt -> let the endpoint try the original
        return image, _image_mime(image)


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """Extract the model's error message from an HTTPError body (best-effort)."""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        return exc.reason or "no detail"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:200] or (exc.reason or "no detail")
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        if isinstance(error, str) and error.strip():
            return error[:200]
    return raw[:200] or (exc.reason or "no detail")


def read_bin_code(client: VisionClient, image: bytes) -> str | None:
    """OCR the bin label's printed code from a photo, or None if illegible.

    The fallback when the QR won't decode (blurry, angled, out of frame). Returns
    a normalized code like ``AGR-001`` or None; never raises.
    """
    prompt = (
        "Read the bin label code printed on the bin in this photo. It is two to four "
        "capital letters, a hyphen, then digits (for example AGR-001, OFE-014). Respond "
        'with ONLY JSON: {"bin_code": "AGR-001"} or {"bin_code": null} if none is legible.'
    )
    try:
        parsed = _parse_vision_json(client.analyze(prompt, image))
    except BinVisionError:
        return None
    raw = _clean(parsed.get("bin_code"))
    if not raw:
        return None
    match = re.search(r"[A-Z]{2,4}-\d{2,4}", raw.upper())
    return match.group(0) if match else None


def read_visible_bin_codes(client: VisionClient, image: bytes) -> list[str]:
    """OCR every bin-label code visible anywhere in a photo (foreground AND background).

    The peripheral-OCR harvest read (RFC 0088 T3b): a single shot of a shelf may show
    several labeled bins, so this returns ALL legible codes, deduped in first-seen order
    and normalized like ``AGR-001``. Advisory and best-effort -- it never raises and
    returns an empty list when the model is unreachable or nothing is legible. The
    harvester validates each code against the known-bin registry before trusting it, so
    an invented or misread code corroborates nothing.
    """
    prompt = (
        "List EVERY bin-label code you can read in this photo -- on bins in the "
        "foreground AND any labels visible in the background or off to the side. Each "
        "code is two to four capital letters, a hyphen, then digits (for example "
        "AGR-001, OFE-014). Respond with ONLY JSON: "
        '{"bin_codes": ["AGR-001", "OFE-014"]} -- an empty list {"bin_codes": []} if '
        "none is legible. Do not invent codes; list only what you can actually read."
    )
    try:
        parsed = _parse_vision_json(client.analyze(prompt, image))
    except BinVisionError:
        return []  # best-effort: an unreachable model or unreadable shot yields no codes
    codes: list[str] = []
    seen: set[str] = set()
    for raw in _as_list(parsed.get("bin_codes")):
        text = _clean(raw)
        if not text:
            continue
        match = re.search(r"[A-Z]{2,4}-\d{2,4}", text.upper())
        if match is None:
            continue
        code = match.group(0)
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _image_mime(image: bytes) -> str:
    """Sniff a common image mime from magic bytes (defaults to jpeg)."""
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    if image[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if image[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    return "image/jpeg"


def _build_prompt(notes: str | None) -> str:
    note_line = (
        f"The owner's notes about this bin: {notes.strip()}\n"
        if notes and notes.strip()
        else "The owner gave no notes.\n"
    )
    return (
        "You are cataloguing a physical storage bin from a photo of its contents.\n"
        f"{note_line}"
        "Identify the distinct items, tools, or parts you can see. Prefer specific "
        "names (for example 'hex keys', 'USB-C cables', 'caliper'). Then propose a "
        "short theme for the whole bin.\n"
        "Respond with ONLY a JSON object and nothing else, in this exact shape:\n"
        '{"items": [{"label": "short name", "traits": ["..."], "confidence": 0.0}], '
        '"theme": "2-5 word bin theme", "summary": "one sentence of what this bin holds"}\n'
        "Use a confidence in [0,1]; lower it when the photo is unclear."
    )


def _parse_vision_json(raw: str) -> dict[str, object]:
    match = _JSON_OBJECT_RE.search(raw)
    if match is None:
        raise BinVisionError("vision output contained no JSON object")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise BinVisionError("vision output was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise BinVisionError("vision output JSON was not an object")
    return {str(key): value for key, value in parsed.items()}


def _items_from(parsed: dict[str, object]) -> list[DetectedItem]:
    raw_items = parsed.get("items")
    if not isinstance(raw_items, list):
        return []
    items: list[DetectedItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        label = _clean(entry.get("label"))
        if not label:
            continue
        items.append(
            DetectedItem(
                label=label,
                traits=tuple(t for t in (_clean(x) for x in _as_list(entry.get("traits"))) if t),
                confidence=_confidence(entry.get("confidence")),
            )
        )
    return items


def _pick_theme(themes: Sequence[str], notes: str | None, items: Sequence[DetectedItem]) -> str:
    if themes:
        return max(set(themes), key=themes.count)
    note = _clean(notes)
    if note:
        return note[:60]
    if items:
        return items[0].label
    return "unlabeled bin"


def _normalize(text: str) -> str:
    return " ".join(m.group(0).lower() for m in _TOKEN_RE.finditer(text))


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _confidence(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        try:
            return max(0.0, min(1.0, float(value.strip())))
        except ValueError:
            return 0.0
    return 0.0
