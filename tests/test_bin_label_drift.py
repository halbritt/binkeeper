"""Tests for the ADR 0006 nightly label-drift workflow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from binkeeper.bin_label_drift import (
    LabelDriftError,
    LabelDriftEvidence,
    diff_label_proposal,
    dismiss_label_drift_proposal,
    fold_label_drift_queue,
    harvest_label_drift,
    load_label_drift_queue,
)
from binkeeper.bin_passport import BinPassport
from binkeeper.bin_register import register_bin
from binkeeper.bin_vision import BinLabelProposal, BinVisionError, DetectedItem
from binkeeper.cli import build_parser, execute, label_drift_exit_code


def _passport(*, theme: str = "Hand tools", contents: str = "hex keys") -> BinPassport:
    return BinPassport(
        bin_code="TST-001",
        theme=theme,
        home_site="test-site",
        current_site="test-site",
        owner_phrase=None,
        accepts=(),
        excludes=(),
        examples=(),
        sibling_contents=(contents,),
        physical_constraints=(),
        volume_profile=None,
        capacity_state="unknown",
        location_confidence=1.0,
        passport_confidence=1.0,
        provenance_refs=(),
    )


def _proposal(*, theme: str, items: tuple[tuple[str, float], ...]) -> BinLabelProposal:
    detected = tuple(
        DetectedItem(label=label, traits=(), confidence=confidence) for label, confidence in items
    )
    return BinLabelProposal(
        theme=theme,
        accepts=tuple(item.label for item in detected),
        owner_phrase=None,
        summary="synthetic proposal",
        items=detected,
        model_version="cloud+local",
        photo_count=1,
    )


def test_materiality_requires_a_theme_change_or_two_new_items() -> None:
    one_new = diff_label_proposal(
        _passport(),
        _proposal(theme="hand tools", items=(("hex keys", 0.9), ("torque wrench", 0.8))),
    )
    two_new = diff_label_proposal(
        _passport(),
        _proposal(theme="hand tools", items=(("torque wrench", 0.8), ("sockets", 0.7))),
    )
    changed_theme = diff_label_proposal(
        _passport(),
        _proposal(theme="mechanic tools", items=(("hex keys", 0.9),)),
    )

    assert not one_new.material
    assert one_new.new_item_labels == ("torque wrench",)
    assert two_new.material
    assert changed_theme.material


class _EnsembleClient:
    model = "anthropic/claude-opus-5+qwen3-vl:8b"
    model_versions = ("anthropic/claude-opus-5", "qwen3-vl:8b")

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, prompt: str, image: bytes) -> str:
        self.calls += 1
        return (
            '{"items": [{"label": "torque wrench", "confidence": 0.9}, '
            '{"label": "socket set", "confidence": 0.8}], '
            '"theme": "mechanic tools", "summary": "synthetic"}'
        )


def test_writer_records_changed_inputs_once_and_preserves_the_decision_evidence(
    conn: psycopg.Connection,
) -> None:
    photo_sha = "a" * 64
    observed = datetime(2026, 8, 11, 3, 30, tzinfo=UTC)
    register_bin(
        conn,
        bin_code="TST-001",
        site="test-site",
        photo_sha256=photo_sha,
        contents_text="hex keys",
        theme="hand tools",
        observed_at=observed,
    )
    client = _EnsembleClient()

    first = harvest_label_drift(
        conn,
        client=client,
        open_photo=lambda _conn, sha: b"synthetic-photo" if sha == photo_sha else None,
        now=observed,
    )
    replay = harvest_label_drift(
        conn,
        client=client,
        open_photo=lambda _conn, sha: b"synthetic-photo" if sha == photo_sha else None,
        now=observed,
    )

    assert first.to_json() == {
        "bins_scanned": 1,
        "bins_analyzed": 1,
        "proposals_recorded": 1,
        "material_proposals": 1,
        "skipped_unchanged": 0,
        "skipped_no_photos": 0,
        "skipped_unreadable_photos": 0,
        "model_errors": 0,
    }
    assert replay.skipped_unchanged == 1
    assert client.calls == 1
    payload = conn.execute(
        """
        SELECT payload
        FROM capture_evidence
        WHERE payload->'metadata'->>'kind' = 'label_drift_proposal'
        """
    ).fetchone()[0]
    metadata = payload["metadata"]
    assert metadata["photo_hashes"] == [photo_sha]
    assert metadata["model_versions"] == list(client.model_versions)
    assert metadata["passport_snapshot"]["theme"] == "hand tools"
    assert metadata["diff"]["material"] is True
    assert metadata["proposal"]["theme"] == "mechanic tools"
    assert metadata["idempotency_key"].startswith("label-drift:TST-001:")


def test_writer_preserves_other_bins_when_one_strict_ensemble_call_fails(
    conn: psycopg.Connection,
) -> None:
    observed = datetime(2026, 8, 11, 3, 30, tzinfo=UTC)
    for code, digest in (("TST-001", "a" * 64), ("TST-002", "b" * 64)):
        register_bin(
            conn,
            bin_code=code,
            site="test-site",
            photo_sha256=digest,
            contents_text="hex keys",
            theme="hand tools",
            observed_at=observed,
        )

    class _PartlyFailingClient(_EnsembleClient):
        def analyze(self, prompt: str, image: bytes) -> str:
            if image == b"bad-photo":
                raise BinVisionError("synthetic ensemble failure")
            return super().analyze(prompt, image)

    summary = harvest_label_drift(
        conn,
        client=_PartlyFailingClient(),
        open_photo=lambda _conn, sha: b"bad-photo" if sha == "a" * 64 else b"good-photo",
        now=observed + timedelta(hours=1),
    )

    assert summary.bins_scanned == 2
    assert summary.model_errors == 1
    assert summary.proposals_recorded == 1
    assert label_drift_exit_code(summary.to_json()) == 5
    recorded_bins = conn.execute(
        """
        SELECT raw_payload->'metadata'->>'bin_code'
        FROM captures
        WHERE raw_payload->'metadata'->>'kind' = 'label_drift_proposal'
        """
    ).fetchall()
    assert recorded_bins == [("TST-002",)]


def test_cli_refuses_the_nightly_writer_when_authority_is_closed(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BINKEEPER_WRITES_ENABLED", raising=False)
    args = build_parser().parse_args(["bin-label-drift-harvest"])

    with pytest.raises(RuntimeError, match="writer is frozen"):
        execute(args, conn)


def _proposal_evidence(
    external_id: str,
    observed_at: datetime,
    *,
    theme: str,
    new_items: tuple[str, ...],
    material: bool = True,
    photo_hashes: tuple[str, ...] = ("a" * 64,),
) -> LabelDriftEvidence:
    return LabelDriftEvidence(
        external_id=external_id,
        bin_code="TST-001",
        kind="label_drift_proposal",
        observed_at=observed_at,
        metadata={
            "proposal": {"theme": theme, "accepts": list(new_items)},
            "passport_snapshot": {"theme": "hand tools", "contents": "hex keys"},
            "diff": {
                "theme": {"before": "hand tools", "after": theme, "changed": True},
                "new_items": [
                    {"label": label, "confidence": 0.8, "traits": []} for label in new_items
                ],
                "material": material,
            },
            "photo_hashes": list(photo_hashes),
            "model_versions": ["anthropic/claude-opus-5", "qwen3-vl:8b"],
        },
    )


def test_queue_uses_only_the_newest_proposal_for_a_bin() -> None:
    older = _proposal_evidence(
        "proposal-old",
        datetime(2026, 8, 10, 3, 30, tzinfo=UTC),
        theme="mechanic tools",
        new_items=("torque wrench", "socket set"),
    )
    newer = _proposal_evidence(
        "proposal-new",
        datetime(2026, 8, 11, 3, 30, tzinfo=UTC),
        theme="precision tools",
        new_items=("calipers", "micrometer"),
    )

    queue = fold_label_drift_queue([older, newer], now=datetime(2026, 8, 11, 4, tzinfo=UTC))

    assert len(queue) == 1
    assert queue[0].proposal_external_id == "proposal-new"
    assert queue[0].proposed_theme == "precision tools"
    assert queue[0].new_item_labels == ("calipers", "micrometer")


def test_queue_excludes_a_recorded_non_material_proposal() -> None:
    proposal = _proposal_evidence(
        "proposal-non-material",
        datetime(2026, 8, 11, 3, 30, tzinfo=UTC),
        theme="hand tools",
        new_items=("torque wrench",),
        material=False,
    )

    assert fold_label_drift_queue([proposal]) == []


def test_profile_snapshot_after_a_proposal_clears_the_queue_entry() -> None:
    proposed_at = datetime(2026, 8, 11, 3, 30, tzinfo=UTC)
    proposal = _proposal_evidence(
        "proposal-accepted",
        proposed_at,
        theme="mechanic tools",
        new_items=("torque wrench", "socket set"),
    )
    profile = LabelDriftEvidence(
        external_id="profile-after-proposal",
        bin_code="TST-001",
        kind="bin_capture",
        observed_at=datetime(2026, 8, 11, 4, tzinfo=UTC),
        metadata={"profile_mode": "snapshot"},
    )

    assert fold_label_drift_queue([proposal, profile], now=proposed_at) == []


def test_dismissed_suggestions_return_after_the_ninety_day_horizon() -> None:
    proposed_at = datetime(2026, 8, 11, 3, 30, tzinfo=UTC)
    proposal = _proposal_evidence(
        "proposal-dismissed",
        proposed_at,
        theme="mechanic tools",
        new_items=("torque wrench", "socket set"),
    )
    dismissal = LabelDriftEvidence(
        external_id="dismissal",
        bin_code="TST-001",
        kind="label_drift_dismissal",
        observed_at=proposed_at + timedelta(hours=1),
        metadata={
            "proposal_external_id": proposal.external_id,
            "dismissed_theme": "mechanic tools",
            "dismissed_items": ["torque wrench", "socket set"],
            "photo_hashes": ["a" * 64],
        },
    )

    assert (
        fold_label_drift_queue(
            [proposal, dismissal], now=dismissal.observed_at + timedelta(days=89)
        )
        == []
    )
    assert (
        len(
            fold_label_drift_queue(
                [proposal, dismissal], now=dismissal.observed_at + timedelta(days=91)
            )
        )
        == 1
    )


def test_new_photo_evidence_bypasses_a_matching_dismissal() -> None:
    dismissed_at = datetime(2026, 8, 11, 4, tzinfo=UTC)
    proposal = _proposal_evidence(
        "proposal-new-photo",
        dismissed_at + timedelta(days=1),
        theme="mechanic tools",
        new_items=("torque wrench", "socket set"),
        photo_hashes=("b" * 64,),
    )
    dismissal = LabelDriftEvidence(
        external_id="dismissal-old-photo",
        bin_code="TST-001",
        kind="label_drift_dismissal",
        observed_at=dismissed_at,
        metadata={
            "dismissed_theme": "mechanic tools",
            "dismissed_items": ["torque wrench", "socket set"],
            "photo_hashes": ["a" * 64],
        },
    )

    queue = fold_label_drift_queue(
        [dismissal, proposal], now=proposal.observed_at + timedelta(hours=1)
    )

    assert [entry.proposal_external_id for entry in queue] == ["proposal-new-photo"]


def test_queue_loads_native_proposals_and_owner_profile_snapshots(
    conn: psycopg.Connection,
) -> None:
    observed = datetime(2026, 8, 11, 3, 30, tzinfo=UTC)
    register_bin(
        conn,
        bin_code="TST-001",
        site="test-site",
        photo_sha256="a" * 64,
        contents_text="hex keys",
        theme="hand tools",
        observed_at=observed,
    )
    harvest_label_drift(
        conn,
        client=_EnsembleClient(),
        open_photo=lambda _conn, _sha: b"synthetic-photo",
        now=observed + timedelta(hours=1),
    )

    queue = load_label_drift_queue(conn, now=observed + timedelta(hours=2))

    assert len(queue) == 1
    assert queue[0].bin_code == "TST-001"
    assert queue[0].new_item_labels == ("torque wrench", "socket set")


def test_dismissal_is_append_only_idempotent_and_clears_the_current_entry(
    conn: psycopg.Connection,
) -> None:
    observed = datetime(2026, 8, 11, 3, 30, tzinfo=UTC)
    register_bin(
        conn,
        bin_code="TST-001",
        site="test-site",
        photo_sha256="a" * 64,
        contents_text="hex keys",
        theme="hand tools",
        observed_at=observed,
    )
    harvest_label_drift(
        conn,
        client=_EnsembleClient(),
        open_photo=lambda _conn, _sha: b"synthetic-photo",
        now=observed + timedelta(hours=1),
    )
    proposal = load_label_drift_queue(conn, now=observed + timedelta(hours=2))[0]
    action_id = "2f1eb067-e4fa-4451-b833-f8e8e0fe695d"

    first = dismiss_label_drift_proposal(
        conn,
        bin_code="TST-001",
        proposal_external_id=proposal.proposal_external_id,
        action_id=action_id,
        observed_at=observed + timedelta(hours=2),
    )
    replay = dismiss_label_drift_proposal(
        conn,
        bin_code="TST-001",
        proposal_external_id=proposal.proposal_external_id,
        action_id=action_id,
        observed_at=observed + timedelta(hours=3),
    )

    assert not first.already_existed
    assert replay.already_existed
    assert load_label_drift_queue(conn, now=observed + timedelta(hours=3)) == []
    metadata = conn.execute(
        """
        SELECT raw_payload->'metadata'
        FROM captures
        WHERE raw_payload->'metadata'->>'kind' = 'label_drift_dismissal'
        """
    ).fetchone()[0]
    assert metadata["proposal_external_id"] == proposal.proposal_external_id
    assert metadata["photo_hashes"] == ["a" * 64]


def test_dismissal_refuses_a_stale_or_mismatched_proposal(
    conn: psycopg.Connection,
) -> None:
    with pytest.raises(LabelDriftError, match="not the current pending proposal"):
        dismiss_label_drift_proposal(
            conn,
            bin_code="TST-001",
            proposal_external_id="proposal-does-not-exist",
            action_id="2f1eb067-e4fa-4451-b833-f8e8e0fe695d",
            observed_at=datetime(2026, 8, 11, 4, tzinfo=UTC),
        )
