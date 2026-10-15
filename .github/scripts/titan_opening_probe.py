"""Read-only Titan opening-odds diagnostic; standard library only.

This deliberately fixed, retrospective example is not a prediction, an odds feed,
or proof that the provider captured the bookmaker's first ever offer.  It keeps
only a few diagnostic fields.  It neither saves HTML nor uses authentication.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


SUMMARY_URL = "https://vip.titan007.com/AsianOdds_n.aspx?id=3003902"
DETAIL_URL = "https://vip.titan007.com/changeDetail/handicap.aspx?id=3003902&companyID=3&l=0"
EXPECTED_KICKOFF = "2026-10-13 03:00"
TARGET_COMPANIES = {"3": "Crow*", "8": "36*", "48": "香港马*"}
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 2_000_000
FULL_DATE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})\b")
SHORT_DATE = re.compile(r"^(\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})$")


class Node:
    def __init__(self, tag="document", attrs=(), parent=None):
        self.tag = tag.lower()
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []

    def descendants(self, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.descendants(tag)

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        return " ".join(
            child.text() if isinstance(child, Node) else child
            for child in self.children
        ).strip()

    def visible(self):
        node = self
        while node is not None:
            style = re.sub(r"\s+", "", node.attrs.get("style", "") or "").lower()
            if "hidden" in node.attrs or "display:none" in style or "visibility:hidden" in style:
                return False
            node = node.parent
        return True


class Document(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        # Tolerate optional end tags without letting a wrapper acquire its
        # nested table's cells. Only direct cells are ever read below.
        if tag in {"td", "th"} and self.stack[-1].tag in {"td", "th"}:
            self.stack.pop()
        if tag == "tr" and self.stack[-1].tag == "tr":
            self.stack.pop()
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def direct_cells(row):
    return [child for child in row.children if isinstance(child, Node) and child.tag == "td"]


def profit_odds(text):
    value = clean(text)
    if not re.fullmatch(r"\d{1,2}(?:\.\d{1,3})?", value):
        return None
    number = float(value)
    return number if 0 < number < 20 else None


def home_handicap(text):
    value = re.sub(r"\s+", "", text).replace("／", "/")
    receiving = value.startswith(("受让", "受讓", "受"))
    value = re.sub(r"^(受让|受讓|受)", "", value)
    values = {
        "平手": 0, "平手/半球": .25, "平/半": .25, "半球": .5,
        "半球/一球": .75, "半/一": .75, "一球": 1,
        "一球/球半": 1.25, "一/球半": 1.25, "球半": 1.5,
        "球半/两球": 1.75, "球半/兩球": 1.75, "两球": 2, "兩球": 2,
        "两球/两球半": 2.25, "兩球/兩球半": 2.25,
        "两球半": 2.5, "兩球半": 2.5, "两球半/三球": 2.75,
        "兩球半/三球": 2.75, "三球": 3,
    }
    if value not in values:
        return None
    return values[value] if receiving else -values[value]


def title_datetime(cells):
    dates = set()
    for cell in cells:
        for node in [cell, *cell.descendants()]:
            for match in FULL_DATE.finditer(node.attrs.get("title", "") or ""):
                try:
                    dates.add(datetime(*(int(part) for part in match.groups())).strftime("%Y-%m-%d %H:%M"))
                except ValueError:
                    pass
    return next(iter(dates)) if len(dates) == 1 else None


def company_links(row):
    found = {}
    for anchor in row.descendants("a"):
        href = urllib.parse.urljoin(SUMMARY_URL, anchor.attrs.get("href", ""))
        parsed = urllib.parse.urlparse(href)
        query = {key.lower(): values for key, values in urllib.parse.parse_qs(parsed.query).items()}
        company = query.get("companyid", [None])[0]
        if (parsed.scheme == "https" and parsed.hostname == "vip.titan007.com"
                and parsed.path.lower() == "/changedetail/handicap.aspx"
                and query.get("id") == ["3003902"] and company in TARGET_COMPANIES):
            found[company] = href
    return found


def quote_fields(cells):
    texts = [clean(cell.text()) for cell in cells]
    home, away = profit_odds(texts[0]), profit_odds(texts[2])
    handicap = home_handicap(texts[1])
    if home is None or away is None or handicap is None:
        return None
    return {
        "home_profit_only_odds": home,
        "handicap_display": texts[1],
        "home_handicap": handicap,
        "away_profit_only_odds": away,
        "home_decimal_odds": round(home + 1, 3),
        "away_decimal_odds": round(away + 1, 3),
    }


def document_title(document):
    return next((clean(node.text())[:200] for node in document.root.descendants("title")), None)


def parse_summary(html):
    document = Document(html)
    selected = {}
    issues = []
    for row in document.root.descendants("tr"):
        cells = direct_cells(row)
        links = company_links(row)
        if len(links) != 1 or not 6 <= len(cells) <= 24:
            continue
        company, detail_url = next(iter(links.items()))
        # Initial columns precede live/closing columns. Hidden live cells must
        # never become a competing candidate or alter visible column positions.
        visible = [cell for cell in cells if cell.visible()]
        triples = []
        for index in range(len(visible) - 2):
            triple = visible[index:index + 3]
            fields = quote_fields(triple)
            if fields is not None:
                triples.append((fields, title_datetime(triple)))
        if not triples or triples[0][1] is None:
            issues.append(f"company_{company}_opening_datetime_missing")
            continue
        opening, date = triples[0]
        item = {
            "company_id": company,
            "company_label": TARGET_COMPANIES[company],
            "opening": {**opening, "displayed_full_datetime": date},
            "history_url": detail_url,
        }
        if company in selected and selected[company] != item:
            issues.append(f"company_{company}_ambiguous_rows")
            continue
        selected[company] = item
    title = document_title(document)
    kickoff_confirmed = EXPECTED_KICKOFF in clean(document.root.text())
    missing = sorted(set(TARGET_COMPANIES) - set(selected))
    if missing:
        issues.append("missing_companies:" + ",".join(missing))
    if not kickoff_confirmed:
        issues.append("expected_fixture_kickoff_not_found_in_visible_text")
    return {
        "parse_ok": len(selected) == 3 and kickoff_confirmed and not issues,
        "page_title": title,
        "expected_fixture_kickoff_found": kickoff_confirmed,
        "companies": [selected[key] for key in TARGET_COMPANIES if key in selected],
        "issues": sorted(set(issues)),
    }


def inferred_datetime(text, kickoff):
    full = FULL_DATE.fullmatch(clean(text))
    if full:
        try:
            return datetime(*(int(part) for part in full.groups()))
        except ValueError:
            return None
    short = SHORT_DATE.fullmatch(clean(text))
    if not short:
        return None
    candidates = []
    for year in (kickoff.year - 1, kickoff.year, kickoff.year + 1):
        try:
            candidates.append(datetime(year, *(int(part) for part in short.groups())))
        except ValueError:
            pass
    return min(candidates, key=lambda value: abs((value - kickoff).total_seconds())) if candidates else None


def parse_history(html, kickoff_text=EXPECTED_KICKOFF):
    document = Document(html)
    kickoff = datetime.strptime(kickoff_text, "%Y-%m-%d %H:%M")
    records, live, issues = [], [], []
    for row in document.root.descendants("tr"):
        cells = direct_cells(row)
        if len(cells) != 7 or not row.visible():
            continue
        texts = [clean(cell.text()) for cell in cells]
        status = texts[6]
        if status not in {"早", "即", "滚", "滾"}:
            continue
        fields = quote_fields(cells[2:5])
        when = inferred_datetime(texts[5], kickoff)
        if fields is None or when is None:
            issues.append("unparseable_quote_row")
            continue
        record = {
            **fields,
            "displayed_change_time": texts[5],
            "fixture_year_inferred_datetime": when.strftime("%Y-%m-%d %H:%M"),
            "status": status,
        }
        if status in {"滚", "滾"}:
            live.append((when, record))
        elif texts[0] or texts[1]:
            # An in-play clock/score disqualifies a nominally prematch row.
            issues.append("prematch_status_has_clock_or_score")
        elif when >= kickoff:
            issues.append("prematch_quote_not_before_kickoff")
        else:
            records.append((when, record))
    records.sort(key=lambda item: item[0])
    live.sort(key=lambda item: item[0])
    before_t15 = [item for item in records if item[0] <= kickoff - timedelta(minutes=15)]
    if not records:
        issues.append("no_valid_prematch_records")
    return {
        "parse_ok": bool(records) and bool(before_t15) and not issues,
        "page_title": document_title(document),
        "prematch_row_count": len(records),
        "first_opening": records[0][1] if records else None,
        "last_prematch": records[-1][1] if records else None,
        "last_change_at_or_before_t15": before_t15[-1][1] if before_t15 else None,
        "first_inplay": live[0][1] if live else None,
        "issues": sorted(set(issues)),
    }


class SameProviderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (host == "titan007.com" or host.endswith(".titan007.com")):
            raise urllib.error.URLError("redirect_outside_https_provider")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def decode_html(payload, content_type):
    match = re.search(r"charset\s*=\s*['\"]?([^\s;'\">]+)", content_type, re.I)
    declared = match.group(1) if match else None
    if not declared:
        match = re.search(rb"charset\s*=\s*['\"]?([\w-]+)", payload[:8192], re.I)
        declared = match.group(1).decode("ascii") if match else None
    encodings = [declared, "utf-8", "gb18030"]
    for encoding in dict.fromkeys(value for value in encodings if value):
        try:
            return payload.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError):
            pass
    raise ValueError("unsupported_or_invalid_response_encoding")


def fetch_and_parse(url, referer, parser):
    result = {"url": url, "fetched_at_utc": datetime.now(timezone.utc).isoformat(), "transport_ok": False, "parse_ok": False}
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"})
    # urllib's default HTTPS handler verifies certificates. No cookies, tokens,
    # login sessions, custom CA relaxation, retries, or alternate endpoints.
    opener = urllib.request.build_opener(SameProviderRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            result["http_status"] = response.status
            result["final_url"] = response.url
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("response_exceeded_diagnostic_size_limit")
            html, encoding = decode_html(payload, response.headers.get("Content-Type", ""))
            result["response_encoding"] = encoding
            result["response_bytes"] = len(payload)
            result["transport_ok"] = response.status == 200
        if result["transport_ok"]:
            result.update(parser(html))
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        # Avoid printing response bodies or any local proxy configuration.
        result["error"] = "request_or_parse_failed"
        if isinstance(exc, urllib.error.HTTPError):
            result["http_status"] = exc.code
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional limited JSON evidence file; never writes raw HTML")
    args = parser.parse_args()
    summary = fetch_and_parse(SUMMARY_URL, "https://www.titan007.com/", parse_summary)
    history = fetch_and_parse(DETAIL_URL, SUMMARY_URL, parse_history)
    crow = next((row for row in summary.get("companies", []) if row["company_id"] == "3"), None)
    opening = history.get("first_opening")
    opening_matches = bool(crow and opening and all(crow["opening"][field] == opening[field] for field in ("home_profit_only_odds", "home_handicap", "away_profit_only_odds")) and crow["opening"]["displayed_full_datetime"] == opening["fixture_year_inferred_datetime"])
    output = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "retrospective_public_source_connectivity_and_field_diagnostic",
        "match_id": "3003902", "fixture": "Coventry-Newcastle",
        "fixture_kickoff_displayed": EXPECTED_KICKOFF,
        "fixture_context_source": "independently_observed_browser_fixture_page",
        "time_zone": "displayed_local_time_not_independently_confirmed",
        "formal_observation": False, "candidate_training_input": False,
        "source_declared_opening_only": True,
        "network_timeout_seconds_per_request": TIMEOUT_SECONDS,
        "summary": summary, "history": history,
        "opening_summary_history_agree": opening_matches,
        "diagnostic_pass": bool(summary.get("parse_ok") and history.get("parse_ok") and opening_matches),
        "limits": [
            "A historic last change before T15 is not a prospective T15 capture.",
            "An aggregator opening label is not proof of the bookmaker's first ever quote.",
            "Displayed timestamps retain the provider's clock; no UTC conversion is asserted.",
            "A successful single-match probe does not establish continuous EPL coverage.",
        ],
    }
    serialized = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0  # Source/network failures are evidence, not failure-notification jobs.


if __name__ == "__main__":
    raise SystemExit(main())
