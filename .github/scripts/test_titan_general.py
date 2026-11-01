"""Safety contracts for identity, opening columns, and odds interpretation."""
from html import escape
import json
from pathlib import Path
import unittest

from titan_general import parse_home_handicap, parse_opening_summary, validate_history_identity


EVENT = "3003902"
HOME = "考文垂"
AWAY = "纽卡斯尔联"
KICKOFF = "2026-10-13 03:00"
OPENING = "2026-09-19 01:31"


def url(event=EVENT, company="3"):
    return f"https://vip.titan007.com/changeDetail/handicap.aspx?id={event}&companyID={company}&l=0"


def row(company="3", *, event=EVENT, label="Crow*", water="1.02", line="受让平手/半球",
        away="0.86", opening=OPENING, link=None):
    cells = ["", escape(label), "", f'<span title="{escape(opening, quote=True)}">{escape(water)}</span>',
             escape(line), escape(away), "1.92", "半球", "1.93",
             f'<a href="{escape(link or url(event, company), quote=True)}">详</a>']
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def document(rows=None, *, title=None, body=KICKOFF):
    return ("<html><head><title>" + (title or f"{HOME}VS{AWAY}(2026-2027赛季英超)-亚指指数")
            + "</title></head><body><div>" + body + '</div><table id="odds">'
            + (row() if rows is None else rows) + "</table></body></html>")


def parse(html=None, **options):
    args = dict(event_id=EVENT, expected_home=HOME, expected_away=AWAY,
                expected_kickoff_display=KICKOFF)
    args.update(options)
    return parse_opening_summary(document() if html is None else html, **args)


def history(**options):
    args = dict(projection={"header_labels": ["时间", "比分", HOME, "盘", AWAY, "变化时间", "状态"],
                            "table_found": True, "table_count": 1},
                source_url=url(), event_id=EVENT, company_id="3", expected_home=HOME, expected_away=AWAY)
    args.update(options)
    return validate_history_identity(**args)


class SummaryIdentityTests(unittest.TestCase):
    def test_valid_single_company_preserves_actual_label_and_raw_odds(self):
        result = parse(document(row(label="原样公司名称")))
        self.assertTrue(result["parse_ok"], result["issues"])
        company = result["companies"]["3"]
        self.assertEqual(company["company_label"], "原样公司名称")
        self.assertEqual(company["opening"]["home_water_raw"], "1.02")
        self.assertEqual(company["opening"]["home_handicap"], 0.25)
        self.assertEqual(company["opening"]["displayed_full_datetime"], OPENING)

    def test_odds_conversion_requires_explicit_verified_format(self):
        for flag in (False, None, "false", "HK", 1):
            result = parse(odds_format_verified=flag)
            self.assertFalse(result["odds_format_verified"])
            for key in ("home_profit_only_odds", "away_profit_only_odds", "home_decimal_odds", "away_decimal_odds"):
                self.assertIsNone(result["companies"]["3"]["opening"][key])
        verified = parse(odds_format_verified=True)
        self.assertEqual(verified["companies"]["3"]["opening"]["home_decimal_odds"], 2.02)
        self.assertEqual(verified["companies"]["3"]["opening"]["away_decimal_odds"], 1.86)

    def test_never_certifies_timezone_bookmaker_first_or_formal_signal(self):
        result = parse(odds_format_verified=True)
        for key in ("timezone_verified", "bookmaker_original_opening_certified", "complete_tick_history_certified",
                    "formal_signal_eligible"):
            self.assertFalse(result[key])
        self.assertIsNone(result["companies"]["3"]["opening"]["quote_time_utc"])

    def test_multiple_requested_companies_and_partial_missing(self):
        html = document(row() + row("8", label="36*", water="0.83", line="受让半球", away="1.03"))
        result = parse(html, company_ids=("3", "8"))
        self.assertTrue(result["parse_ok"], result["issues"])
        self.assertEqual(set(result["companies"]), {"3", "8"})
        missing = parse(html, company_ids=("3", "8", "48"))
        self.assertFalse(missing["parse_ok"])
        self.assertIn("missing_companies:48", missing["issues"])
        self.assertEqual(set(missing["companies"]), {"3", "8"})

    def test_fixture_home_away_and_exact_title_not_substring(self):
        for title in (f"{AWAY}VS{HOME}(英超)", f"{HOME}VS{AWAY}青年队(英超)",
                      f"其他球队VS{AWAY}(英超)", f"历史{HOME}VS{AWAY}(英超)"):
            with self.subTest(title=title):
                result = parse(document(title=title))
                self.assertFalse(result["parse_ok"])
                self.assertIn("fixture_title_home_away_mismatch", result["issues"])

    def test_wrong_or_hidden_kickoff_rejected(self):
        for body in ("2026-10-13 03:01", '<span hidden>' + KICKOFF + "</span>",
                     '<script>"' + KICKOFF + '"</script>', '<span style="visibility:collapse">' + KICKOFF + "</span>"):
            with self.subTest(body=body):
                self.assertFalse(parse(document(body=body))["parse_ok"])

    def test_invalid_expected_identity_fails_without_exception(self):
        for options in ({"event_id": "abc"}, {"event_id": "0"}, {"company_ids": "48"},
                        {"company_ids": None}, {"company_ids": ("3", "3")}, {"company_ids": ("x",)},
                        {"expected_home": ""}, {"expected_away": HOME}, {"expected_home": None},
                        {"expected_kickoff_display": "2026-02-30 03:00"}):
            with self.subTest(options=options):
                self.assertFalse(parse(**options)["parse_ok"])

    def test_detail_url_identity_rejects_event_company_market_and_origin_changes(self):
        candidates = [url(event="999"), url(company="48"), url().replace("l=0", "l=1"),
                      url() + "&id=" + EVENT, url() + "&unexpected=1", url().replace("https:", "http:"),
                      url().replace("vip.titan007.com", "vip.titan007.com.other.example"),
                      url().replace("handicap.aspx", "total.aspx"), url() + "#other",
                      url().replace("vip.titan007.com", "user:pass@vip.titan007.com")]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertFalse(parse(document(row(link=candidate)))["parse_ok"])

    def test_query_order_and_relative_link_do_not_change_identity(self):
        candidate = f"/changeDetail/handicap.aspx?l=0&companyID=3&id={EVENT}"
        self.assertTrue(parse(document(row(link=candidate)))["parse_ok"])

    def test_duplicate_company_rows_are_rejected_even_identical(self):
        for rows in (row() + row(), row() + row(water="1.03"), row() * 3):
            result = parse(document(rows))
            self.assertFalse(result["parse_ok"])
            self.assertNotIn("3", result["companies"])
            self.assertIn("company_3_duplicate_rows", result["issues"])

    def test_multiple_detail_links_in_one_row_rejected(self):
        html = row().replace("</a>", '</a><a href="' + escape(url(), quote=True) + '">再详</a>')
        self.assertFalse(parse(document(html))["parse_ok"])

    def test_hidden_duplicate_is_not_selected(self):
        html = row() + row().replace("<tr>", '<tr style="display:none">', 1)
        self.assertTrue(parse(document(html))["parse_ok"])

    def test_missing_or_duplicate_odds_table_fails(self):
        self.assertFalse(parse(document().replace('id="odds"', 'id="other"'))["parse_ok"])
        self.assertFalse(parse(document().replace("</body>", '<table id="odds"></table></body>'))["parse_ok"])

    def test_nested_table_cannot_supply_detail_identity(self):
        nested = row().replace('<a href=', '<table><tr><td><a href=').replace('</a>', '</a></td></tr></table>')
        result = parse(document(nested))
        self.assertFalse(result["parse_ok"])
        self.assertIn("nested_table_in_company_row", result["issues"])


class OpeningColumnsTests(unittest.TestCase):
    def test_closing_odds_never_replace_missing_opening(self):
        for bad in ("", "封", "-", "NaN", "-0.9", "0", "20"):
            with self.subTest(bad=bad):
                result = parse(document(row(water=bad)))
                self.assertFalse(result["parse_ok"])
                self.assertNotIn("3", result["companies"])

    def test_hidden_opening_cell_not_skipped_to_next_column(self):
        html = row().replace('<td><span title=', '<td hidden><span title=', 1)
        result = parse(document(html))
        self.assertFalse(result["parse_ok"])
        self.assertIn("company_3_opening_cells_missing_or_hidden", result["issues"])

    def test_hidden_initial_content_cannot_supply_value_or_timestamp(self):
        for change in (row().replace('<span title=', '<span hidden title=', 1),
                       row().replace('<span title=', '<span aria-hidden="true" title=', 1)):
            self.assertFalse(parse(document(change))["parse_ok"])

    def test_leading_hidden_cell_preserves_physical_position(self):
        html = row().replace("<td></td>", "<td hidden></td>", 1)
        self.assertTrue(parse(document(html))["parse_ok"])

    def test_colspan_cannot_shift_opening(self):
        for html in (row().replace("<td>", '<td colspan="2">', 1),
                     row().replace('<td><span title=', '<td colspan="2"><span title=', 1)):
            self.assertFalse(parse(document(html))["parse_ok"])

    def test_opening_time_missing_invalid_or_not_before_kickoff(self):
        for value in ("", "9-19 01:31", "2026-02-30 01:31", KICKOFF, "2026-10-14 03:00",
                      "2026-09-19 01:31:999"):
            with self.subTest(value=value):
                self.assertFalse(parse(document(row(opening=value)))["parse_ok"])

    def test_conflicting_opening_tooltips_fail(self):
        html = row().replace("</span>", '</span><span title="2026-09-20 01:31"></span>', 1)
        self.assertFalse(parse(document(html))["parse_ok"])

    def test_hidden_tooltip_is_not_evidence(self):
        html = row(opening="").replace("</span>", '<span hidden title="' + OPENING + '"></span></span>', 1)
        self.assertFalse(parse(document(html))["parse_ok"])


class HandicapTests(unittest.TestCase):
    def test_numeric_quarter_lines_beyond_three_in_both_directions(self):
        for quarter in range(0, 81):
            value = quarter / 4
            for prefix, direction in (("", -1), ("-", -1), ("+", 1), ("受让", 1), ("让", -1)):
                with self.subTest(value=value, prefix=prefix):
                    self.assertEqual(parse_home_handicap(prefix + str(value)), value * direction)

    def test_chinese_and_split_lines(self):
        cases = {"平手": 0, "平/半": -.25, "半/一": -.75, "一/球半": -1.25,
                 "球半/两球": -1.75, "兩球半/三球": -2.75, "三球/三球半": -3.25,
                 "三球半": -3.5, "三球半/四球": -3.75, "四球": -4,
                 "受让四球/四球半": 4.25, "受讓四球半/五球": 4.75,
                 "受五球半": 5.5, "十一球/十一球半": -11.25,
                 "0/0.5": -.25, "受让3.5/4": 3.75, "受让 3.5 ／ 4": 3.75}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_home_handicap(text), expected)

    def test_invalid_or_ambiguous_handicaps_are_not_guessed(self):
        for text in ("", "未知", "封", "0.1", "NaN", "inf", "半球/两球", "一球/半球", "0.25/0.75",
                     "1/1.5/2", "受让-0.5", "+受让半球", "--1", "-1/+1.5", None):
            with self.subTest(text=text):
                self.assertIsNone(parse_home_handicap(text))

    def test_large_handicap_is_parsed_by_summary(self):
        result = parse(document(row(line="四球半/五球")))
        self.assertTrue(result["parse_ok"])
        self.assertEqual(result["companies"]["3"]["opening"]["home_handicap"], -4.75)


class HistoryIdentityTests(unittest.TestCase):
    def test_valid_header_and_exact_destination(self):
        self.assertTrue(history()["identity_verified"])

    def test_actual_archived_header_has_expected_layout(self):
        artifact = Path(__file__).with_name("cloud-result-40456999904.json")
        if not artifact.exists():
            self.skipTest("local witness artifact not packaged with source")
        value = json.loads(artifact.read_text(encoding="utf-8"))
        header = value["evidence"]["pages"]["history"]["displayed_prematch_rows"][0]
        self.assertEqual([cell["text"] for cell in header["cells"]],
                         ["时间", "比分", HOME, "盘", AWAY, "变化时间", "状态"])

    def test_wrong_or_duplicate_event_company_market_parameters_rejected(self):
        for changed in (url(event="99"), url(company="8"), url().replace("l=0", "l=1"),
                        url() + "&companyID=3", url() + "&id=" + EVENT,
                        url().replace("https:", "http:"), url().replace("handicap.aspx", "other.aspx")):
            with self.subTest(changed=changed):
                self.assertFalse(history(source_url=changed)["identity_verified"])

    def test_header_reordering_blank_market_or_malformed_labels_rejected(self):
        for labels in (["时间", "比分", AWAY, "盘", HOME, "变化时间", "状态"],
                       ["时间", "比分", HOME, "", AWAY, "变化时间", "状态"],
                       ["时间", "比分", HOME, "盘", AWAY, "变化时间"], None):
            with self.subTest(labels=labels):
                projection = {"header_labels": labels, "table_found": True, "table_count": 1}
                self.assertFalse(history(projection=projection)["identity_verified"])

    def test_unique_table_presence_must_be_explicit(self):
        projection = {"header_labels": ["时间", "比分", HOME, "盘", AWAY, "变化时间", "状态"]}
        for values in ({}, {"table_found": True}, {"table_found": True, "table_count": 2},
                       {"table_found": False, "table_count": 1},
                       {"table_found": True, "table_count": 1, "projection_truncated": True}):
            self.assertFalse(history(projection={**projection, **values})["identity_verified"])


if __name__ == "__main__":
    unittest.main()
