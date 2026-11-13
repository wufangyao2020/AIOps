import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from global_signal_alert_policy import decide, snapshot

FIXTURES = Path(__file__).parent / 'fixtures/global_radar'


def numeric(level='P2', value=100):
    return {'type': 'numeric', 'level': level, 'latest': value,
            'trend_sign': 1, 'hits': [{'metric': 'five_day', 'level': level}]}


def event(key='doc1', level='P1', title='Confirmed filing'):
    return {'event_key': key, 'title': title, 'level': level, 'polarity': -1,
            'summary': 'Substantive source evidence', 'source_url': 'https://example.org/doc'}


def latest(results=None, time='2026-10-14T04:15:00+08:00', errors=None):
    return {'generated_at': time, 'signal_results': results or {'a': numeric()},
            'errors': errors or [], 'stock_results': [{'security_code': '000001'}]}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.data = latest()
        self.old = snapshot(self.data)

    def current(self, data):
        return snapshot(data, self.old)

    def test_first_trigger(self):
        self.assertEqual(decide(self.old)['reasons'][0]['kind'], 'first_trigger')

    def test_stable_same_stocks_cross_day(self):
        self.data['generated_at'] = '2026-10-15T06:35:00+08:00'
        self.assertFalse(decide(self.current(self.data), self.old)['issue_required'])

    def test_small_move_and_cumulative_material_move(self):
        self.data['signal_results']['a']['latest'] = 101
        observed = self.current(self.data)
        self.assertEqual(decide(observed, self.old)['classification'], 'ordinary_numeric_change')
        self.data['signal_results']['a']['latest'] = 106
        result = decide(snapshot(self.data, observed), self.old)
        self.assertEqual(result['reasons'][0]['kind'], 'material_numeric_change')

    def test_one_source_failure_and_recovery(self):
        self.data['errors'] = [{'signal_id': 'a'}]
        self.data['signal_results'] = {}
        failed = self.current(self.data)
        decision = decide(failed, self.old, self.old)
        self.assertFalse(decision['issue_required'])
        self.assertEqual(decision['health']['new_failures'], ['a'])
        self.assertFalse(decide(snapshot(latest(), failed), self.old, failed)['issue_required'])

    def test_health_only_ten_failures_no_industry_issue(self):
        d = latest(); d['signal_results'] = {}; d['errors'] = [{'signal_id': str(i)} for i in range(13)]
        self.assertFalse(decide(snapshot(d))['issue_required'])

    def test_upgrade_same_stocks(self):
        self.data['signal_results']['a'] = numeric('P1')
        self.assertEqual(decide(self.current(self.data), self.old)['reasons'][0]['kind'], 'risk_upgrade')

    def test_new_p1_event_on_existing_p1_same_stocks(self):
        d = latest({'e': {'type': 'event', 'level': 'P1', 'events': [event()]}})
        old = snapshot(d)
        d['signal_results']['e']['events'].append(event('doc2'))
        self.assertEqual(decide(snapshot(d, old), old)['reasons'][0]['kind'], 'important_new_event')

    def test_event_refetch_standing_negative_cross_day(self):
        d = latest({'e': {'type': 'event', 'level': 'P1', 'events': [event()]}})
        old = snapshot(d)
        d['generated_at'] = '2026-10-15T04:15:00+08:00'
        d['signal_results']['e']['events'][0].update(date='2026-10-15', fetched_at='new')
        self.assertFalse(decide(snapshot(d, old), old)['issue_required'])

    def test_page_hash_candidate_but_p1_bypasses(self):
        d = latest({'usda_wasde': {'type': 'event', 'level': 'P2', 'events': [
            event('hash', 'P2', 'USDA WASDE页面出现更新')]}})
        self.assertFalse(decide(snapshot(d))['issue_required'])
        d['signal_results']['usda_wasde']['events'][0]['level'] = 'P1'
        self.assertTrue(decide(snapshot(d))['issue_required'])

    def test_direction_and_threshold_changes(self):
        self.data['signal_results']['a']['trend_sign'] = -1
        self.assertEqual(decide(self.current(self.data), self.old)['reasons'][0]['kind'], 'direction_change')
        self.data['signal_results']['a']['trend_sign'] = 1
        self.data['signal_results']['a']['hits'].append({'metric': 'breakdown', 'level': 'P2'})
        self.assertEqual(decide(self.current(self.data), self.old)['reasons'][0]['kind'], 'new_threshold')

    def test_retrigger_survives_publication_failure(self):
        self.data['signal_results']['a'] = numeric('NONE')
        recovered = self.current(self.data)
        d = latest(time='2026-10-15T04:15:00+08:00')
        again = snapshot(d, recovered)
        decision = decide(again, self.old)
        self.assertEqual(decision['reasons'][0]['kind'], 'retrigger')
        # No successful receipt yet: the next run must retry the recurrence.
        next_run = snapshot(latest(time='2026-10-15T06:35:00+08:00'), again)
        self.assertTrue(decide(next_run, self.old)['issue_required'])
        self.assertFalse(decide(next_run, decision['publication_snapshot'])['issue_required'])

    def test_receipt_roundtrip_and_order_independence(self):
        receipt = json.loads(json.dumps(decide(self.old)['publication_snapshot']))
        self.assertFalse(decide(self.old, receipt)['issue_required'])
        self.assertEqual(self.old, snapshot(self.data))

    def test_new_p1_outside_top_ten_and_after_twenty_events(self):
        events = [event(str(i), 'P2') for i in range(21)]
        d = latest({'e': {'type': 'event', 'level': 'P2', 'events': events}})
        old = snapshot(d)
        d['signal_results']['e']['events'].append(event('new-p1', 'P1'))
        result = decide(snapshot(d, old), old)
        self.assertTrue(result['issue_required'])
        self.assertEqual(result['reasons'][0]['level'], 'P1')

    def test_confirmed_wasde_p2_not_suppressed(self):
        e = event('official-report', 'P2', 'USDA WASDE页面出现更新')
        e['confirmed_material'] = True
        d = latest({'usda_wasde': {'type': 'event', 'level': 'P2', 'events': [e]}})
        self.assertTrue(decide(snapshot(d))['issue_required'])

    def test_historical_replay_and_exact_fingerprint_cause(self):
        import hashlib
        a, b = [json.loads((FIXTURES / (c + '.json')).read_text()) for c in ('1e73587', '2a57eb6')]
        self.assertEqual(a['stock_results'], b['stock_results'])
        self.assertEqual(a['errors'], b['errors'])
        def raw(d):
            active = sorted([r for r in d['signal_results'].values() if r['level'] in ('P1', 'P2')],
                            key=lambda r: r['severity'], reverse=True)
            return [d['generated_at'][:10], [[r['signal_id'], r['level'], r['latest_date'], r['latest']] for r in active],
                    [e['signal_id'] for e in d['errors']]]
        def fp(obj):
            return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
        x, y = raw(a), raw(b)
        self.assertEqual(fp(x), 'b1655dbd025a5958421c')
        self.assertEqual(fp(y), '8eb744f815f5abe1f649')
        for row in x[1]:
            if row[0] == 'usda_wasde': row[2] = '2026-10-14'
        self.assertEqual(x, y)  # The only fingerprint input change.
        old = snapshot(a)
        result = decide(snapshot(b, old), old)
        self.assertFalse(result['issue_required'])
        self.assertTrue(result['unverified_events'])


if __name__ == '__main__':
    unittest.main()
