"""Synthetic archive contracts; no provider dataset is redistributed in tests."""
import csv
import io
import json
import tempfile
from pathlib import Path

import pytest

import f4_sgodds_archive as archive


NOW = "2026-10-12T11:00:00Z"


def row(**changes):
    result = dict(zip(archive.REQUIRED_FIELDS,
        ["0390", "Home vs Away", "2026-10-13 03:00:00", "English Premier", "1",
         "HT:0-0, FT:2-1", "-0.25", "1.97", "0.25", "1.85", "2.00", "3.30", "3.00"]))
    result.update(changes)
    return result


def content(rows, fields=archive.REQUIRED_FIELDS):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def test_prices_line_score_and_live_market_flag_are_not_t15():
    result = archive.parse_archive(content([row()]), retrieved_at=NOW)
    record = result["records"][0]
    assert record["odds"] == [1.97, 1.85]
    assert record["final_score"] == [2, 1]
    assert record["home_handicap"] == -.25
    assert record["has_live_market"] is True
    assert record["quote_timestamp"] is None
    assert record["kickoff_timezone"] is None
    assert record["training_eligible"] is False
    assert record["prospective_eligible"] is False


@pytest.mark.parametrize("changes,reason", [
    ({"Ah_02_Hcap": "0.50"}, "handicap sides do not oppose"),
    ({"Ah_01_Hcap": "-.3"}, "invalid handicap"),
    ({"Ah_01": "NaN"}, "invalid price"),
    ({"Ah_01": "1"}, "invalid price"),
    ({"Ft1X2_03": ""}, "invalid price"),
    ({"League": "Other"}, "wrong league"),
    ({"Start Time": "tomorrow"}, "invalid provider-local kickoff"),
    ({"Result": "HT:0-0"}, "unrecognised final result"),
    ({"Match": "Home vs Home"}, "invalid team pair"),
    ({"Live Bet": "maybe"}, "invalid live-market flag"),
])
def test_incomplete_or_contradictory_rows_are_excluded(changes, reason):
    result = archive.parse_archive(content([row(**changes)]), retrieved_at=NOW)
    assert result["records"] == []
    assert result["summary"]["rejected_rows"] == {reason: 1}


def test_pending_is_not_a_completed_result_and_season_is_local():
    result = archive.parse_archive(content([row(Result="pending_or_postpone")]), retrieved_at=NOW)
    assert result["records"][0]["final_score"] is None
    assert result["summary"]["completed_candidate_matches"] == 0
    assert result["summary"]["pending_or_postponed_candidates"] == 1


def test_duplicate_rows_do_not_inflate_fixtures_and_match_numbers_are_not_ids():
    rows = [row(), row(), row(**{"Start Time": "2026-10-20 03:00:00"})]
    result = archive.parse_archive(content(rows), retrieved_at=NOW)
    assert result["summary"]["exact_duplicate_rows"] == 1
    assert result["summary"]["reused_match_numbers"] == 1
    assert len(result["records"]) == 2


def test_conflicting_quote_or_result_quarantines_all_copies():
    result = archive.parse_archive(content([row(), row(Ah_01="1.90")]), retrieved_at=NOW)
    assert result["records"] == []
    assert result["summary"]["conflicting_fixtures"] == 1
    assert result["summary"]["rejected_rows"] == {"conflicting fixture records": 2}


def test_public_summary_contains_no_match_names_prices_or_scores():
    summary = archive.parse_archive(content([row()]), retrieved_at=NOW)["summary"]
    text = json.dumps(summary)
    assert "Home vs Away" not in text
    assert "1.97" not in text
    assert "final_score" not in text
    assert "content_sha256" in text
    assert summary["required_field_nonempty_counts"]["Ah_01"] == 1
    assert summary["completed_matches_by_season"] == {"2026/2027": 1}


def test_missing_or_duplicate_headers_and_naive_retrieval_time_fail():
    with pytest.raises(ValueError, match="headers"):
        archive.parse_archive(b"ID,Match\n1,Home\n", retrieved_at=NOW)
    with pytest.raises(ValueError, match="timestamp"):
        archive.parse_archive(content([row()]), retrieved_at="2026-10-12T11:00:00")


def test_discovery_accepts_only_one_exact_official_csv():
    expected = "https://sgodds.com/downloads/sgodds-123-english-premier.csv"
    assert archive.discover_csv('<a href="/downloads/sgodds-123-english-premier.csv">Download</a>') == expected
    for page in ('<a href="https://other.invalid/downloads/sgodds-123-english-premier.csv">x</a>',
                 '<a href="/downloads/sgodds-123-english-premier.csv?q=x">x</a>',
                 '<a href="/downloads/sgodds-123-english-premier.csv">x</a>'
                 '<a href="/downloads/sgodds-124-english-premier.csv">x</a>'):
        with pytest.raises(ValueError, match="exactly one"):
            archive.discover_csv(page)


def test_private_output_rejects_git_directory_or_descendant():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / ".git").mkdir()
        with pytest.raises(ValueError, match="outside"):
            archive._private_directory(root / "private")


def test_archive_limits_are_enforced(monkeypatch):
    monkeypatch.setattr(archive, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="bounded"):
        archive.parse_archive(content([row()]), retrieved_at=NOW)
    monkeypatch.setattr(archive, "MAX_BYTES", 5_000_000)
    monkeypatch.setattr(archive, "MAX_ROWS", 1)
    with pytest.raises(ValueError, match="row count"):
        archive.parse_archive(content([row(), row()]), retrieved_at=NOW)
