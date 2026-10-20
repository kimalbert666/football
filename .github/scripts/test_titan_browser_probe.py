import asyncio
import unittest
from unittest.mock import AsyncMock

import titan_browser_probe as probe


def cell(text, **extra):
    return {"visible": True, "text": text, **extra}


def row(home="1.02", line="受让平手/半球", away="0.86", when="9-19 01:31", status="早"):
    return [cell(text) for text in ("", "", home, line, away, when, status)]


class BrowserDiagnosticTests(unittest.TestCase):
    def test_explicit_restrictions_stop(self):
        for status in (403, 429, 442):
            self.assertTrue(probe.restriction_reason(status, {}))
        for text in ("非法访问，您的行为已被WAF系统记录！", "Verify you are human", "Access denied"):
            self.assertTrue(probe.restriction_reason(200, {"body": text}))
        self.assertFalse(probe.restriction_reason(200, {"body": "亚赔变化表"}))

    def test_hidden_cells_preserve_positions(self):
        projection = {"rows": [[cell("shown"), {"visible": False, "text": "secret"}, cell("third")]]}
        html = probe.projected_html(projection)
        self.assertIn("<td hidden></td>", html)
        self.assertNotIn("secret", html)
        self.assertEqual(html.count("<td"), 3)

    def test_opening_summary_visible_tooltip_and_witness(self):
        cases = (("3", "1.02", "受让平手/半球", "0.86", "2026-09-19 01:31"),
                 ("8", "0.83", "受让半球", "1.03", "2026-09-13 01:32"),
                 ("48", "0.90", "受让平手/半球", "0.86", "2026-10-08 18:39"))
        rows = []
        for company, home, line, away, when in cases:
            rows.append([cell(""), cell(company), cell(""), cell(home, titles=[when]), cell(line), cell(away),
                cell("详", links=[{"href": probe.DETAIL_URL.replace("companyID=3", "companyID=" + company), "text": "详"}])])
        result = probe.parse_summary(probe.projected_html({"body": probe.EXPECTED_KICKOFF, "rows": rows}))
        self.assertTrue(result["parse_ok"])

    def test_invalid_rolling_row_excluded_but_invalid_prematch_rejected(self):
        projection = {"rows": [row(), row(home="封", status="滚", when="10-13 03:01")]}
        result = probe.parse_projected_history(projection)
        self.assertTrue(result["parse_ok"])
        self.assertEqual(result["prematch_row_count"], 1)
        self.assertEqual(result["visible_rolling_rows_excluded"], 1)
        self.assertFalse(result["complete_tick_history_certified"])
        projection["rows"].append(row(home="封", status="即"))
        self.assertFalse(probe.parse_projected_history(projection)["parse_ok"])

    def test_empty_or_incomplete_history_is_not_data_success(self):
        self.assertFalse(probe.compose_result({})["diagnostic_pass"])
        self.assertFalse(probe.compose_result({"summary": {"parse_ok": True}, "history": {"parse_ok": True, "prematch_row_count": 61}})["diagnostic_pass"])

    def test_denial_does_not_evaluate_dom_or_retry(self):
        response = type("Response", (), {"status": 442})()
        page = type("Page", (), {"goto": AsyncMock(return_value=response), "close": AsyncMock(),
                                 "evaluate": AsyncMock(), "url": probe.SUMMARY_URL})()
        context = type("Context", (), {"new_page": AsyncMock(return_value=page)})()
        result = asyncio.run(probe.visit(context, probe.SUMMARY_URL, probe.parse_summary))
        self.assertEqual(result["stop_reason"], "explicit_restriction_http_442")
        self.assertEqual(page.goto.await_count, 1)
        page.evaluate.assert_not_awaited()
        page.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
