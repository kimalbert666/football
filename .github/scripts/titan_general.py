"""Conservative offline readers for visible Titan summary/history projections.

No fetching, bookmaker-ID inference, fixed event witness, model promotion, or
claim of a complete bookmaker tick stream takes place here. Displayed dates
remain in the provider's unverified timezone. Callers supply fixture identity
from independent mapping, and project only directly-owned table rows.
"""
from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import parse_qsl, urljoin, urlsplit

from titan_cloud_probe import Document, Node, clean, direct_cells, document_title, profit_odds


HOST = "vip.titan007.com"
DETAIL_PATH = "/changedetail/handicap.aspx"
DATE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?![:\d])")
CHINESE_DIGITS = dict(zip("零一二三四五六七八九", range(10)))


def _visible(node):
    if not node.visible():
        return False
    while node is not None:
        style = re.sub(r"\s+", "", node.attrs.get("style", "") or "").lower()
        if "visibility:collapse" in style or node.attrs.get("aria-hidden", "").lower() == "true":
            return False
        node = node.parent
    return True


def _visible_text(node):
    if not _visible(node) or node.tag in {"head", "title", "script", "style"}:
        return ""
    return " ".join(_visible_text(child) if isinstance(child, Node) else child
                    for child in node.children)


def _integer(text):
    text = text.replace("两", "二").replace("兩", "二").replace("〇", "零")
    if re.fullmatch(r"\d{1,2}", text):
        return int(text)
    if text in CHINESE_DIGITS:
        return CHINESE_DIGITS[text]
    match = re.fullmatch(r"([一二三四五六七八九]?)十([一二三四五六七八九]?)", text)
    if match:
        return CHINESE_DIGITS.get(match[1], 1) * 10 + CHINESE_DIGITS.get(match[2], 0)
    return None


def _handicap_atom(text):
    if text in {"平手", "平", "零球"}:
        return 0.0
    if text in {"半", "半球"}:
        return 0.5
    if text == "球半":
        return 1.5
    if re.fullmatch(r"\d{1,2}(?:\.\d{1,2})?", text):
        number = float(text)
        return number if number * 4 == int(number * 4) else None
    half = text.endswith("半")
    number_text = text[:-1] if half else text
    if number_text.endswith("球"):
        number_text = number_text[:-1]
    number = _integer(number_text)
    return None if number is None else float(number) + (0.5 if half else 0)


def parse_home_handicap(display):
    """Return home-signed goals; unsigned Chinese/numeric lines mean home gives.

    Numeric explicit + means receiving, - means giving. Unsigned numbers,
    Chinese integer/half lines (0..99.5), and adjacent half-goal split lines
    are accepted. Unknown words and non-quarter numeric lines are rejected.
    """
    if not isinstance(display, str):
        return None
    text = re.sub(r"\s+", "", display).replace("／", "/").replace("−", "-")
    prefix = re.match(r"^(受让|受讓|受|让|讓)", text)
    direction = -1
    if prefix:
        direction = 1 if prefix[1].startswith("受") else -1
        text = text[len(prefix[1]):]
        if text.startswith(("+", "-")):
            return None
    elif text.startswith(("+", "-")):
        direction = 1 if text[0] == "+" else -1
        text = text[1:]
    parts = text.split("/")
    values = [_handicap_atom(part) for part in parts]
    if len(values) not in (1, 2) or any(value is None for value in values):
        return None
    if len(values) == 2:
        # Split handicaps represent adjacent integer/half lines, not ranges.
        if values[1] - values[0] != 0.5 or any(value * 2 != int(value * 2) for value in values):
            return None
    value = sum(values) / len(values)
    return 0.0 if value == 0 else direction * value


def _datetime(match):
    values = [int(value or 0) for value in match.groups()]
    return datetime(*values)


def _parse_expected_date(value):
    if not isinstance(value, str):
        return None
    match = DATE.fullmatch(clean(value))
    try:
        return _datetime(match) if match else None
    except ValueError:
        return None


def _query(url):
    parsed = urlsplit(url)
    query = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        query.setdefault(key.lower(), []).append(value)
    return parsed, query


def _url_issues(url, event_id, company_id):
    if not isinstance(url, str) or not url:
        return ["detail_url_invalid"]
    try:
        parsed, query = _query(url)
        correct_origin = (parsed.scheme == "https" and parsed.hostname == HOST
                          and parsed.port in {None, 443} and parsed.username is None
                          and parsed.password is None and not parsed.fragment)
    except (TypeError, ValueError):
        return ["detail_url_invalid"]
    issues = []
    if not correct_origin:
        issues.append("detail_url_origin_mismatch")
    if parsed.path.lower() != DETAIL_PATH:
        issues.append("detail_url_path_mismatch")
    if query != {"id": [str(event_id)], "companyid": [str(company_id)], "l": ["0"]}:
        issues.append("detail_url_query_mismatch")
    return issues


def _valid_expected_identity(event_id, home, away, company_ids):
    return (re.fullmatch(r"[1-9]\d*", str(event_id)) is not None
            and bool(company_ids) and len(set(company_ids)) == len(company_ids)
            and all(re.fullmatch(r"[1-9]\d*", company) for company in company_ids)
            and isinstance(home, str) and isinstance(away, str)
            and bool(clean(home)) and bool(clean(away)) and clean(home) != clean(away))


def validate_history_identity(projection, source_url, event_id, company_id, expected_home, expected_away):
    """Validate an explicit seven-label header projection plus exact source URL.

    ``projection['header_labels']`` must come from a single visible row owned
    directly by the uniquely selected history table. Content validation and
    archival are handled separately by titan_history_archive.
    """
    issues = _url_issues(source_url, event_id, company_id)
    if not _valid_expected_identity(event_id, expected_home, expected_away, (str(company_id),)):
        issues.append("expected_identity_invalid")
    labels = projection.get("header_labels") if isinstance(projection, dict) else None
    # The real 2026-10-27 cloud artifact displays the middle header as '盘'.
    # Do not silently accept an empty/moved label as the same market layout.
    expected = ["时间", "比分", expected_home, "盘", expected_away, "变化时间", "状态"]
    if (not isinstance(labels, list) or len(labels) != 7
            or not all(isinstance(label, str) for label in labels)
            or [clean(label) for label in labels] != expected):
        issues.append("history_header_identity_mismatch")
    if isinstance(projection, dict):
        if projection.get("table_found") is not True or projection.get("table_count") != 1:
            issues.append("history_table_missing_or_ambiguous")
        if projection.get("projection_truncated"):
            issues.append("history_projection_truncated")
    return {"identity_verified": not issues, "event_id": str(event_id),
            "company_id": str(company_id), "issues": issues}


def _opening(cells, kickoff, odds_format_verified):
    issues = []
    if len(cells) != 3 or not all(_visible(cell) for cell in cells):
        return None, ["opening_cells_missing_or_hidden"]
    if any(cell.attrs.get("colspan", "1") != "1" for cell in cells):
        return None, ["opening_cells_colspan_unsupported"]
    texts = [clean(_visible_text(cell)) for cell in cells]
    home, away = profit_odds(texts[0]), profit_odds(texts[2])
    handicap = parse_home_handicap(texts[1])
    if home is None or away is None or handicap is None:
        issues.append("opening_quote_invalid")
    dates = set()
    for cell in cells:
        for node in [cell, *cell.descendants()]:
            if not _visible(node):
                continue
            for match in DATE.finditer(node.attrs.get("title", "") or ""):
                try:
                    dates.add(_datetime(match))
                except ValueError:
                    issues.append("opening_datetime_invalid")
    if len(dates) != 1:
        issues.append("opening_datetime_missing" if not dates else "opening_datetime_ambiguous")
    date = next(iter(dates)) if len(dates) == 1 else None
    if date is not None and date >= kickoff:
        issues.append("opening_datetime_not_before_kickoff")
    if issues:
        return None, list(dict.fromkeys(issues))
    return {"home_water_raw": texts[0], "away_water_raw": texts[2],
            "home_profit_only_odds": home if odds_format_verified else None,
            "away_profit_only_odds": away if odds_format_verified else None,
            "home_decimal_odds": round(home + 1, 3) if odds_format_verified else None,
            "away_decimal_odds": round(away + 1, 3) if odds_format_verified else None,
            "handicap_display": texts[1], "home_handicap": handicap,
            "displayed_full_datetime": date.strftime("%Y-%m-%d %H:%M:%S" if date.second else "%Y-%m-%d %H:%M"),
            "quote_time_utc": None}, []


def parse_opening_summary(html, event_id, expected_home, expected_away, expected_kickoff_display,
                          company_ids=("3",), *, odds_format_verified=False):
    """Parse requested companies only, retaining rejected-row diagnostics.

    Input is a visible-DOM projection containing title/body and table#odds.
    Initial columns are exactly [3:6]; nested-table rows are never considered.
    A successful parse verifies these displayed fields, not authentic first-ever
    bookmaker opening, odds-format provenance, timezone, or complete history.
    ``odds_format_verified=True`` is reserved for an independently checked HK
    display format. Otherwise raw water strings are retained and interpreted
    profit-only/decimal fields remain null. This flag never certifies timezone.
    """
    # A string such as '48' is one ID, never an iterable of company 4 and 8.
    companies = (tuple(str(company) for company in company_ids)
                 if isinstance(company_ids, (list, tuple, set, frozenset)) else ())
    verified_odds = odds_format_verified is True
    issues, selected = [], {}
    result = {"parse_ok": False, "event_id": str(event_id), "companies": selected,
              "issues": issues, "research_only": True, "formal_signal_eligible": False,
              "bookmaker_original_opening_certified": False, "complete_tick_history_certified": False,
              "timezone_verified": False, "odds_format_verified": verified_odds}
    if not _valid_expected_identity(event_id, expected_home, expected_away, companies):
        issues.append("expected_identity_invalid")
        return result
    kickoff = _parse_expected_date(expected_kickoff_display)
    if kickoff is None:
        issues.append("expected_kickoff_invalid")
        return result
    document = Document(html)
    title = document_title(document)
    result["document_title"] = title
    title_pattern = (r"^" + re.escape(expected_home) + r"\s*VS\s*" + re.escape(expected_away)
                     + r"(?=\s*(?:[（(\-|—]|$))")
    if title is None or not re.search(title_pattern, title, re.I):
        issues.append("fixture_title_home_away_mismatch")
    visible_dates = set()
    for match in DATE.finditer(clean(_visible_text(document.root))):
        try:
            visible_dates.add(_datetime(match))
        except ValueError:
            pass
    if kickoff not in visible_dates:
        issues.append("expected_fixture_kickoff_not_found_in_visible_text")
    tables = [node for node in document.root.descendants("table") if node.attrs.get("id") == "odds" and _visible(node)]
    if len(tables) != 1:
        issues.append("odds_table_missing_or_ambiguous")
    if issues:
        return result
    table = tables[0]
    seen = set()
    for row in table.descendants("tr"):
        ancestor = row.parent
        while ancestor is not None and ancestor.tag != "table":
            ancestor = ancestor.parent
        if ancestor is not table or not _visible(row):
            continue
        # Direct cell positions have meaning only in a flat company row.
        # A nested layout table must not lend its links/quotes to its parent.
        if any(row.descendants("table")):
            issues.append("nested_table_in_company_row")
            continue
        links = []
        for anchor in row.descendants("a"):
            if not _visible(anchor):
                continue
            href = urljoin(f"https://{HOST}/AsianOdds_n.aspx?id={event_id}", anchor.attrs.get("href", ""))
            try:
                _, query = _query(href)
            except ValueError:
                continue
            targets = set(query.get("companyid", [])) & set(companies)
            for company in sorted(targets):
                links.append((company, href, _url_issues(href, event_id, company)))
        if not links:
            continue
        # Several visible cells may link to the identical history page. This
        # is one destination, unlike two company/event/market destinations.
        links = list({(company, href): (company, href, errors)
                      for company, href, errors in links}.values())
        targets = {item[0] for item in links}
        duplicate = targets & seen
        seen.update(targets)
        for company in duplicate:
            selected.pop(company, None)
            issues.append(f"company_{company}_duplicate_rows")
        if len(links) != 1:
            issues.append("ambiguous_detail_links:" + ",".join(sorted(targets)))
            continue
        company, href, link_issues = links[0]
        if link_issues:
            issues.extend(f"company_{company}_{issue}" for issue in link_issues)
            continue
        if duplicate:
            continue
        cells = direct_cells(row)
        if not 6 <= len(cells) <= 24:
            issues.append(f"company_{company}_row_shape_invalid")
            continue
        # Colspan before opening would alter physical column positions.
        if any(cell.attrs.get("colspan", "1") != "1" for cell in cells[:3]):
            issues.append(f"company_{company}_leading_cells_colspan_unsupported")
            continue
        label = clean(_visible_text(cells[1]))
        if not label:
            issues.append(f"company_{company}_label_missing")
            continue
        opening, row_issues = _opening(cells[3:6], kickoff, verified_odds)
        if row_issues:
            issues.extend(f"company_{company}_{issue}" for issue in row_issues)
            continue
        selected[company] = {"company_id": company, "company_label": label,
                             "history_url": href, "opening": opening}
    missing = sorted(set(companies) - set(selected))
    if missing:
        issues.append("missing_companies:" + ",".join(missing))
    result["issues"] = list(dict.fromkeys(issues))
    result["parse_ok"] = not issues
    return result
