#!/usr/bin/env python3
"""C29 synthetic, serial, in-memory reference. No network or external actions."""
import argparse
import copy
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Practice:
    def __init__(self, dataset, ttl):
        self.sessions = copy.deepcopy(dataset['sessions'])
        self.registrations = copy.deepcopy(dataset['registrations'])
        self.waitlists = copy.deepcopy(dataset['waitlists'])
        self.offers = {}
        self.seen = {}
        self.ttl = ttl
        self.counters = {sid: 0 for sid in self.sessions}
        self.check()

    def snapshot(self):
        return copy.deepcopy({'registrations': self.registrations,
                              'waitlists': self.waitlists, 'offers': self.offers})

    def check(self):
        for sid, session in self.sessions.items():
            booked = [r for r in self.registrations.values()
                      if r['session_id'] == sid and r['status'] == 'booked']
            held = [o for o in self.offers.values()
                    if o['session_id'] == sid and o['status'] in ('draft', 'invited')]
            assert len(booked) + len(held) <= session['capacity'], 'over_capacity'
            people = [r['person_id'] for r in booked] + [o['person_id'] for o in held]
            assert len(people) == len(set(people)), 'same_person_twice_in_one_session'

    def next_draft(self, sid):
        occupied = sum(r['session_id'] == sid and r['status'] == 'booked'
                       for r in self.registrations.values())
        occupied += sum(o['session_id'] == sid and o['status'] in ('draft', 'invited')
                        for o in self.offers.values())
        if occupied >= self.sessions[sid]['capacity'] or not self.waitlists[sid]:
            return None
        person = self.waitlists[sid].pop(0)
        self.counters[sid] += 1
        oid = f"OFFER-{sid}-{self.counters[sid]}"
        self.offers[oid] = {'session_id': sid, 'person_id': person,
                            'status': 'draft', 'expires_at': None}
        return oid

    def apply(self, event):
        eid = event['event_id']
        payload = json.dumps({k: v for k, v in event.items() if k != 'event_id'},
                             sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        if eid in self.seen:
            old_payload, old_result = self.seen[eid]
            if payload != old_payload:
                return {'status': 'id_conflict'}
            return {'status': 'duplicate', 'original_result': copy.deepcopy(old_result)}
        result = self.dispatch(event)
        self.check()
        self.seen[eid] = (payload, copy.deepcopy(result))
        return result

    def dispatch(self, e):
        kind = e['action']
        if kind == 'cancel':
            if not e.get('session_id') or not e.get('registration_id'):
                return {'status': 'needs_clarification'}
            if e.get('actor') != 'ORG' or not e.get('organizer_approved'):
                return {'status': 'needs_organizer_confirmation'}
            r = self.registrations.get(e['registration_id'])
            if not r or r['session_id'] != e['session_id']:
                return {'status': 'registration_mismatch'}
            if r['status'] != 'booked':
                return {'status': 'already_cancelled'}
            r['status'] = 'cancelled'
            oid = self.next_draft(e['session_id'])
            return {'status': 'cancelled_draft_created' if oid else 'cancelled_no_waiter',
                    'offer_id': oid}
        offer = self.offers.get(e.get('offer_id'))
        if offer is None:
            return {'status': 'offer_unknown'}
        if kind == 'confirm_offer':
            if e.get('actor') != 'ORG':
                return {'status': 'not_organizer'}
            if offer['status'] != 'draft':
                return {'status': 'not_draft'}
            offer['status'] = 'invited'
            offer['expires_at'] = e['now'] + self.ttl
            return {'status': 'invited', 'expires_at': offer['expires_at']}
        if kind == 'expire':
            if e.get('actor') != 'CLOCK':
                return {'status': 'not_clock'}
            if offer['status'] != 'invited':
                return {'status': 'not_invited'}
            if e['now'] < offer['expires_at']:
                return {'status': 'not_due'}
            offer['status'] = 'expired'
            oid = self.next_draft(offer['session_id'])
            return {'status': 'expired_next_draft' if oid else 'expired_no_waiter',
                    'offer_id': oid}
        if kind == 'accept_offer':
            if e.get('actor') != offer['person_id']:
                return {'status': 'wrong_person'}
            if offer['status'] == 'expired':
                return {'status': 'rejected_expired'}
            if offer['status'] != 'invited':
                return {'status': 'not_invited'}
            if e['now'] >= offer['expires_at']:
                # Preserve the due offer for CLOCK to expire and advance the queue.
                return {'status': 'rejected_expired'}
            offer['status'] = 'accepted'
            rid = f"REG-{e['offer_id']}"
            self.registrations[rid] = {'session_id': offer['session_id'],
                                       'person_id': offer['person_id'], 'status': 'booked'}
            return {'status': 'booked', 'registration_id': rid}
        return {'status': 'unknown_action'}


def run(dataset, cases, ttl):
    app = Practice(dataset, ttl)
    trace = []
    no_change_statuses = {'duplicate', 'needs_clarification', 'id_conflict',
                         'rejected_expired', 'registration_mismatch'}
    for case in cases:
        before = app.snapshot()
        result = app.apply(case['event'])
        assert result['status'] == case['expected_status'], (case['id'], result)
        if result['status'] in no_change_statuses:
            assert app.snapshot() == before, (case['id'], 'unexpected_mutation')
        if case['id'] == 'S1_cancel':
            assert app.registrations['R21']['status'] == 'booked', 'S2_must_survive'
        trace.append({'case': case['id'], 'result': result})
    return trace, app


def boundary_check(dataset, ttl):
    app = Practice(dataset, ttl)
    app.apply({'event_id': 'BC1', 'action': 'cancel', 'session_id': 'S2',
               'registration_id': 'R21', 'actor': 'ORG', 'organizer_approved': True, 'now': 3})
    app.apply({'event_id': 'BC2', 'action': 'confirm_offer', 'offer_id': 'OFFER-S2-1',
               'actor': 'ORG', 'now': 4})
    result = app.apply({'event_id': 'BC3', 'action': 'expire', 'offer_id': 'OFFER-S2-1',
                        'actor': 'CLOCK', 'now': 15})
    expected = 'expired_next_draft' if 15 >= 4 + ttl else 'not_due'
    assert result['status'] == expected
    return {'ttl_minutes': ttl, 'confirmed_at': 4, 'expires_at': 4 + ttl,
            'checked_at': 15, 'result': result['status']}


def extra_checks(dataset, ttl):
    app = Practice(dataset, ttl)
    wrong = app.apply({'event_id': 'W1', 'action': 'cancel', 'session_id': 'S2',
                      'registration_id': 'R11', 'actor': 'ORG',
                      'organizer_approved': True, 'now': 0})
    assert wrong['status'] == 'registration_mismatch'
    cancel = {'event_id': 'X1', 'action': 'cancel', 'session_id': 'S1',
              'registration_id': 'R11', 'actor': 'ORG', 'organizer_approved': True, 'now': 0}
    app.apply(cancel)
    app.apply({'event_id': 'X2', 'action': 'confirm_offer', 'offer_id': 'OFFER-S1-1',
               'actor': 'ORG', 'now': 1})
    at_boundary = app.apply({'event_id': 'X3', 'action': 'accept_offer',
                            'offer_id': 'OFFER-S1-1', 'actor': 'P2', 'now': 1 + ttl})
    assert at_boundary['status'] == 'rejected_expired'
    app.check()
    # Deliberately wrong lookup demonstrates the person/registration confusion.
    wrong_ids = [rid for rid, r in dataset['registrations'].items() if r['person_id'] == 'P1']
    assert wrong_ids == ['R11', 'R21']
    return {'session_registration_mismatch': 'rejected', 'accept_at_expiry': 'rejected',
            'person_only_bad_lookup_would_affect': wrong_ids,
            'correct_S1_cancel_affected': ['R11']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ttl', type=int, default=None, help='invitation minutes, 1 to 15 for supplied cases')
    args = parser.parse_args()
    config = json.loads((HERE / 'config.json').read_text())
    ttl = config['invite_ttl_minutes'] if args.ttl is None else args.ttl
    if not 1 <= ttl <= 15:
        parser.error('本练习固定样本仅支持1—15分钟；其他范围需先修改样本事件时间与预期')
    dataset = json.loads((HERE / 'dataset.json').read_text())
    cases = json.loads((HERE / 'cases.json').read_text())
    trace, app = run(dataset, cases, ttl)
    result = {'kind': 'synthetic_serial_reference_execution', 'network_calls': 0,
              'external_actions': 0, 'case_count': len(trace), 'trace': trace,
              'handover_comparison': [boundary_check(dataset, 15), boundary_check(dataset, 10)],
              'extra_checks': extra_checks(dataset, ttl), 'final_state': app.snapshot(),
              'scope': '合成内存状态与预期一致；未测试持久化、并发、模型或真实用户'}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
