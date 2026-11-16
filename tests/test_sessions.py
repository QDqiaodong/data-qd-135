import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, '-m', 'retrieval_eval.cli']
RSS_LIMIT = 192 * 1024


def jsonl(records):
    return ''.join(json.dumps(r, ensure_ascii=False, separators=(',', ':')) + '\n' for r in records)


def cli(*args):
    return subprocess.run(CLI + list(args), capture_output=True, text=True)


def peak_rss(pid):
    try:
        with open('/proc/%d/status' % pid) as status:
            for line in status:
                if line.startswith('VmHWM'):
                    return int(line.split()[1])
    except OSError:
        return 0
    return 0


def cli_rss(*args):
    proc = subprocess.Popen(CLI + list(args), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    peak = 0
    while proc.poll() is None:
        peak = max(peak, peak_rss(proc.pid))
        time.sleep(0.005)
    peak = max(peak, peak_rss(proc.pid))
    stderr = proc.stderr.read().decode()
    proc.stderr.close()
    return proc.returncode, peak, stderr


def put(eid, user, at, kind, **payload):
    record = {'op': 'put', 'id': eid, 'user': user, 'at': at, 'kind': kind}
    record.update(payload)
    return record


def view(eid, user, at):
    return put(eid, user, at, 'view')


def search(eid, user, at, channel='paid'):
    return put(eid, user, at, 'search', channel=channel)


def purchase(eid, user, at, amount=100):
    return put(eid, user, at, 'purchase', amount_cents=amount)


def reference(events):
    """Small in-memory reference implementation of the complete contract."""
    by_user = {}
    for e in events:
        by_user.setdefault(e['user'], []).append(e)
    sessions, membership = {}, {}
    for user, rows in by_user.items():
        rows = sorted(rows, key=lambda e: (e['at'], e['id']))
        current = None
        previous = None
        for event in rows:
            if current is None or event['at'] - previous > 1800:
                current = {'id': event['id'], 'user': user, 'start': event['at'],
                           'end': event['at'], 'event_count': 0, 'day': event['at'] // 86400}
                sessions[current['id']] = current
            membership[event['id']] = current['id']
            current['end'] = event['at']
            current['event_count'] += 1
            previous = event['at']
    orders = []
    daily_sessions = {}
    daily_orders = {}
    for session in sessions.values():
        daily_sessions[session['day']] = daily_sessions.get(session['day'], 0) + 1
    for event in sorted((e for e in events if e['kind'] == 'purchase'), key=lambda e: e['id']):
        sid = membership[event['id']]
        session = sessions[sid]
        candidates = [e for e in by_user[session['user']]
                      if e['kind'] == 'search' and membership[e['id']] == sid
                      and (e['at'], e['id']) < (event['at'], event['id'])
                      and event['at'] - e['at'] <= 3600]
        chosen = max(candidates, key=lambda e: (e['at'], e['id']), default=None)
        order = {'id': event['id'], 'session_id': sid,
                 'search_id': None if chosen is None else chosen['id'],
                 'channel': None if chosen is None else chosen['channel'],
                 'amount_cents': event['amount_cents']}
        orders.append(order)
        key = (session['day'], order['channel'])
        count, amount = daily_orders.get(key, (0, 0))
        daily_orders[key] = (count + 1, amount + order['amount_cents'])
    session_rows = sorted(({k: s[k] for k in ('id', 'user', 'start', 'end', 'event_count')}
                           for s in sessions.values()), key=lambda s: (s['user'], s['start'], s['id']))
    return {
        'sessions': session_rows,
        'orders': sorted(orders, key=lambda o: o['id']),
        'daily_sessions': [{'day': d, 'session_count': daily_sessions[d]} for d in sorted(daily_sessions)],
        'daily_orders': [{'day': d, 'channel': c, 'order_count': daily_orders[(d, c)][0],
                          'amount_cents': daily_orders[(d, c)][1]}
                         for d, c in sorted(daily_orders, key=lambda x: (x[0], x[1] is not None, x[1] or ''))]
    }


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self.db = self.dir / 's.db'
        self.assertTrue(cli('session-init', '--db', self.db).returncode == 0)

    def tearDown(self):
        self.td.cleanup()

    def source(self, records, name='batch.jsonl'):
        path = self.dir / name
        path.write_text(jsonl(records), encoding='utf-8')
        return path

    def apply(self, records, expect=0):
        result = cli('session-apply', '--db', self.db, '--source', self.source(records))
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def report(self, name='report.json', expect=0):
        out = self.dir / name
        result = cli('session-report', '--db', self.db, '--output', out)
        self.assertEqual(result.returncode, expect, result.stderr)
        return json.loads(out.read_text(encoding='utf-8'))

    def events(self):
        con = sqlite3.connect('file:%s?mode=ro' % self.db, uri=True)
        try:
            rows = con.execute('SELECT id,user,at,kind,channel,amount_cents FROM events').fetchall()
        finally:
            con.close()
        result = []
        for eid, user, at, kind, channel, amount in rows:
            event = {'id': eid, 'user': user, 'at': at, 'kind': kind}
            if channel is not None:
                event['channel'] = channel
            if amount is not None:
                event['amount_cents'] = amount
            result.append(event)
        return result

    def assert_matches_reference(self, revision):
        got = self.report()
        expected = reference(self.events())
        self.assertEqual(got['revision'], revision)
        for key in expected:
            self.assertEqual(got[key], expected[key])
        return got


class SessionizationTests(SessionCase):
    def test_exact_1800_remains_connected(self):
        self.apply([view('a', 'u', 1000), view('b', 'u', 2800)])
        self.assert_matches_reference(1)
        self.assertEqual(self.report()['sessions'], [
            {'id': 'a', 'user': 'u', 'start': 1000, 'end': 2800, 'event_count': 2}])

    def test_gap_1801_splits_and_same_second_id_order_sets_session_id(self):
        self.apply([view('z', 'u', 1000), view('a', 'u', 2801), view('m', 'u', 2801)])
        report = self.report()
        self.assertEqual([s['id'] for s in report['sessions']], ['z', 'a'])
        self.assertEqual(report['sessions'][1]['event_count'], 2)
        self.assert_matches_reference(1)

    def test_continuous_bridge_merges_all_connected_sessions(self):
        self.apply([view('a', 'u', 0), view('b', 'u', 2001), view('c', 'u', 4002), view('d', 'u', 6002)])
        self.assertEqual(len(self.report()['sessions']), 4)
        self.apply([view('x', 'u', 1500), view('y', 'u', 3500)])
        report = self.report()
        self.assertEqual(len(report['sessions']), 2)
        # A second bridge closes the last >1800s boundary and reaches the isolated session.
        self.apply([view('z', 'u', 5500)])
        report = self.report()
        self.assertEqual(len(report['sessions']), 1)
        self.assertEqual(report['sessions'][0]['id'], 'a')
        self.assertEqual(report['sessions'][0]['event_count'], 7)
        self.assert_matches_reference(3)

    def test_bridge_point_deletion_splits_sessions(self):
        self.apply([view('a', 'u', 0), view('b', 'u', 1500), view('c', 'u', 3000)])
        self.assertEqual(len(self.report()['sessions']), 1)
        self.apply([{'op': 'delete', 'id': 'b'}])
        report = self.report()
        self.assertEqual([s['id'] for s in report['sessions']], ['a', 'c'])
        self.assert_matches_reference(2)

    def test_user_migration_repairs_both_users(self):
        self.apply([view('a', 'u1', 0), view('b', 'u1', 100), view('x', 'u2', 50)])
        self.apply([view('b', 'u2', 50)])
        report = self.report()
        self.assertEqual([(s['user'], s['id'], s['event_count']) for s in report['sessions']],
                         [('u1', 'a', 1), ('u2', 'b', 2)])
        self.assert_matches_reference(2)

    def test_deleting_all_events_from_user_leaves_empty_user(self):
        self.apply([view('a', 'u1', 0), view('b', 'u1', 100), view('x', 'u2', 0)])
        self.apply([{'op': 'delete', 'id': 'a'}, {'op': 'delete', 'id': 'b'}])
        report = self.report()
        self.assertEqual([s['user'] for s in report['sessions']], ['u2'])
        self.assertEqual(report['daily_sessions'], [{'day': 0, 'session_count': 1}])
        self.assert_matches_reference(2)

    def test_reference_comparison_across_random_corrections(self):
        records = [view('e%03d' % i, 'u%d' % (i % 7), (i * 37) % 100000) for i in range(80)]
        self.apply(records)
        self.assert_matches_reference(1)
        # Out-of-order corrections, moves, kind changes and deletes, several per batch.
        self.apply([search('e000', 'u9', 12345, 'seo'), purchase('e001', 'u9', 12500, 0),
                    view('e002', 'u9', 13000), {'op': 'delete', 'id': 'e003'}])
        self.assert_matches_reference(2)
        self.apply([view('e010', 'u2', 99999), view('e011', 'u2', 99800),
                    purchase('e012', 'u2', 99950, 7), {'op': 'delete', 'id': 'e013'}])
        self.assert_matches_reference(3)
        self.apply([{'op': 'delete', 'id': 'e%03d' % i} for i in range(0, 40)])
        self.assert_matches_reference(4)


class AttributionTests(SessionCase):
    def setUp(self):
        super().setUp()
        # One stable session containing all fixed attribution fixtures.
        self.apply([
            search('s1', 'u', 100, 'paid'),
            search('s2', 'u', 200, 'seo'),
            purchase('o3600', 'u', 2000, 11),
            purchase('o3601', 'u', 3801, 12),
            purchase('osame', 'u', 200, 13),
            purchase('alone', 'u', 5500, 14),
            purchase('zero', 'u', 300, 0),
        ])

    def test_last_search_and_3600_boundary(self):
        orders = {o['id']: o for o in self.report()['orders']}
        self.assertEqual(orders['o3600']['search_id'], 's2')
        self.assertEqual(orders['o3600']['channel'], 'seo')
        self.assertIsNone(orders['o3601']['search_id'])
        self.assertIsNone(orders['o3601']['channel'])
        self.assert_matches_reference(1)

    def test_same_second_uses_strict_id_order(self):
        order = next(o for o in self.report()['orders'] if o['id'] == 'osame')
        # s2 has the same timestamp as the order and is not strictly before it.
        self.assertEqual(order['search_id'], 's1')
        self.assertEqual(order['channel'], 'paid')
        self.assert_matches_reference(1)

    def test_order_without_search_and_zero_amount_are_still_materialized(self):
        orders = {o['id']: o for o in self.report()['orders']}
        self.assertIsNone(orders['alone']['search_id'])
        self.assertEqual(orders['zero']['amount_cents'], 0)
        daily = self.report()['daily_orders']
        null_group = next(d for d in daily if d['day'] == 0 and d['channel'] is None)
        # osame is unattributed because s2 has the same timestamp; alone is a later no-search order.
        self.assertEqual(null_group['order_count'], 2)
        self.assert_matches_reference(1)

    def test_kind_correction_can_create_and_remove_attribution(self):
        self.apply([view('s2', 'u', 200)])
        order = next(o for o in self.report()['orders'] if o['id'] == 'o3600')
        self.assertEqual(order['search_id'], 's1')
        self.assertEqual(order['channel'], 'paid')
        self.apply([search('s2', 'u', 200, 'social')])
        order = next(o for o in self.report()['orders'] if o['id'] == 'o3600')
        self.assertEqual(order['search_id'], 's2')
        self.assertEqual(order['channel'], 'social')
        self.assert_matches_reference(3)

    def test_amount_correction_updates_daily_amount_once(self):
        self.apply([purchase('zero', 'u', 300, 42)])
        report = self.report()
        order = next(o for o in report['orders'] if o['id'] == 'zero')
        self.assertEqual(order['amount_cents'], 42)
        daily = next(d for d in report['daily_orders'] if d['day'] == 0 and d['channel'] == 'seo')
        self.assertEqual(daily['amount_cents'], 53)
        self.assert_matches_reference(2)


class IntegrationAndSafetyTests(SessionCase):
    def seed_linked_case(self):
        self.apply([search('s', 'u', 85800, 'paid'), purchase('o', 'u', 88800, 500)])
        before = self.report()
        self.assertEqual([x['id'] for x in before['sessions']], ['s', 'o'])
        self.assertEqual(before['daily_sessions'], [
            {'day': 0, 'session_count': 1}, {'day': 1, 'session_count': 1}])
        unattributed = before['orders'][0]
        self.assertIsNone(unattributed['search_id'])
        self.assertEqual(before['daily_orders'], [
            {'day': 1, 'channel': None, 'order_count': 1, 'amount_cents': 500}])
        return before

    def test_bridge_attribution_and_revert(self):
        self.seed_linked_case()
        self.apply([view('b', 'u', 87600)])
        joined = self.report()
        self.assertEqual(joined['sessions'], [
            {'id': 's', 'user': 'u', 'start': 85800, 'end': 88800, 'event_count': 3}])
        self.assertEqual(joined['orders'][0]['search_id'], 's')
        self.assertEqual(joined['orders'][0]['channel'], 'paid')
        self.assertEqual(joined['daily_sessions'], [{'day': 0, 'session_count': 1}])
        self.assertEqual(joined['daily_orders'], [
            {'day': 0, 'channel': 'paid', 'order_count': 1, 'amount_cents': 500}])
        self.apply([{'op': 'delete', 'id': 'b'}])
        restored = self.report()
        expected = self.seed_linked_case()
        expected['revision'] = restored['revision']
        self.assertEqual(restored, expected)

    def test_move_bridge_away_and_back(self):
        self.seed_linked_case()
        self.apply([view('b', 'u', 87600)])
        self.apply([view('b', 'other', 87600)])
        split = self.report()
        self.assertEqual([(s['user'], s['id']) for s in split['sessions']],
                         [('other', 'b'), ('u', 's'), ('u', 'o')])
        self.assertIsNone(split['orders'][0]['search_id'])
        self.apply([view('b', 'u', 87600)])
        joined = self.report()
        self.assertEqual(joined['sessions'][0]['id'], 's')
        self.assertEqual(joined['orders'][0]['search_id'], 's')
        self.assert_matches_reference(4)

    def test_bad_tail_line_rolls_back_everything(self):
        path = self.dir / 'bad.jsonl'
        path.write_text(jsonl([view('a', 'u', 0)]) + '{"op":"put",', encoding='utf-8')
        result = cli('session-apply', '--db', self.db, '--source', path)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.report()['revision'], 0)

    def test_process_reopen_preserves_all_tables_and_revision(self):
        self.apply([search('s', 'u', 0, 'paid'), purchase('o', 'u', 100, 9)])
        con = sqlite3.connect('file:%s?mode=ro' % self.db, uri=True)
        try:
            self.assertEqual(con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM members').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT search_id,amount_cents FROM orders').fetchone(), ('s', 9))
            self.assertEqual(con.execute('SELECT SUM(session_count) FROM daily_sessions').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT SUM(amount_cents) FROM daily_orders').fetchone()[0], 9)
        finally:
            con.close()
        self.assertEqual(self.report()['revision'], 1)

    def test_interleaved_reader_sees_only_complete_revision(self):
        self.apply([view('a', 'u', 0)])
        before = self.report()
        con = sqlite3.connect('file:%s?mode=rw' % self.db)
        try:
            con.execute('BEGIN IMMEDIATE')
            con.execute("INSERT INTO events(id,user,at,kind) VALUES('x','u',100,'view')")
            con.execute("UPDATE meta SET value=99 WHERE key='revision'")
            seen = self.report()
            self.assertEqual(seen, before)
        finally:
            con.execute('ROLLBACK')
            con.close()

    def test_deterministic_bytes_and_safety(self):
        self.apply([search('a', 'u', 0, 'z'), purchase('b', 'u', 10, 3), view('c', 'v', 100000)])
        out1, out2 = self.dir / 'a.json', self.dir / 'b.json'
        self.assertEqual(cli('session-report', '--db', self.db, '--output', out1).returncode, 0)
        self.assertEqual(cli('session-report', '--db', self.db, '--output', out2).returncode, 0)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        self.assertEqual(cli('session-report', '--db', self.db, '--output', self.db).returncode, 2)
        link = self.dir / 'link.json'
        link.symlink_to(self.dir / 'victim')
        self.assertEqual(cli('session-report', '--db', self.db, '--output', link).returncode, 2)
        self.assertEqual(cli('session-apply', '--db', self.db, '--source', self.db).returncode, 2)
        self.assertEqual(cli('session-init', '--db', self.db).returncode, 2)

    def test_strict_jsonl_and_event_field_validation(self):
        valid = view('ok', 'u', 0)
        bad_batches = [
            b'\n', b'   \n', b'{"op":"put","id":"a","id":"b","user":"u","at":1,"kind":"view"}\n',
            b'{"op":"put","id":"a","user":"u","at":1,"kind":"view","x":1}\n',
            b'{"op":"put","id":"a","user":"u","at":1,"kind":"search"}\n',
            b'{"op":"put","id":"a","user":"u","at":1,"kind":"purchase"}\n',
            b'{"op":"put","id":"a","user":"u","at":true,"kind":"view"}\n',
            b'{"op":"put","id":"a","user":"u","at":-1,"kind":"view"}\n',
            b'{"op":"put","id":"a","user":"u","at":4102444801,"kind":"view"}\n',
            b'{"op":"put","id":"a","user":"u","at":1,"kind":"purchase","amount_cents":true}\n',
            b'{"op":"put","id":"a","user":"u","at":1,"kind":"purchase","amount_cents":1000000001}\n',
            b'{"op":"frobnicate","id":"a"}\n',
            json.dumps(valid).encode() + b' ' + b'x' * (1024 * 1024) + b'\n',
        ]
        for body in bad_batches:
            path = self.dir / 'bad.jsonl'
            path.write_bytes(body)
            result = cli('session-apply', '--db', self.db, '--source', path)
            self.assertEqual(result.returncode, 2, body[:50])
            self.assertEqual(self.report()['revision'], 0)
        path = self.dir / 'good.jsonl'
        path.write_text(json.dumps(valid), encoding='utf-8')
        self.assertEqual(cli('session-apply', '--db', self.db, '--source', path).returncode, 0)

    def test_duplicate_batch_id_and_empty_batch(self):
        self.source([view('a', 'u', 0)], 'first.jsonl')
        path = self.dir / 'dup.jsonl'
        path.write_text(jsonl([view('a', 'u', 0), view('a', 'u', 1)]), encoding='utf-8')
        self.assertEqual(cli('session-apply', '--db', self.db, '--source', path).returncode, 2)
        empty = self.dir / 'empty.jsonl'
        empty.write_bytes(b'')
        self.assertEqual(cli('session-apply', '--db', self.db, '--source', empty).returncode, 0)
        self.assertEqual(self.report()['revision'], 0)

    def test_write_exception_before_commit_rolls_back(self):
        # A trigger is installed only for this test; SQLite raises while applying the batch,
        # before the transaction commits, so revision and all derived tables remain unchanged.
        con = sqlite3.connect(self.db)
        try:
            con.execute('CREATE TRIGGER fail_event_insert BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,"injected"); END')
        finally:
            con.close()
        result = self.apply([view('a', 'u', 0)], expect=2)
        self.assertIn('injected', result.stderr)
        self.assertEqual(self.report()['revision'], 0)


class LocalityTests(SessionCase):
    def test_small_batch_does_not_scan_independent_sessions(self):
        records = []
        for user in ('u1', 'u2', 'u3'):
            for j in range(6):
                records.append(view('%s_%d' % (user, j), user, j * 10000))
        self.apply(records)
        # Instrument the process-wide sqlite connection used by session.apply.  The trace gives
        # auditable SQL text; EXPLAIN QUERY PLAN then proves each persistent events/members access
        # is an index seek or a bounded index range, never a full table/user scan.
        from retrieval_eval import session as session_store
        statements = []
        real_connect = sqlite3.connect

        def tracing_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        source = self.source([view('u1_1', 'u1', 10001)])
        sqlite3.connect = tracing_connect
        try:
            self.assertEqual(session_store.apply(self.db, source)['revision'], 2)
        finally:
            sqlite3.connect = real_connect
        joined_trace = '\n'.join(statements)
        self.assertIn("CREATE TEMP TABLE ordered_repair_events", joined_trace)
        self.assertIn("INSERT INTO members", joined_trace)
        audit = sqlite3.connect(self.db)
        try:
            # Representative persistent-table predicates used by the bounded repair path.
            plans = [
                'EXPLAIN QUERY PLAN SELECT * FROM events WHERE user=? AND ABS(at-?)<=1800',
                'EXPLAIN QUERY PLAN SELECT * FROM members WHERE event_id=?',
                'EXPLAIN QUERY PLAN SELECT * FROM sessions WHERE id=?',
            ]
            rendered_plans = [' '.join(row[3] for row in audit.execute(plan, ('u1', 10000, 'u1_1')[:plan.count('?')]))
                              for plan in plans]
            for plan in rendered_plans:
                self.assertNotIn('SCAN events', plan)
                self.assertNotIn('SCAN members', plan)
                self.assertNotIn('SCAN sessions', plan)
            self.assertIn('CREATE TEMP TABLE candidate_events', joined_trace)
            self.assertIn('INSERT INTO members', joined_trace)
            # The repair interval itself is bounded around u1's first two independent sessions.
            self.assertEqual(audit.execute(
                "SELECT COUNT(*) FROM events WHERE user='u1' AND at BETWEEN 0 AND 30000").fetchone()[0], 4)
            self.assertEqual(audit.execute(
                "SELECT COUNT(*) FROM events WHERE user<>'u1'").fetchone()[0], 12)
            self.assertEqual(audit.execute(
                "SELECT COUNT(*) FROM events WHERE user='u1' AND at>30000").fetchone()[0], 2)
        finally:
            audit.close()
        self.assert_matches_reference(2)


class CapacityTests(SessionCase):
    def test_large_warehouse_memory(self):
        # 100k users with ten events each gives one million events and one million sessions.
        # All grouping and joins execute in SQLite; Python only streams/stages input lines.
        users = 100_000
        per_user = 10
        source = self.dir / 'million.jsonl'
        with source.open('w', encoding='utf-8') as stream:
            for user_id in range(users):
                user = 'u%06d' % user_id
                for j in range(per_user):
                    stream.write(json.dumps(view('%s_e%d' % (user, j), user, j * 10000), separators=(',', ':')))
                    stream.write('\n')
        code, rss, stderr = cli_rss('session-apply', '--db', self.db, '--source', source)
        self.assertEqual(code, 0, stderr)
        print('\nsession capacity: million-event apply peak RSS %.1f MiB' % (rss / 1024))
        self.assertLessEqual(rss, RSS_LIMIT)
        out = self.dir / 'large-report.json'
        code, rss, stderr = cli_rss('session-report', '--db', self.db, '--output', out)
        self.assertEqual(code, 0, stderr)
        print('session capacity: million-row report peak RSS %.1f MiB' % (rss / 1024))
        self.assertLessEqual(rss, RSS_LIMIT)
        report = json.loads(out.read_text())
        self.assertEqual(report['revision'], 1)
        self.assertEqual(len(report['sessions']), users * per_user)
        self.assertEqual(sum(s['event_count'] for s in report['sessions']), users * per_user)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM events').fetchone()[0], users * per_user)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM members').fetchone()[0], users * per_user)
        finally:
            con.close()

    def test_long_single_user_session_memory(self):
        # A 100k-event connected chain exercises explicit member materialization and windowed
        # boundary handling without creating 100k independent sessions.
        source = self.dir / 'long-session.jsonl'
        with source.open('w', encoding='utf-8') as stream:
            for i in range(100_000):
                stream.write(json.dumps(view('e%07d' % i, 'long-user', i * 1000),
                                        separators=(',', ':')))
                stream.write('\n')
        code, rss, stderr = cli_rss('session-apply', '--db', self.db, '--source', source)
        self.assertEqual(code, 0, stderr)
        print('\nsession capacity: long-session apply peak RSS %.1f MiB' % (rss / 1024))
        self.assertLessEqual(rss, RSS_LIMIT)
        report = self.report('long-report.json')
        self.assertEqual(len(report['sessions']), 1)
        self.assertEqual(report['sessions'][0]['event_count'], 100_000)
        self.assertEqual(report['sessions'][0]['id'], 'e0000000')


if __name__ == '__main__':
    unittest.main()
