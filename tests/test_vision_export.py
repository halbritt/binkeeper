"""The advisory export boundary must never fall back to original bytes."""

from __future__ import annotations

import base64
import builtins
import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from PIL import Image

from binkeeper.bin_vision import (
    GeminiVisionClient,
    ImagePreparationError,
    OpenRouterVisionClient,
)


@pytest.fixture
def transport(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {
            "choices": [{"message": {"content": '{"items": []}'}}],
            "candidates": [{"content": {"parts": [{"text": '{"items": []}'}]}}],
        }
    ).encode()
    transport = MagicMock(return_value=response)
    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", transport)
    return transport


@pytest.mark.parametrize("client_type", [OpenRouterVisionClient, GeminiVisionClient])
@pytest.mark.parametrize("original", [b"invalid image", b"\xff\xd8\xff", b"\0\0\0\x18ftypheic"])
def test_unreadable_image_is_refused_before_transport(client_type, original, transport) -> None:
    with pytest.raises(ImagePreparationError, match="prepare image"):
        client_type(api_key="synthetic-key").analyze("Describe contents", original)

    transport.assert_not_called()


@pytest.mark.parametrize("client_type", [OpenRouterVisionClient, GeminiVisionClient])
def test_export_is_bounded_oriented_jpeg_without_original_metadata(client_type, transport):
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010E] = "private synthetic EXIF"
    original = BytesIO()
    Image.new("RGB", (3000, 1600), "navy").save(
        original, format="JPEG", exif=exif, comment=b"private synthetic comment"
    )
    client_type(api_key="synthetic-key", max_edge=1280).analyze(
        "Describe contents", original.getvalue()
    )

    assert transport.call_count == 1
    payload = json.loads(transport.call_args.args[0].data)
    if client_type is GeminiVisionClient:
        image = payload["contents"][0]["parts"][0]["inline_data"]
        assert image["mime_type"] == "image/jpeg"
        encoded = image["data"]
    else:
        data_uri = payload["messages"][0]["content"][1]["image_url"]["url"]
        assert data_uri.startswith("data:image/jpeg;base64,")
        encoded = data_uri.split(",", 1)[1]
    exported = base64.b64decode(encoded)
    assert exported != original.getvalue()
    assert b"private synthetic" not in exported
    with Image.open(BytesIO(exported)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.height == 1280 and decoded.width < decoded.height
        assert not decoded.getexif()
        assert "comment" not in decoded.info


@pytest.mark.parametrize("client_type", [OpenRouterVisionClient, GeminiVisionClient])
@pytest.mark.parametrize("failure", ["import", "decode", "orient", "convert", "resize", "encode"])
def test_preparation_failure_never_falls_back_to_original(
    client_type, failure, transport, monkeypatch
):
    original = BytesIO()
    Image.new("RGB", (32, 16), "navy").save(original, format="JPEG")
    error = ImportError("synthetic import failure") if failure == "import" else OSError(failure)

    def fail(*args, **kwargs):
        raise error

    if failure == "import":
        original_import = builtins.__import__

        def fail_pillow(name, *args, **kwargs):
            if name == "PIL":
                raise error
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail_pillow)
    else:
        monkeypatch.setattr(
            {
                "decode": "PIL.Image.open",
                "orient": "PIL.ImageOps.exif_transpose",
                "convert": "PIL.Image.Image.convert",
                "resize": "PIL.Image.Image.thumbnail",
                "encode": "PIL.Image.Image.save",
            }[failure],
            fail,
        )

    with pytest.raises(ImagePreparationError) as raised:
        client_type(api_key="synthetic-key").analyze("Describe contents", original.getvalue())

    assert raised.value.__cause__ is error
    transport.assert_not_called()
