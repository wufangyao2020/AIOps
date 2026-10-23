#!/usr/bin/env python3
"""Offline notification policy. Publication receipts (Issue bodies) are the baseline.

Raw observation fingerprints remain audit IDs, never notification identities.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys

LEVEL = {'NONE': 0, 'P3': 1, 'P2': 2, 'P1': 3}
# Measured against the last successfully notified value, so small moves accumulate.
MATERIAL_RELATIVE_MOVE = 0.05


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()[:20]


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def event_identity(event):
    # Stable identity survives refetch/rolling standing-negative dates. Content
    # revisions on the same document are still new evidence.
    return digest([event.get(k) for k in
                   ('event_key', 'title', 'summary', 'polarity', 'level', 'source_url')])


def unverified_page_change(sid, event):
    # Narrow exception for this collector's whole-page hash heuristic only.
    # A P1 must always pass through, even for this provider.
    return (sid == 'usda_wasde' and event.get('level') != 'P1' and
            event.get('title') in {'USDA WASDE页面出现更新', 'USDA WASDE监控建立基线'}
            and not event.get('confirmed_material'))


def snapshot(latest, previous=None):
    signals = {}
    candidates = []
    for sid, result in sorted(latest.get('signal_results', {}).items()):
        events, uncertain = {}, []
        for event in result.get('events', []):
            if LEVEL.get(event.get('level'), 0) < 2:
                continue
            if unverified_page_change(sid, event):
                uncertain.append(event.get('event_key'))
            else:
                events[event_identity(event)] = {k: event.get(k) for k in
                                                ('level', 'event_key', 'title', 'source_url')}
        level = result.get('level', 'NONE')
        if result.get('type') == 'event':
            level = max((e['level'] for e in events.values()), key=lambda x: LEVEL[x], default='NONE')
        signals[sid] = {
            'level': level, 'trend': result.get('trend_sign', 0),
            'value': result.get('latest') if finite(result.get('latest')) else None,
            'hits': sorted({(h['metric'], h.get('level', 'NONE'))
                            for h in result.get('hits', [])}),
            'events': events,
        }
        candidates.extend(f'{sid}:{key}' for key in uncertain)
    failed = {e['signal_id'] for e in latest.get('errors', [])}
    old_signals = (previous or {}).get('signals', {})
    for sid, old in old_signals.items():
        if sid in failed:
            signals[sid] = old
    for sid, now in signals.items():
        old = old_signals.get(sid, {})
        now['episode'] = (old.get('episode') if LEVEL.get(old.get('level'), 0) >= 2
                          else latest['generated_at']) if LEVEL.get(now['level'], 0) >= 2 else None
    # JSON round-trip canonicalizes hit tuples, including the in-process path.
    return json.loads(json.dumps({
        'version': 1, 'generated_at': latest['generated_at'], 'signals': signals,
        'failed_sources': sorted({e['signal_id'] for e in latest.get('errors', [])}),
        'unverified_events': sorted(candidates),
    }))


def decide(current, published=None, observed=None):
    """Compare against a successful publication, with last run for re-arming.

    Missing/failed sources never imply recovery. Health changes are a separate
    channel, recorded in Actions warnings/summary, not industry P1/P2 Issues.
    """
    if published is not None and published.get('version') != 1:
        raise ValueError('Unsupported publication baseline version')
    prior = (published or {}).get('signals', {})
    failed = set(current.get('failed_sources', []))
    reasons, ordinary = [], []
    for sid, now in current['signals'].items():
        if LEVEL.get(now['level'], 0) < 2:
            continue
        old = prior.get(sid)
        kind = None
        if not old or LEVEL.get(old['level'], 0) < 2:
            kind = 'first_trigger'
        elif now.get('episode') and old.get('episode') != now['episode']:
            kind = 'retrigger'
        elif LEVEL[now['level']] > LEVEL[old['level']]:
            kind = 'risk_upgrade'
        elif any(k not in old['events'] or LEVEL[v['level']] > LEVEL.get(old['events'][k]['level'], 0)
                 for k, v in now['events'].items()):
            kind = 'important_new_event'
        elif now['trend'] != old['trend']:
            kind = 'direction_change'
        if kind is None and old:
            old_hits = {m: LEVEL.get(l, 0) for m, l in old['hits']}
            if any(LEVEL.get(l, 0) > old_hits.get(m, 0) for m, l in now['hits']):
                kind = 'new_threshold'
            elif finite(now['value']) and finite(old['value']):
                change = abs(now['value'] - old['value'])
                if change and change >= max(abs(old['value']) * MATERIAL_RELATIVE_MOVE, 1e-12):
                    kind = 'material_numeric_change'
                elif change:
                    ordinary.append(sid)
        if kind:
            reason = {'signal_id': sid, 'kind': kind, 'level': now['level']}
            if kind in {'first_trigger', 'important_new_event', 'risk_upgrade'} and now['events']:
                reason['events'] = [e for k, e in now['events'].items()
                                    if not old or k not in old['events']]
            if kind == 'retrigger':
                reason['occurrence'] = now['episode']
            reasons.append(reason)
    health_before = set((observed or published or {}).get('failed_sources', []))
    health = {'failed': sorted(failed), 'new_failures': sorted(failed - health_before),
              'recovered': sorted(health_before - failed)}
    baseline = dict(current)
    # Keep last known state through missing observations, without treating a
    # source outage as a new industry activation when it returns.
    baseline['signals'] = {**{s: v for s, v in prior.items() if s in failed},
                           **current['signals']}
    for sid, now in baseline['signals'].items():
        now = dict(now)
        now['events'] = {**prior.get(sid, {}).get('events', {}), **now['events']}
        baseline['signals'][sid] = now
    return {
        'issue_required': bool(reasons), 'reasons': reasons,
        'classification': 'material_increment' if reasons else
                          'ordinary_numeric_change' if ordinary else 'stable_duplicate',
        'ordinary_numeric_signals': ordinary, 'health': health,
        'unverified_events': current.get('unverified_events', []),
        'notification_key': digest([published, reasons, current['signals']]) if reasons else None,
        'publication_snapshot': baseline,
    }


if __name__ == '__main__':
    payload = json.load(sys.stdin)
    print(json.dumps(decide(payload['current'], payload.get('published'),
                            payload.get('observed')), ensure_ascii=False))
