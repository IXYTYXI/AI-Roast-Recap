"""Phase-two integration checks using only temporary ledgers and synthetic text."""
from contextlib import closing
import importlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import scold


class PhaseTwoIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.native = self.root / 'native'
        self.native.mkdir()
        scold.initialize(self.data, 'codex', self.native, since='2026-10-01T00:00:00Z')

    def reasons(self):
        self.assertTrue((SCRIPTS / 'reasons.py').exists(), 'Phase-two reason storage is missing')
        return importlib.import_module('reasons')

    def confirmed(self, ident='m1', occurred_at='2026-10-15T07:00:00Z'):
        item = {'platform':'codex','conversation_id':'thread','message_id':ident,
                'occurred_at':occurred_at,'text':'条件没解释清楚，你到底在干什么',
                'source_ref':'synthetic/source#' + ident}
        scold.ingest_messages(self.data, [item], {'complete':True,'warnings':[], 'scanned_conversations':1}, '2026-11-01T00:00:00Z')
        event_id = scold.event_id_for('codex', 'thread', ident)
        scold.review(self.data, [{'event_id':event_id,'status':'scolding','note':'明确直接斥责'}])
        return event_id

    def analysis(self, event_id):
        context = self.reasons().context_for(self.data, event_id)
        return {'event_id':event_id,'context_id':context['context_id'],
                'category':'unclear_communication','basis':'user_stated',
                'summary':'用户明确表达说明不清楚的不满。',
                'evidence':[{'message_id':context['event']['message_id'],'quote':'条件没解释清楚'}]}

    def cli(self, command, *args):
        return subprocess.run([sys.executable,str(SCRIPTS/'scold.py'),command,
                               '--data-dir',str(self.data),*args],capture_output=True,text=True)

    def test_initialize_adds_phase_two_tables(self):
        with closing(sqlite3.connect(self.data/'ledger.sqlite')) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'reason_contexts','reason_reviews'} <= tables)

    def test_upgrade_adds_tables_without_resetting_legacy_events_or_config(self):
        ident=self.confirmed()
        config=(self.data/'config.json').read_bytes()
        with closing(sqlite3.connect(self.data/'ledger.sqlite')) as db, db:
            event_before=db.execute('SELECT * FROM events WHERE event_id=?',(ident,)).fetchone()
            for table in ['reason_reviews','reason_contexts']:
                db.execute('DROP TABLE IF EXISTS ' + table)
        result=scold.initialize(self.data,'codex',self.native)
        self.assertFalse(result['created'])
        self.assertEqual(config,(self.data/'config.json').read_bytes())
        with closing(sqlite3.connect(self.data/'ledger.sqlite')) as db:
            self.assertEqual(event_before,db.execute('SELECT * FROM events WHERE event_id=?',(ident,)).fetchone())
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'reason_contexts','reason_reviews'} <= tables)
        self.assertEqual(1,self.reasons().pending(self.data)['total_pending'])

    def test_reclassification_invalidates_reason_and_reopening_requires_analysis(self):
        ident=self.confirmed()
        self.reasons().review(self.data,[self.analysis(ident)])
        before=scold.report(self.data,'day','2026-10-15')
        self.assertEqual('user_stated',before['scoldings'][0]['reason']['basis'])
        # A later invalid item must roll back both the classification and reason deletion.
        with self.assertRaises(scold.ScoldError):
            scold.review(self.data,[{'event_id':ident,'status':'not_scolding','note':'引用'},
                                    {'event_id':'missing','status':'not_scolding','note':'不存在'}])
        self.assertEqual(before['scoldings'][0]['reason'],scold.report(self.data,'day','2026-10-15')['scoldings'][0]['reason'])
        scold.review(self.data,[{'event_id':ident,'status':'not_scolding','note':'引用'}])
        self.assertEqual(0,scold.report(self.data,'day','2026-10-15')['reason_summary']['analyzed_count'])
        scold.review(self.data,[{'event_id':ident,'status':'scolding','note':'重新确认'}])
        after=scold.report(self.data,'day','2026-10-15')
        self.assertIsNone(after['scoldings'][0]['reason'])
        self.assertEqual(1,after['reason_summary']['pending_count'])

    def test_cli_context_reason_review_and_report_round_trip(self):
        ident=self.confirmed()
        pending=self.cli('reason-pending','--limit','5')
        self.assertEqual(0,pending.returncode,pending.stderr)
        self.assertEqual(1,json.loads(pending.stdout)['total_pending'])
        context=self.cli('context','--event-id',ident)
        self.assertEqual(0,context.returncode,context.stderr)
        data=json.loads(context.stdout)
        self.assertFalse(data['target_found'])
        self.assertFalse(data['coverage']['complete'])
        decision=self.analysis(ident)
        path=self.root/'reason-review.json'
        path.write_text(json.dumps({'analyses':[decision]}))
        review=self.cli('reason-review','--input',str(path))
        self.assertEqual(0,review.returncode,review.stderr)
        self.assertEqual(1,json.loads(review.stdout)['updated'])
        report=self.cli('report','--period','day','--date','2026-10-15')
        self.assertEqual(0,report.returncode,report.stderr)
        out=json.loads(report.stdout)
        self.assertEqual(1,out['reason_summary']['categories']['unclear_communication'])
        self.assertTrue(out['scoldings'][0]['reason']['context_coverage']['warnings'])

    def test_reason_counts_follow_existing_period_boundaries(self):
        old=self.confirmed('old','2026-10-14T07:00:00Z')
        today=self.confirmed('today')
        self.reasons().review(self.data,[self.analysis(old),self.analysis(today)])
        self.assertEqual(1,scold.report(self.data,'day','2026-10-15')['reason_summary']['analyzed_count'])
        self.assertEqual(2,scold.report(self.data,'month','2026-10-15')['reason_summary']['analyzed_count'])

    def test_cli_rejects_forged_quote_with_actionable_json_error(self):
        ident=self.confirmed()
        decision=self.analysis(ident)
        decision['evidence'][0]['quote']='this quote was never in the source'
        path=self.root/'forged.json'
        path.write_text(json.dumps({'analyses':[decision]}))
        result=self.cli('reason-review','--input',str(path))
        self.assertNotEqual(0,result.returncode)
        error=json.loads(result.stderr)['error']
        self.assertIn('quote',error.lower())
        self.assertNotIn('Traceback',result.stderr)


if __name__=='__main__':
    unittest.main()
