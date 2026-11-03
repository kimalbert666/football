import unittest

from titan_history_archive import archive_history


def row(home="0.88", line="受让半球", away="1.01", time="10-13 02:36", status="即", clock="", score=""):
    return "<tr>" + "".join("<td>" + value + "</td>" for value in (clock, score, home, line, away, time, status)) + "</tr>"


def archive(body, **kwargs):
    options = dict(kickoff_display="2026-10-13 03:00", captured_at_utc="2026-10-20T12:00:00Z",
                   source_url="https://vip.titan007.com/changeDetail/handicap.aspx?id=3003902&companyID=3&l=0",
                   opening_display="2026-09-19 01:31", odds_format_verified=True)
    options.update(kwargs)
    return archive_history("<table>" + body + "</table>", **options)


class ArchiveTests(unittest.TestCase):
    def test_same_minute_different_quotes_are_preserved_and_ambiguous(self):
        value = archive(row() + row(home="0.90", away="0.99"))
        self.assertEqual([r["source_row_index"] for r in value["rows"]], [0, 1])
        self.assertEqual(value["prematch_quote_count"], 2)
        self.assertEqual(value["last_change_at_or_before_t15"]["state"], "ambiguous_same_minute")
        self.assertIsNone(value["last_change_at_or_before_t15"]["quote"])

    def test_identical_rows_are_not_deduplicated(self):
        value = archive(row() + row())
        self.assertEqual(value["data_row_count"], 2)
        self.assertEqual(value["same_minute_groups"], {"2026-10-13 02:36": 2})

    def test_endpoint_ignores_after_cutoff(self):
        value = archive(row(home="0.89", time="10-13 02:58") + row())
        self.assertEqual(value["last_change_at_or_before_t15"]["quote"]["home_decimal_odds"], 1.88)
        self.assertEqual(value["last_visible_prematch_quote"]["quote"]["home_decimal_odds"], 1.89)

    def test_final_minute_before_kickoff_is_not_cutoff_ambiguity(self):
        value = archive(row(time="10-13 02:59"))
        self.assertEqual(value["last_visible_prematch_quote"]["state"], "unique_at_displayed_minute")
        self.assertEqual(value["last_visible_prematch_quote"]["quote"]["home_decimal_odds"], 1.88)

    def test_undated_candidate_blocks_stale_endpoint(self):
        value = archive(row(time="broken date") + row())
        for key in ("earliest_visible_quote", "last_visible_prematch_quote", "last_change_at_or_before_t15"):
            self.assertEqual(value[key]["state"], "unresolved_event_time")
            self.assertIsNone(value[key]["quote"])
        self.assertEqual(value["data_row_count"], 2)

    def test_undated_explicit_inplay_does_not_block_prematch_endpoint(self):
        value = archive(row(time="broken date", status="滚", clock="01", score="0-0") + row())
        self.assertEqual(value["last_change_at_or_before_t15"]["state"], "unique_at_displayed_minute")
        self.assertFalse(value["parse_ok"])

    def test_suspension_with_colspan_is_preserved(self):
        paused = '<tr><td></td><td></td><td colspan="3">封</td><td>10-13 02:40</td><td>即</td></tr>'
        value = archive(paused + row())
        self.assertEqual(value["suspended_row_count"], 1)
        self.assertEqual(value["rows"][0]["raw_cells"][2]["text"], "封")
        self.assertFalse(value["rows"][0]["prematch_quote"])
        self.assertEqual(value["last_change_at_or_before_t15"]["state"], "suspended")
        self.assertIsNone(value["last_change_at_or_before_t15"]["quote"])

    def test_cutoff_minute_cannot_imply_exact_seconds(self):
        value = archive(row(time="10-13 02:45") + row())
        self.assertEqual(value["last_change_at_or_before_t15"]["state"], "ambiguous_cutoff_minute")
        self.assertIsNone(value["last_change_at_or_before_t15"]["quote"])

    def test_unknown_status_preserved_and_parse_fails(self):
        value = archive(row(status="新状态") + row())
        self.assertEqual(value["data_row_count"], 2)
        self.assertEqual(value["issues"]["unknown_status"], 1)
        self.assertFalse(value["parse_ok"])

    def test_suspension_with_clock_does_not_become_prematch_endpoint(self):
        value = archive(row(home="封", status="封", clock="01", score="0-0", time="10-13 02:40") + row())
        self.assertIn("suspended_status_has_clock_or_score", value["issues"])
        self.assertEqual(value["last_change_at_or_before_t15"]["state"], "unresolved_event")

    def test_nested_or_multiple_tables_rejected_before_archiving_unrelated_page_content(self):
        with self.assertRaisesRegex(ValueError, "isolated history-table"):
            archive('<table><tr><td>unrelated account/page data</td></tr></table>' + row())

    def test_inplay_never_enters_prematch_set(self):
        value = archive(row(status="滚", clock="00", score="0-0", time="10-13 03:00") + row())
        self.assertEqual(value["prematch_quote_count"], 1)
        self.assertEqual(value["data_row_count"], 2)

    def test_false_prematch_clock_and_after_kickoff_rejected(self):
        value = archive(row(clock="03", score="0-0") + row(time="10-13 03:01"))
        self.assertEqual(value["prematch_quote_count"], 0)
        self.assertIn("prematch_not_before_kickoff", value["issues"])
        self.assertIn("prematch_status_has_clock_or_score", value["issues"])

    def test_next_page_or_truncation_never_complete(self):
        value = archive(row() + '<a href="next">下一页</a>')
        self.assertEqual(value["displayed_history_coverage"], "partial")
        self.assertEqual(archive(row(), projection_truncated=True)["displayed_history_coverage"], "partial")
        self.assertEqual(archive(row())["displayed_history_coverage"], "unknown")

    def test_without_year_anchor_short_dates_are_unresolved(self):
        value = archive(row(), opening_display=None)
        self.assertIsNone(value["rows"][0]["displayed_datetime"])
        self.assertEqual(value["prematch_quote_count"], 0)

    def test_cross_year_uses_bounded_full_anchor(self):
        value = archive(row(time="9-25 01:31"), kickoff_display="2027-03-31 03:00",
                        opening_display="2026-09-25 01:31")
        self.assertEqual(value["rows"][0]["displayed_datetime"], "2026-09-25 01:31")

    def test_multi_year_ambiguity_is_not_guessed(self):
        value = archive(row(time="9-25 01:31"), kickoff_display="2027-03-31 03:00",
                        opening_display="2025-09-01 01:31")
        self.assertIsNone(value["rows"][0]["displayed_datetime"])

    def test_timezone_and_formal_eligibility_always_unverified(self):
        value = archive(row(), fixture_identity_verified=True)
        self.assertIsNone(value["rows"][0]["quote_time_utc"])
        for key in ("formal_observation", "training_eligible", "timezone_verified",
                    "bookmaker_every_change_certified", "provider_opening_reconciled"):
            self.assertFalse(value[key])

    def test_odds_conversion_requires_evidence(self):
        value = archive(row(), odds_format_verified=False)
        self.assertIsNone(value["rows"][0]["quote"]["home_decimal_odds"])
        self.assertEqual(value["rows"][0]["quote"]["home_water_raw"], "0.88")

    def test_unrecognized_shape_preserved(self):
        value = archive('<tr><td>unknown data</td></tr>' + row())
        self.assertEqual(value["unrecognized_row_count"], 1)
        self.assertFalse(value["parse_ok"])

    def test_naive_or_non_utc_capture_rejected(self):
        for timestamp in ("2026-10-20T12:00:00", "2026-10-20T12:00:00+08:00"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                archive(row(), captured_at_utc=timestamp)

    def test_unknown_handicap_preserved_and_fails_closed(self):
        value = archive(row(line="未知盘"))
        self.assertEqual(value["rows"][0]["raw_cells"][3]["text"], "未知盘")
        self.assertFalse(value["parse_ok"])

    def test_deep_quarter_handicap_is_supported(self):
        value = archive(row(line="三球半/四球"))
        self.assertTrue(value['parse_ok'])
        self.assertEqual(value['rows'][0]['quote']['home_handicap'], -3.75)

    def test_invalid_earliest_quote_cannot_be_skipped(self):
        value = archive(row(home='?', time='9-19 01:31') + row())
        self.assertEqual(value['earliest_visible_quote']['state'], 'unresolved_event')
        self.assertIsNone(value['earliest_visible_quote']['quote'])


if __name__ == "__main__":
    unittest.main()
