from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from titan_daily import HK, parse_schedule, run, write_json


def resolve(names, aliases):
    return {'甲':'a','乙':'b'}.get(names[0])


def source_row(day, event='42', link_event='42', league='36'):
    return (f'<table><tr sid="{event}" name="{league},1"><td>英超</td><td>{day:%m-%d} 22:00</td>'
            '<td></td><td><span name="order">[1]</span>甲</td><td>-</td><td>乙<span name="order">[8]</span></td>'
            '<td></td><td></td><td></td><td><a onclick="AsianOdds('+link_event+')">亚</a></td></tr></table>')


class DailyTests(unittest.TestCase):
    def test_source_identity_and_rank_badges(self):
        day=datetime(2026,10,17).date()
        values=parse_schedule(source_row(day), day, {}, resolve)
        self.assertEqual(values[0]['home'], '甲')
        self.assertEqual(values[0]['away'], '乙')
        self.assertEqual(values[0]['kickoff_at'], '2026-10-17T14:00:00+00:00')
        for kwargs in ({'link_event':'43'},{'league':'8'}):
            with self.assertRaises(ValueError):
                parse_schedule(source_row(day, **kwargs), day, {}, resolve)

    def test_wrong_schedule_date_is_rejected(self):
        day=datetime(2026,10,17).date()
        with self.assertRaises(ValueError):
            parse_schedule(source_row(day), day+timedelta(days=1), {}, resolve)

    def test_next_morning_and_new_year_keep_actual_displayed_date(self):
        for day in (datetime(2026,10,31).date(), datetime(2026,12,31).date()):
            tomorrow=day+timedelta(days=1)
            value=parse_schedule(source_row(tomorrow).replace('22:00','01:30'), day, {}, resolve)
            expected=datetime.combine(tomorrow,datetime.min.time()).replace(hour=1,minute=30,tzinfo=HK)
            self.assertEqual(value[0]['kickoff_at'],expected.astimezone(timezone.utc).isoformat())

    def test_immutable_records_refuse_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.json'
            write_json(p, {'a':1}, immutable=True)
            write_json(p, {'a':1}, immutable=True)
            with self.assertRaises(ValueError): write_json(p, {'a':2}, immutable=True)

    def test_daily_persists_success_and_missing_coverage(self):
        now=datetime.now(timezone.utc); day=(now.astimezone(HK)+timedelta(days=1)).date()
        ko=datetime.combine(day,datetime.min.time()).replace(hour=22,tzinfo=HK).astimezone(timezone.utc)
        sources=types.SimpleNamespace(_aliases=lambda root:{},_resolve=resolve,
            _instant=lambda s:datetime.fromisoformat(s.replace('Z','+00:00')))
        async def fake_browser(targets, batch):
            for t in targets:
                batch['fixtures'].append({'fixture':t,'status':'displayed_history_reconciled',
                                           'histories':{'3':{'prematch_quote_count':7}}})
        def schedule_reply(url, **kwargs):
            # Only the preceding sporting-day page contains this early fixture.
            page_day=datetime.strptime(url.rsplit('_',1)[1].split('.')[0],'%Y%m%d').date()
            text=source_row(day) if page_day==day-timedelta(days=1) else '<table></table>'
            return types.SimpleNamespace(status_code=200,content=text.encode('gb18030'))
        with tempfile.TemporaryDirectory() as tmp, patch.dict('sys.modules',{'f4_sources':sources}), \
             patch('titan_daily.requests.get',side_effect=schedule_reply), patch('titan_daily.browser_run',new=fake_browser):
            root=Path(tmp)
            fixture={'match_id':'espn:1','source_event_id':'1','provider':'espn','league':'england-premier',
                     'home_id':'a','away_id':'b','kickoff_at':ko.isoformat(),'status':'scheduled'}
            write_json(root/'data/f4/fixtures/1.json',fixture)
            write_json(root/'data/f4/fixtures/2.json',{**fixture,'match_id':'espn:2','source_event_id':'2',
                                                   'home_id':'c','away_id':'d'})
            result=run(root)
            self.assertEqual(result['mapping']['counts']['expected'],2)
            self.assertEqual(result['mapping']['counts']['missing'],1)
            self.assertEqual(result['reconciled'],1)
            paths=list((root/'data/f4/titan-history/observations').rglob('*.json'))
            self.assertEqual(len(paths),1)
            archive=json.loads(paths[0].read_text(encoding='utf-8'))
            self.assertIn('content_sha256',archive['fixture']['mapping_source_evidence'])
            again=run(root)
            self.assertEqual(again['planned'],0)
            self.assertEqual(len(list((root/'data/f4/titan-history/observations').rglob('*.json'))),1)


if __name__=='__main__': unittest.main()
