from atlas_shadow.ingest_daemon.legacy_receipt_corrections import (
    load_legacy_receipt_corrections,
)


def test_load_legacy_receipt_corrections_matches_short_source_commit(tmp_path):
    path = tmp_path / "corrections.yaml"
    path.write_text(
        """
schema_version: 1
corrections:
  - packet_id: packet-a
    question_id: q7
    source_commit: abc1234
    action: skip_legacy_receipt_defect
    clean_excluded_reason: legacy_non_indexed_fixture
    note: JSONL fixtures are not indexed.
""",
        encoding="utf-8",
    )

    corrections = load_legacy_receipt_corrections(path)
    match = corrections.match(
        packet_id="packet-a",
        question_id="q7",
        source_commit="abc123456789",
    )

    assert match is not None
    assert match.action == "skip_legacy_receipt_defect"
    assert match.clean_excluded_reason == "legacy_non_indexed_fixture"
    assert "JSONL" in match.note


def test_load_legacy_receipt_corrections_ignores_non_matching_commit(tmp_path):
    path = tmp_path / "corrections.yaml"
    path.write_text(
        """
schema_version: 1
corrections:
  - packet_id: packet-a
    question_id: q7
    source_commit: abc1234
    action: skip_legacy_receipt_defect
    clean_excluded_reason: legacy_receipt_anchor_mismatch
    note: wrong anchor
""",
        encoding="utf-8",
    )

    corrections = load_legacy_receipt_corrections(path)

    assert corrections.match(
        packet_id="packet-a",
        question_id="q7",
        source_commit="def9876",
    ) is None
