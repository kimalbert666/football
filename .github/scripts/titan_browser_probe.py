"""One-shot, read-only Chromium diagnostic of two fixed public Titan pages.

Default Playwright Chromium launch/context: no browser identity modification,
proxy, imported cookies, authentication, CAPTCHA solving, TLS relaxation, or
access-control workarounds. A denial ends the diagnostic immediately. Only a
limited parsed result is retained; the rendered DOM is never written to disk.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import re

from titan_cloud_probe import SUMMARY_URL, DETAIL_URL, EXPECTED_KICKOFF, parse_summary, parse_history

PAGE_BUDGET_SECONDS = 45
TOTAL_BUDGET_SECONDS = 125
BLOCK_STATUSES = {401, 403, 407, 429, 442, 451}
BLOCK_PATTERNS = (
    r"非法访问", r"行为已被WAF", r"访问被拒绝", r"访问受限", r"安全验证",
    r"请.{0,8}完成.{0,8}验证", r"人机验证", r"验证码验证", r"access denied",
    r"verify (?:that )?you are human", r"checking your browser", r"captcha",
    r"too many requests", r"request (?:was )?blocked", r"just a moment",
)

# This is a bounded rendering-derived projection, not page.content(), network
# interception, hidden application state, or script evaluation. Hidden cells
# remain empty placeholders so initial columns cannot shift to closing columns.
DOM_PROJECTION = r"""() => {
  const shown = el => {
    if (!el || !el.getClientRects().length) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && s.visibility !== 'collapse';
  };
  const allRows = Array.from(document.querySelectorAll('tr')).filter(shown);
  const rows = allRows.slice(0, 2000).map(row =>
    Array.from(row.children).filter(c => c.tagName === 'TD').map(cell => {
      if (!shown(cell)) return {visible: false};
      const nested = Array.from(cell.querySelectorAll('[title]')).filter(shown);
      const links = Array.from(cell.querySelectorAll('a[href]')).filter(shown);
      return {visible: true, text: (cell.innerText || '').slice(0, 500),
        titles: [cell, ...nested].map(x => x.getAttribute('title')).filter(Boolean).slice(0, 8),
        links: links.slice(0, 8).map(a => ({href: a.href, text: (a.innerText || '').slice(0, 60)}))};
    }));
  return {title: document.title.slice(0, 200), body: (document.body?.innerText || '').slice(0, 25000),
    row_count: allRows.length, rows,
    visible_challenge: Array.from(document.querySelectorAll('iframe')).some(el =>
      shown(el) && /captcha|challenge/i.test((el.title || '') + ' ' + (el.getAttribute('src') || '')))};
}"""


def restriction_reason(status, projection):
    if status in BLOCK_STATUSES:
        return "explicit_restriction_http_" + str(status)
    if projection.get("visible_challenge"):
        return "visible_challenge"
    text = projection.get("title", "") + "\n" + projection.get("body", "")
    if any(re.search(pattern, text, re.I) for pattern in BLOCK_PATTERNS):
        return "visible_access_restriction_or_challenge"
    return None


def projected_html(projection):
    """Convert visible text/tooltips/links to a minimal parser input in memory."""
    out = ["<title>" + escape(projection.get("title", "")) + "</title>",
           "<div>" + escape(projection.get("body", "")) + "</div><table>"]
    for row in projection.get("rows", []):
        out.append("<tr>")
        for cell in row:
            if not cell.get("visible"):
                out.append("<td hidden></td>")
                continue
            out.append("<td>" + escape(cell.get("text", "")))
            for title in cell.get("titles", []):
                out.append('<span title="' + escape(title, quote=True) + '"></span>')
            for link in cell.get("links", []):
                out.append('<a href="' + escape(link.get("href", ""), quote=True) + '">' +
                           escape(link.get("text", "")) + '</a>')
            out.append("</td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def parse_projected_history(projection):
    # Only visible prematch rows enter the historical diagnostic. Suspended or
    # malformed rolling prices are unrelated to the required prematch evidence.
    rows, rolling = [], 0
    for row in projection.get("rows", []):
        if len(row) != 7 or not all(cell.get("visible") for cell in row):
            continue
        status = row[6].get("text", "").strip()
        if status in {"滚", "滾"}:
            rolling += 1
        elif status in {"早", "即"}:
            rows.append(row)
    prepared = {**projection, "rows": rows}
    result = parse_history(projected_html(prepared))
    result["visible_prematch_rows_examined"] = len(rows)
    result["visible_rolling_rows_excluded"] = rolling
    times = [row[5].get("text", "").strip() for row in rows]
    result["same_minute_history_rows_present"] = len(times) != len(set(times))
    result["complete_tick_history_certified"] = False
    return result


async def visit(context, url, parser):
    result = {"url": url, "transport": "default_playwright_chromium",
              "navigation_attempts": 1, "transport_ok": False, "parse_ok": False,
              "checked_at_utc": datetime.now(timezone.utc).isoformat()}
    page = await context.new_page()
    try:
        # Exactly one top-level navigation. The whole page budget includes DOM
        # rendering and projection; there are no retries or alternate endpoints.
        response = await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        status = response.status if response else None
        result["http_status"] = status
        result["final_url"] = page.url
        if status in BLOCK_STATUSES:
            result["stop_reason"] = restriction_reason(status, {})
            return result
        projection = await page.evaluate(DOM_PROJECTION)
        result["page_title"] = projection.get("title")
        reason = restriction_reason(status, projection)
        if reason:
            result["stop_reason"] = reason
            return result
        if page.url.split("?", 1)[0] != url.split("?", 1)[0]:
            result["stop_reason"] = "unexpected_navigation_destination"
            return result
        result["transport_ok"] = status == 200
        result["visible_row_count"] = projection.get("row_count", 0)
        if projection.get("row_count", 0) > 2000:
            result["error"] = "visible_row_projection_limit_exceeded"
        elif result["transport_ok"]:
            result.update(parse_projected_history(projection) if url == DETAIL_URL else parser(projected_html(projection)))
        return result
    finally:
        await page.close()


def compose_result(pages):
    summary, history = pages.get("summary", {}), pages.get("history", {})
    crow = next((x for x in summary.get("companies", []) if x["company_id"] == "3"), None)
    first = history.get("first_opening")
    agree = bool(crow and first and all(crow["opening"][key] == first[key] for key in
                 ("home_profit_only_odds", "home_handicap", "away_profit_only_odds")) and
                 crow["opening"]["displayed_full_datetime"] == first["fixture_year_inferred_datetime"])
    expected_count = history.get("prematch_row_count") == 61
    return {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "one_shot_retrospective_browser_connectivity_diagnostic",
        "match_id": "3003902", "fixture": "Coventry-Newcastle",
        "fixture_kickoff_displayed": EXPECTED_KICKOFF,
        "displayed_timezone": "not_independently_confirmed",
        "formal_observation": False, "candidate_training_input": False,
        "source_declared_opening_only": True,
        "opening_summary_history_agree": agree,
        "expected_61_prematch_rows_found": expected_count,
        "diagnostic_pass": bool(summary.get("parse_ok") and history.get("parse_ok") and agree and expected_count),
        "pages": pages,
        "limits": ["Single historical case; this does not establish stable cloud access or EPL coverage.",
                   "Source-reported opening is not independently certified bookmaker-first opening.",
                   "61 visible rows do not prove the provider recorded every bookmaker price change.",
                   "Retrospective history is not a prospective pre-kickoff capture.",
                   "Workflow success means diagnostic completed; inspect diagnostic_pass for data success."]}


async def diagnose(pages):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        async with await playwright.chromium.launch() as browser:
            context = await browser.new_context()
            for name, url, parser in (("summary", SUMMARY_URL, parse_summary), ("history", DETAIL_URL, parse_history)):
                try:
                    pages[name] = await asyncio.wait_for(visit(context, url, parser), PAGE_BUDGET_SECONDS)
                except Exception as exc:
                    # Exception text can contain environment URLs; retain class only.
                    pages[name] = {"url": url, "parse_ok": False, "transport_ok": False,
                                   "error_type": type(exc).__name__, "error": "navigation_or_parse_failed"}
                if pages[name].get("stop_reason"):
                    break
            await context.close()


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--output", type=Path, required=True)
    options = args.parse_args()
    pages = {}
    try:
        asyncio.run(asyncio.wait_for(diagnose(pages), TOTAL_BUDGET_SECONDS))
    except Exception as exc:
        pages["diagnostic_error"] = {"error_type": type(exc).__name__}
    result = compose_result(pages)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0  # Preserve source failure evidence without fault-notification spam.


if __name__ == "__main__":
    raise SystemExit(main())
