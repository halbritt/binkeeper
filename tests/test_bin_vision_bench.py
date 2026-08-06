"""Tests for the vision-benchmark scoring core (synthetic data only)."""

from __future__ import annotations

from binkeeper.bin_vision_bench import (
    BenchPhoto,
    CallScore,
    ModelReport,
    parse_owner_items,
    render_summary,
    score_raw_output,
    significant_tokens,
)

_PHOTO = BenchPhoto(
    bin_code="TST-001",
    photo_sha256="0" * 64,
    owner_theme="Measuring tools",
    owner_items=("Mitutoyo calipers", "micrometer", "feeler gauges"),
)


def test_parse_owner_items_reads_the_photo_drop_format() -> None:
    text = "bin TST-001 @ test-site · Mitutoyo calipers, micrometer, feeler gauges"
    assert parse_owner_items(text) == ("Mitutoyo calipers", "micrometer", "feeler gauges")


def test_parse_owner_items_without_list_is_theme_only() -> None:
    assert parse_owner_items("bin TST-001 @ test-site") == ()


def test_significant_tokens_drop_stopwords_and_short_tokens() -> None:
    assert significant_tokens("a set of 12 new hex keys") == {"hex", "keys", "set"}


def test_score_matches_items_by_significant_tokens() -> None:
    raw = (
        '{"items": [{"label": "digital calipers", "traits": ["Mitutoyo"], "confidence": 0.9},'
        '{"label": "shop rag", "confidence": 0.8}],'
        '"theme": "precision measuring", "summary": "measuring gear"}'
    )

    score = score_raw_output(_PHOTO, raw, seconds=1.5, repeat=0)

    assert score.json_ok
    assert score.matched_items == ("Mitutoyo calipers",)
    assert score.missed_items == ("micrometer", "feeler gauges")
    assert score.unmatched_predictions == ("shop rag",)
    assert score.recall is not None and abs(score.recall - 1 / 3) < 1e-9
    assert score.theme_match  # "measuring" is shared with the owner theme


def test_score_without_json_counts_every_item_missed() -> None:
    score = score_raw_output(_PHOTO, "sorry, I cannot tell", seconds=0.5, repeat=0)

    assert not score.json_ok
    assert score.recall == 0.0
    assert score.missed_items == _PHOTO.owner_items


def test_theme_only_photo_has_no_recall() -> None:
    photo = BenchPhoto("TST-002", "1" * 64, "Empty bin", ())
    raw = '{"items": [], "theme": "empty bin", "summary": ""}'

    score = score_raw_output(photo, raw, seconds=1.0, repeat=0)

    assert score.recall is None
    assert score.theme_match


def test_report_aggregates_and_ranks_in_summary() -> None:
    good = ModelReport(
        model_id="good-model",
        calls=[
            score_raw_output(
                _PHOTO,
                '{"items": [{"label": "calipers"}, {"label": "micrometer"},'
                '{"label": "feeler gauge set"}], "theme": "measuring tools"}',
                seconds=2.0,
                repeat=0,
            )
        ],
    )
    bad = ModelReport(
        model_id="bad-model",
        calls=[
            CallScore(
                bin_code="TST-001",
                repeat=0,
                seconds=9.0,
                json_ok=False,
                error="endpoint unreachable",
                theme=None,
                predicted_labels=(),
                matched_items=(),
                missed_items=_PHOTO.owner_items,
                unmatched_predictions=(),
                theme_match=False,
            )
        ],
    )

    assert good.mean_recall == 1.0
    assert good.error_count == 0
    assert bad.mean_recall is None
    assert bad.error_count == 1

    summary = render_summary([bad, good])
    lines = summary.splitlines()
    assert "good-model" in lines[2]  # scored model ranks above the errored one
    assert "bad-model" in lines[3]
