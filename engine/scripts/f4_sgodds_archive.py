"""Bounded, private-only exploration of sgodds' provider-claimed opening archive.

This is not a T-15 collector or a training-data loader.  Quote timestamps are
absent, and the provider warns that historical AH line changes can leave a
single inconsistent record.  Mechanically coherent rows remain quarantined
from the prospective experiment.  Never publish the original CSV or row file.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse


SOURCE_PAGE = "https://sgodds.com/football/data"
TERMS_URL = "https://sgodds.com/terms-conditions"
HELP_URL = "https://sgodds.com/help"
PROTOCOL = "f4-sgodds-opening-exploration-v1"
REQUIRED_FIELDS = ("ID", "Match", "Start Time", "League", "Live Bet", "Result",
                   "Ah_01_Hcap", "Ah_01", "Ah_02_Hcap", "Ah_02",
                   "Ft1X2_01", "Ft1X2_02", "Ft1X2_03")
MAX_BYTES = 5_000_000
MAX_ROWS = 10_000
KNOWN_WARNINGS = [
    "Provider labels the export opening odds; exact quote timestamps are absent.",
    "Kickoff timezone is not declared in the CSV; provider-local time is retained.",
    "Provider warns that historical AH/OU line changes may leave inconsistent single records.",
    "All rights reserved; no open redistribution licence was found. Keep raw and row data private.",
    "Live Bet marks availability of live betting, not the observation time of this opening quote.",
    "These records are not prospective T-15 observations and do not establish model advantage.",
]


def _retrieved_at(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timezone-aware retrieval timestamp required") from None
    if result.utcoffset() is None:
        raise ValueError("timezone-aware retrieval timestamp required")
    return result.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value, *, price=False):
    if isinstance(value, bool):
        raise ValueError("invalid price" if price else "invalid handicap")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("invalid price" if price else "invalid handicap") from None
    if not math.isfinite(result) or (price and result <= 1):
        raise ValueError("invalid price" if price else "invalid handicap")
    if not price and (abs(result) > 20 or not math.isclose(
            result * 4, round(result * 4), abs_tol=1e-9, rel_tol=0)):
        raise ValueError("invalid handicap")
    return result


def _name(value):
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _normalise_row(raw, retrieved_at):
    if raw.get("League") != "English Premier":
        raise ValueError("wrong league")
    names = str(raw.get("Match", "")).split(" vs ")
    if len(names) != 2:
        raise ValueError("invalid team pair")
    home, away = (_name(x) for x in names)
    if not home or not away or home.casefold() == away.casefold():
        raise ValueError("invalid team pair")
    try:
        kickoff = datetime.strptime(str(raw.get("Start Time", "")), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ValueError("invalid provider-local kickoff") from None
    if raw.get("Live Bet") not in {"0", "1"}:
        raise ValueError("invalid live-market flag")
    result_text = raw.get("Result", "")
    score_match = re.fullmatch(r"(?:HT:\d+-\d+,\s*)?FT:(\d+)-(\d+)", result_text)
    if result_text == "pending_or_postpone":
        final_score = None
    elif score_match:
        final_score = [int(score_match[1]), int(score_match[2])]
        if any(x > 50 for x in final_score):
            raise ValueError("invalid final score")
    else:
        raise ValueError("unrecognised final result")
    home_line, away_line = (_number(raw.get(field)) for field in ("Ah_01_Hcap", "Ah_02_Hcap"))
    if not math.isclose(home_line, -away_line, abs_tol=1e-9, rel_tol=0):
        raise ValueError("handicap sides do not oppose")
    odds = [_number(raw.get(field), price=True) for field in ("Ah_01", "Ah_02")]
    had = [_number(raw.get(field), price=True) for field in ("Ft1X2_01", "Ft1X2_02", "Ft1X2_03")]
    local_time = kickoff.strftime("%Y-%m-%d %H:%M:%S")
    identity = f"{home.casefold()}|{away.casefold()}|{local_time}"
    match_id = "sgodds-opening:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    season_start = kickoff.year if kickoff.month >= 7 else kickoff.year - 1
    return {
        "archive_match_id": match_id, "provider_match_number": str(raw.get("ID", "")),
        "home": home, "away": away, "provider_local_kickoff": local_time,
        "kickoff_timezone": None, "season": f"{season_start}/{season_start + 1}",
        "bookmaker": "singapore_pools", "bookmaker_attribution": "sgodds_help_provider_claim",
        "home_handicap": home_line, "away_handicap": away_line, "odds": odds,
        "same_provider_1x2_odds": had, "final_score": final_score,
        "quote_period": "opening_provider_claim", "quote_timestamp": None,
        "retrieved_at": retrieved_at, "has_live_market": raw["Live Bet"] == "1",
        "training_eligible": False, "prospective_eligible": False,
        "exclusion_reasons": ["quote_timestamp_unknown", "historical_contract_alignment_unverified"],
    }


def parse_archive(content, *, retrieved_at):
    """Return private candidate rows plus a publishable aggregate audit.

    Repeated match numbers are not fixture IDs.  Exact duplicates collapse;
    conflicting entries for a fixture quarantine all copies.  Mechanical
    validation cannot establish that an opening price refers to its stated line.
    """
    if not isinstance(content, bytes) or len(content) > MAX_BYTES:
        raise ValueError("CSV exceeds bounded download size or is not bytes")
    retrieved_at = _retrieved_at(retrieved_at)
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        fields = reader.fieldnames or []
    except (UnicodeError, csv.Error):
        raise ValueError("invalid UTF-8 CSV") from None
    if len(fields) != len(set(fields)) or any(field not in fields for field in REQUIRED_FIELDS):
        raise ValueError("missing or duplicated required CSV headers")
    reject_counts, field_coverage = Counter(), Counter()
    candidates, number_to_matches = defaultdict(list), defaultdict(set)
    total = 0
    for raw in reader:
        total += 1
        if total > MAX_ROWS:
            raise ValueError("CSV exceeds bounded row count")
        for field in REQUIRED_FIELDS:
            if raw.get(field) not in {None, ""}:
                field_coverage[field] += 1
        if None in raw or any(value is None for value in raw.values()):
            reject_counts["malformed CSV row"] += 1
            continue
        try:
            candidate = _normalise_row(raw, retrieved_at)
        except ValueError as exc:
            reject_counts[str(exc)] += 1
            continue
        candidates[candidate["archive_match_id"]].append(candidate)
        number_to_matches[candidate["provider_match_number"]].add(candidate["archive_match_id"])
    records, exact_duplicates, conflicts = [], 0, 0
    for group in candidates.values():
        keys = {json.dumps({k: v for k, v in row.items() if k != "provider_match_number"}, sort_keys=True)
                for row in group}
        if len(keys) != 1:
            conflicts += 1
            reject_counts["conflicting fixture records"] += len(group)
            continue
        records.append(group[0])
        exact_duplicates += len(group) - 1
    records.sort(key=lambda row: (row["provider_local_kickoff"], row["archive_match_id"]))
    completed = [row for row in records if row["final_score"] is not None]
    local_dates = [row["provider_local_kickoff"][:10] for row in records]
    summary = {
        "schema_version": PROTOCOL, "retrieved_at": retrieved_at,
        "source_page": SOURCE_PAGE, "terms_url": TERMS_URL, "help_url": HELP_URL,
        "content_sha256": hashlib.sha256(content).hexdigest(), "content_bytes": len(content),
        "provider": "sgodds", "bookmaker_provider_claim": "singapore_pools",
        "quote_period": "opening_provider_claim", "quote_timestamps_available": False,
        "kickoff_timezone_certified": False, "training_eligible": False,
        "prospective_eligible": False, "raw_and_row_data_publication_allowed": False,
        "licence_status": "no_open_redistribution_licence_found_private_exploration_only",
        "input_rows": total, "mechanically_coherent_unique_fixtures": len(records),
        "completed_candidate_matches": len(completed), "pending_or_postponed_candidates": len(records) - len(completed),
        "exact_duplicate_rows": exact_duplicates, "conflicting_fixtures": conflicts,
        "reused_match_numbers": sum(len(ids) > 1 for ids in number_to_matches.values()),
        "rejected_rows": dict(sorted(reject_counts.items())),
        "required_field_nonempty_counts": dict(sorted(field_coverage.items())),
        "provider_local_date_range": [min(local_dates), max(local_dates)] if local_dates else None,
        "completed_matches_by_season": dict(sorted(Counter(row["season"] for row in completed).items())),
        "completed_matches_by_handicap_size": dict(sorted(Counter(str(abs(row["home_handicap"])) for row in completed).items())),
        "known_limitations": KNOWN_WARNINGS,
    }
    return {"records": records, "summary": summary}


class _DownloadLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.urls.extend(value for key, value in attrs if key == "href" and value)


def discover_csv(page):
    parser = _DownloadLinks()
    parser.feed(page)
    urls = set()
    for value in parser.urls:
        url = urljoin(SOURCE_PAGE, value)
        parsed = urlparse(url)
        if (parsed.scheme == "https" and parsed.netloc == "sgodds.com"
                and re.fullmatch(r"/downloads/sgodds-\d+-english-premier\.csv", parsed.path)
                and not parsed.query and not parsed.fragment):
            urls.add(url)
    if len(urls) != 1:
        raise ValueError("exactly one official English Premier CSV link required")
    return urls.pop()


def _download(url, *, session):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "sgodds.com":
        raise ValueError("only official sgodds HTTPS URLs are permitted")
    with session.get(url, timeout=(10, 30), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if response.is_redirect:
            raise ValueError("unexpected archive redirect")
        chunks, size = [], 0
        for chunk in response.iter_content(64_000):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("download exceeds bounded size")
            chunks.append(chunk)
        return b"".join(chunks)


def _private_directory(path):
    """Avoid accidental raw data publication inside this or any Git checkout."""
    result = Path(path).resolve()
    if any((ancestor / ".git").exists() for ancestor in (result, *result.parents)):
        raise ValueError("private output must be outside every Git checkout")
    result.mkdir(parents=True, exist_ok=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-output", required=True,
                        help="Private directory outside any Git checkout; no raw rows are published.")
    parser.add_argument("--summary-output", help="Optional publishable aggregate JSON path.")
    args = parser.parse_args()
    private = _private_directory(args.private_output)
    import requests
    with requests.Session() as session:
        session.headers["User-Agent"] = "f4-personal-historical-exploration/1.0"
        page = _download(SOURCE_PAGE, session=session).decode("utf-8")
        csv_url = discover_csv(page)
        raw = _download(csv_url, session=session)
    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = parse_archive(raw, retrieved_at=retrieved_at)
    result["summary"]["csv_url"] = csv_url
    digest = result["summary"]["content_sha256"][:16]
    (private / f"sgodds-epl-opening-{digest}.csv").write_bytes(raw)
    (private / f"sgodds-epl-opening-{digest}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_text = json.dumps(result["summary"], ensure_ascii=False, indent=2) + "\n"
    if args.summary_output:
        output = Path(args.summary_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(summary_text, encoding="utf-8")
    print(summary_text)


if __name__ == "__main__":
    main()
