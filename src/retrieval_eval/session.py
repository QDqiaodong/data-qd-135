"""Revisioned SQLite sessionization and order-attribution warehouse.

The warehouse deliberately separates three durable layers:

* ``events`` is the corrected global event fact table.
* ``sessions``/``members`` explicitly materialize every session boundary and membership.
* ``orders`` plus the two sparse daily tables materialize attribution and reporting facts.

A batch is parsed and staged one JSONL line at a time.  All fact changes, boundary repairs,
attribution withdrawals/recomputation, daily deltas and the revision update happen in one
SQLite transaction.  Reports only read those materialized tables; they never sessionize or
attribute events again.

Small batches locate a repair interval through ``(user, at, id)`` indexes.  Only old sessions
touching that interval, and the final events inside its merged/split interval, are read.
Independent users and independent sessions of the same user are not scanned.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from .common import ident

LINE_LIMIT = 1024 * 1024
MAX_AT = 4_102_444_800
MAX_AMOUNT = 1_000_000_000
SESSION_GAP = 1800
ATTRIBUTION_WINDOW = 3600
DAY_SECONDS = 86_400

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE events(
  id TEXT PRIMARY KEY,
  user TEXT NOT NULL,
  at INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('view','search','purchase')),
  channel TEXT,
  amount_cents INTEGER,
  CHECK((kind='view' AND channel IS NULL AND amount_cents IS NULL)
     OR (kind='search' AND channel IS NOT NULL AND amount_cents IS NULL)
     OR (kind='purchase' AND channel IS NULL AND amount_cents IS NOT NULL))
);
CREATE INDEX events_by_user_time ON events(user, at, id);
CREATE TABLE sessions(
  id TEXT PRIMARY KEY,
  user TEXT NOT NULL,
  start INTEGER NOT NULL,
  end INTEGER NOT NULL,
  event_count INTEGER NOT NULL,
  day INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX sessions_by_user_start ON sessions(user, start, id);
CREATE INDEX sessions_by_day ON sessions(day);
CREATE TABLE members(
  user TEXT NOT NULL,
  at INTEGER NOT NULL,
  event_id TEXT NOT NULL PRIMARY KEY,
  session_id TEXT NOT NULL
);
CREATE INDEX members_by_user_time ON members(user, at, event_id);
CREATE INDEX members_by_session_time ON members(session_id, at, event_id);
CREATE TABLE orders(
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  search_id TEXT,
  channel TEXT,
  amount_cents INTEGER NOT NULL
);
CREATE INDEX orders_by_session ON orders(session_id);
CREATE INDEX orders_by_search ON orders(search_id);
CREATE TABLE daily_sessions(
  day INTEGER PRIMARY KEY,
  session_count INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE daily_orders(
  day INTEGER NOT NULL,
  channel TEXT,
  order_count INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  group_key TEXT NOT NULL PRIMARY KEY
) WITHOUT ROWID;
CREATE INDEX daily_orders_by_day ON daily_orders(day, group_key);
"""

STAGE = """
CREATE TEMP TABLE stage_event(
  id TEXT PRIMARY KEY,
  op TEXT NOT NULL CHECK(op IN ('put','delete')),
  user TEXT,
  at INTEGER,
  kind TEXT,
  channel TEXT,
  amount_cents INTEGER,
  old_user TEXT,
  old_at INTEGER
);
"""


def _connect(db: os.PathLike[str] | str, mode: str) -> sqlite3.Connection:
    uri = 'file:' + quote(os.path.abspath(os.fspath(db))) + '?mode=' + mode
    con = sqlite3.connect(uri, uri=True, isolation_level=None)
    con.execute('PRAGMA busy_timeout=5000')
    con.execute('PRAGMA temp_store=FILE')
    con.execute('PRAGMA cache_size=-2000')
    con.execute('PRAGMA mmap_size=0')
    return con


def _meta(con: sqlite3.Connection) -> int:
    try:
        pairs = dict(con.execute('SELECT key, value FROM meta'))
    except sqlite3.Error:
        raise ValueError('not a session warehouse: meta table missing') from None
    if pairs.get('schema') != 1 or type(pairs.get('revision')) is not int:
        raise ValueError('not a session warehouse: bad meta contents')
    return pairs['revision']


def init(db: os.PathLike[str] | str) -> dict[str, int]:
    """Create a new independent session warehouse; never overwrite or open an existing path."""
    path = Path(db)
    if os.path.lexists(path):
        raise ValueError('database already exists: ' + os.fspath(db))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)
    try:
        con = sqlite3.connect(os.fspath(path), isolation_level=None)
        try:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA busy_timeout=5000')
            con.execute('PRAGMA temp_store=FILE')
            con.execute('BEGIN')
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    con.execute(statement)
            con.executemany('INSERT INTO meta VALUES(?, ?)', (('schema', 1), ('revision', 0)))
            con.execute('COMMIT')
        finally:
            con.close()
    except Exception:
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(os.fspath(path) + suffix)
            except OSError:
                pass
        raise
    return {'revision': 0}


def _parse_json(line: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result

    def invalid(value):
        raise ValueError('non-finite JSON number: ' + value)

    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('non-finite JSON number: ' + value)
        return result

    return json.loads(line, object_pairs_hook=unique,
                      parse_constant=invalid, parse_float=finite)


def _strict_integer(value, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('%s must be an integer in %d..%d' % (name, minimum, maximum))
    return value


def _stage_record(con: sqlite3.Connection, obj: object) -> None:
    if not isinstance(obj, dict):
        raise ValueError('expected a JSON object')
    op = obj.get('op')
    if op == 'delete':
        if set(obj) != {'op', 'id'}:
            raise ValueError('delete record fields must be exactly op,id')
        eid = ident(obj['id'])
        con.execute('INSERT INTO stage_event(id, op) VALUES(?, ?)', (eid, op))
        return
    if op != 'put':
        raise ValueError('unknown op: ' + repr(op))
    base = {'op', 'id', 'user', 'at', 'kind'}
    if not base.issubset(obj):
        raise ValueError('put record is missing required fields')
    eid, user = ident(obj['id']), ident(obj['user'])
    at = _strict_integer(obj['at'], 0, MAX_AT, 'at')
    kind = obj['kind']
    channel = None
    amount = None
    if kind == 'view':
        if set(obj) != base:
            raise ValueError('view record fields must be exactly op,id,user,at,kind')
    elif kind == 'search':
        if set(obj) != base | {'channel'}:
            raise ValueError('search record fields must be exactly op,id,user,at,kind,channel')
        channel = ident(obj['channel'])
    elif kind == 'purchase':
        if set(obj) != base | {'amount_cents'}:
            raise ValueError('purchase record fields must be exactly op,id,user,at,kind,amount_cents')
        amount = _strict_integer(obj['amount_cents'], 0, MAX_AMOUNT, 'amount_cents')
    else:
        raise ValueError('unknown event kind: ' + repr(kind))
    try:
        con.execute('''INSERT INTO stage_event
            (id, op, user, at, kind, channel, amount_cents)
            VALUES(?, 'put', ?, ?, ?, ?, ?)''',
                    (eid, user, at, kind, channel, amount))
    except sqlite3.IntegrityError:
        raise ValueError('duplicate event operation in batch: ' + eid) from None


def _stream_batch(con: sqlite3.Connection, source: os.PathLike[str] | str) -> int:
    count = 0
    with open(source, 'rb') as stream:
        while True:
            chunk = stream.readline(LINE_LIMIT + 2)
            if not chunk:
                return count
            count += 1
            line = chunk[:-1] if chunk.endswith(b'\n') else chunk
            try:
                if len(line) > LINE_LIMIT:
                    raise ValueError('line exceeds 1 MiB')
                if not line.strip():
                    raise ValueError('blank line')
                _stage_record(con, _parse_json(line.decode('utf-8')))
            except (ValueError, UnicodeError, json.JSONDecodeError) as error:
                raise ValueError('line %d: %s' % (count, error)) from None


def _capture_old_stage_values(con: sqlite3.Connection) -> None:
    con.execute('''UPDATE stage_event SET old_user = e.user, old_at = e.at
                   FROM events e WHERE stage_event.id = e.id''')


def _legacy_dirty_state(con: sqlite3.Connection) -> None:
    """Capture every pre-change session touching an anchor or an adjacent event coordinate."""
    con.execute('''CREATE TEMP TABLE anchor_neighbors AS
        WITH pre_change AS (
          SELECT e.user, e.at, e.id FROM stage_event s JOIN events e ON e.id=s.id
        ), usable_anchors AS (
          SELECT user, at, id FROM pre_change
          UNION
          SELECT s.user, s.at, s.id FROM stage_event s WHERE s.op='put'
            AND NOT EXISTS (SELECT 1 FROM pre_change p
                            WHERE p.user=s.user AND p.at=s.at AND p.id=s.id)
        ), previous_neighbor AS (
          SELECT a.user, prev.at, prev.id
          FROM usable_anchors a JOIN events prev
            ON prev.user=a.user AND (prev.at,prev.id) < (a.at,a.id)
          WHERE a.at-prev.at <= ?
            AND NOT EXISTS (
              SELECT 1 FROM events mid WHERE mid.user=a.user
                AND (mid.at,mid.id) > (prev.at,prev.id)
                AND (mid.at,mid.id) < (a.at,a.id))
        ), next_neighbor AS (
          SELECT a.user, nxt.at, nxt.id
          FROM usable_anchors a JOIN events nxt
            ON nxt.user=a.user AND (nxt.at,nxt.id) > (a.at,a.id)
          WHERE nxt.at-a.at <= ?
            AND NOT EXISTS (
              SELECT 1 FROM events mid WHERE mid.user=a.user
                AND (mid.at,mid.id) > (a.at,a.id)
                AND (mid.at,mid.id) < (nxt.at,nxt.id))
        )
        SELECT user, at, id FROM usable_anchors
        UNION SELECT user, at, id FROM previous_neighbor
        UNION SELECT user, at, id FROM next_neighbor''',
                (SESSION_GAP, SESSION_GAP))
    con.execute('''CREATE TEMP TABLE dirty_sessions AS
        SELECT DISTINCT s.* FROM sessions s
        JOIN members m ON m.session_id=s.id
        JOIN anchor_neighbors n ON n.user=m.user AND n.at=m.at AND n.id=m.event_id''')
    con.execute('CREATE INDEX dirty_sessions_id ON dirty_sessions(id)')
    con.execute('''CREATE TEMP TABLE ordered_members AS
        SELECT s.id AS sid, s.user, m.at, m.event_id,
               ROW_NUMBER() OVER (PARTITION BY s.id ORDER BY m.at, m.event_id) AS rn_asc,
               ROW_NUMBER() OVER (PARTITION BY s.id ORDER BY m.at DESC, m.event_id DESC) AS rn_desc,
               COUNT(*) OVER (PARTITION BY s.id) AS member_count
        FROM dirty_sessions s JOIN members m ON m.session_id=s.id''')
    con.execute('''CREATE TEMP TABLE legacy_ranges AS
        SELECT f.user, f.at AS lo, f.event_id AS lo_id, l.at AS hi, l.event_id AS hi_id
        FROM ordered_members f JOIN ordered_members l ON l.sid=f.sid
        WHERE f.rn_asc=1 AND l.rn_desc=1''')
    con.execute('CREATE TEMP TABLE affected_orders(id TEXT PRIMARY KEY) WITHOUT ROWID')
    con.execute('INSERT OR IGNORE INTO affected_orders(id) SELECT id FROM orders WHERE session_id IN (SELECT id FROM dirty_sessions)')


def _remove_old_materialization(con: sqlite3.Connection) -> None:
    con.execute('CREATE TEMP TABLE old_session_days AS SELECT day, COUNT(*) AS n FROM dirty_sessions GROUP BY day')
    con.execute('''CREATE TEMP TABLE old_order_groups AS
        SELECT s.day, o.channel, CAST(s.day AS TEXT)||'|'||COALESCE(o.channel,'') AS group_key,
               COUNT(*) AS n, SUM(o.amount_cents) AS amount
        FROM orders o JOIN sessions s ON s.id=o.session_id
        WHERE o.id IN (SELECT id FROM affected_orders) GROUP BY s.day, o.channel''')
    con.execute('DELETE FROM orders WHERE id IN (SELECT id FROM affected_orders)')
    con.execute('''UPDATE daily_orders SET
                     order_count = daily_orders.order_count - old.n,
                     amount_cents = daily_orders.amount_cents - old.amount
                   FROM old_order_groups old
                   WHERE daily_orders.group_key=old.group_key''')
    con.execute('DELETE FROM daily_orders WHERE order_count=0')
    con.execute('''UPDATE daily_sessions SET session_count = daily_sessions.session_count - old.n
                   FROM old_session_days old WHERE daily_sessions.day=old.day''')
    con.execute('DELETE FROM daily_sessions WHERE session_count=0')
    con.execute('''DELETE FROM members
                   WHERE event_id IN (SELECT event_id FROM ordered_members)''')
    con.execute('DELETE FROM sessions WHERE id IN (SELECT id FROM dirty_sessions)')


def _apply_event_facts(con: sqlite3.Connection) -> None:
    con.execute('DELETE FROM events WHERE id IN (SELECT id FROM stage_event)')
    con.execute('''INSERT INTO events(id, user, at, kind, channel, amount_cents)
                   SELECT id, user, at, kind, channel, amount_cents
                   FROM stage_event WHERE op = 'put' ''')


def _final_intervals(con: sqlite3.Connection) -> None:
    """Build connected final components around changed coordinates using set-based SQL."""
    con.execute('''CREATE TEMP TABLE repair_seed_points AS
        SELECT e.user, e.at, e.id
        FROM events e JOIN anchor_neighbors n ON n.user=e.user AND n.at=e.at AND n.id=e.id
        UNION
        SELECT e.user, e.at, e.id FROM stage_event s JOIN events e ON e.id=s.id WHERE s.op='put'
        UNION
        SELECT e.user, e.at, e.id FROM events e JOIN legacy_ranges r ON r.user=e.user
        WHERE (e.at,e.id) BETWEEN (r.lo,r.lo_id) AND (r.hi,r.hi_id)''')
    con.execute('CREATE INDEX repair_seed_points_user_at ON repair_seed_points(user,at)')
    # A cold warehouse (no pre-existing materialized sessions) has no boundary to localize: the
    # final facts are exactly the staged puts. Read them in (user, at, id) index order directly,
    # avoiding a quadratic self-join across the whole new batch.
    cold = con.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0 and \
        con.execute("SELECT COUNT(*) FROM stage_event WHERE op <> 'put'").fetchone()[0] == 0
    if cold:
        con.execute('''CREATE TEMP TABLE candidate_events AS
            SELECT user, at, id,
                   LAG(at) OVER (PARTITION BY user ORDER BY at,id) AS prev_at,
                   ROW_NUMBER() OVER (PARTITION BY user ORDER BY at,id) AS rn
            FROM events''')
    else:
        # Incremental repair: include every seed and only its immediate predecessor/successor in
        # the final ordering. The one-hop UNION (not a range self-join) stays linear in the
        # affected coordinates even when a long chain bridges many old sessions.
        con.execute('''CREATE TEMP TABLE nearby_events AS
            SELECT user, at, id FROM repair_seed_points
            UNION
            SELECT prev.user, prev.at, prev.id
            FROM repair_seed_points p JOIN events prev
              ON prev.user=p.user AND (prev.at,prev.id) < (p.at,p.id)
            WHERE NOT EXISTS (
              SELECT 1 FROM events mid WHERE mid.user=p.user
                AND (mid.at,mid.id) > (prev.at,prev.id)
                AND (mid.at,mid.id) < (p.at,p.id))
            UNION
            SELECT nxt.user, nxt.at, nxt.id
            FROM repair_seed_points p JOIN events nxt
              ON nxt.user=p.user AND (nxt.at,nxt.id) > (p.at,p.id)
            WHERE NOT EXISTS (
              SELECT 1 FROM events mid WHERE mid.user=p.user
                AND (mid.at,mid.id) > (p.at,p.id)
                AND (mid.at,mid.id) < (nxt.at,nxt.id))''')
        con.execute('''CREATE TEMP TABLE candidate_events AS
            SELECT user, at, id,
                   LAG(at) OVER (PARTITION BY user ORDER BY at,id) AS prev_at,
                   ROW_NUMBER() OVER (PARTITION BY user ORDER BY at,id) AS rn
            FROM nearby_events''')
    con.execute('CREATE INDEX candidate_events_rn ON candidate_events(user,rn)')
    con.execute('CREATE INDEX candidate_events_user_at ON candidate_events(user,at,id)')
    con.execute('''CREATE TEMP TABLE repair_bounds AS
        WITH flags AS (
          SELECT *, CASE WHEN prev_at IS NULL OR at-prev_at>? THEN 1 ELSE 0 END AS is_start
          FROM candidate_events
        ), grouped AS (
          SELECT *, SUM(is_start) OVER (PARTITION BY user ORDER BY rn ROWS UNBOUNDED PRECEDING) AS grp
          FROM flags
        ), last_rows AS (
          SELECT user,grp,at AS hi_at,id AS hi_id,
                 ROW_NUMBER() OVER (PARTITION BY user,grp ORDER BY at DESC,id DESC) AS rn
          FROM grouped
        ), limits AS (
          SELECT user,grp,hi_at,hi_id FROM last_rows WHERE rn=1
        )
        SELECT g.user, g.at AS lo, g.id AS lo_id, l.hi_at AS hi, l.hi_id AS hi_id
        FROM grouped g JOIN limits l ON l.user=g.user AND l.grp=g.grp
        WHERE g.is_start=1
          AND EXISTS (SELECT 1 FROM repair_seed_points p
                      WHERE p.user=g.user AND p.at BETWEEN g.at AND l.hi_at)''',
                (SESSION_GAP,))



def _rebuild_intervals(con: sqlite3.Connection) -> None:
    """Rebuild all affected sessions with set-based SQL; Python holds only table names."""
    con.execute('CREATE TEMP TABLE new_sessions(id TEXT PRIMARY KEY, day INTEGER NOT NULL) WITHOUT ROWID')
    con.execute('''CREATE TEMP TABLE ordered_repair_events AS
        SELECT e.id, e.user, e.at,
               LAG(e.at) OVER (PARTITION BY e.user ORDER BY e.at,e.id) AS prev_at,
               ROW_NUMBER() OVER (PARTITION BY e.user ORDER BY e.at,e.id) AS rn
        FROM events e JOIN repair_bounds r ON r.user=e.user
        WHERE (e.at,e.id) BETWEEN (r.lo,r.lo_id) AND (r.hi,r.hi_id)''')
    con.execute('CREATE INDEX ordered_repair_events_rn ON ordered_repair_events(user,rn)')
    con.execute('''CREATE TEMP TABLE session_bounds AS
        WITH flags AS (
          SELECT *, CASE WHEN prev_at IS NULL OR at-prev_at>? THEN 1 ELSE 0 END AS is_start
          FROM ordered_repair_events
        ), grouped AS (
          SELECT *, SUM(is_start) OVER (PARTITION BY user ORDER BY rn ROWS UNBOUNDED PRECEDING) AS grp
          FROM flags
        )
        SELECT grp, user, at AS start_at, id AS start_id, rn AS start_rn
        FROM grouped WHERE is_start=1''',
                (SESSION_GAP,))
    con.execute('''CREATE TEMP TABLE session_ranges AS
        WITH bounded AS (
          SELECT b.*,
                 LEAD(b.start_rn) OVER (PARTITION BY b.user ORDER BY b.start_rn) AS next_start_rn
          FROM session_bounds b
        )
        SELECT user, grp, start_at, start_id, start_rn,
               COALESCE(next_start_rn-1,
                        (SELECT MAX(rn) FROM ordered_repair_events o WHERE o.user=bounded.user)) AS end_rn
        FROM bounded''')
    con.execute('''INSERT INTO sessions(id,user,start,end,event_count,day)
        SELECT r.start_id, r.user, r.start_at,
               (SELECT e.at FROM ordered_repair_events e
                 WHERE e.user=r.user AND e.rn=r.end_rn),
               r.end_rn-r.start_rn+1, r.start_at/?
        FROM session_ranges r''', (DAY_SECONDS,))
    con.execute('''INSERT INTO members(user,at,event_id,session_id)
        SELECT e.user,e.at,e.id,r.start_id
        FROM ordered_repair_events e JOIN session_ranges r
          ON r.user=e.user AND e.rn BETWEEN r.start_rn AND r.end_rn''')
    con.execute('INSERT INTO new_sessions(id,day) SELECT id,day FROM sessions WHERE id IN (SELECT start_id FROM session_ranges)')
    con.execute('''INSERT INTO affected_orders(id)
                   SELECT m.event_id FROM members m
                   JOIN events e ON e.id=m.event_id AND e.kind='purchase'
                   WHERE m.session_id IN (SELECT id FROM new_sessions)
                   ON CONFLICT(id) DO NOTHING''')
    con.execute('''INSERT INTO daily_sessions(day, session_count)
                   SELECT day, COUNT(*) FROM new_sessions GROUP BY day
                   ON CONFLICT(day) DO UPDATE SET
                     session_count=daily_sessions.session_count+excluded.session_count''')


def _reattribute_orders(con: sqlite3.Connection) -> None:
    con.execute('''INSERT INTO orders(id, session_id, search_id, channel, amount_cents)
        WITH purchase_members AS (
          SELECT p.id AS order_id, pm.session_id, pm.at AS order_at, p.amount_cents
          FROM events p JOIN members pm ON pm.event_id=p.id
          WHERE p.kind='purchase' AND p.id IN (SELECT id FROM affected_orders)
        ), chosen AS (
          SELECT pm.order_id,
                 (SELECT sm.event_id FROM members sm JOIN events se ON se.id=sm.event_id
                  WHERE sm.session_id=pm.session_id AND se.kind='search'
                    AND (sm.at,sm.event_id) < (pm.order_at,pm.order_id)
                    AND pm.order_at-sm.at <= ?
                  ORDER BY sm.at DESC, sm.event_id DESC LIMIT 1) AS search_id,
                 pm.session_id, pm.amount_cents
          FROM purchase_members pm
        )
        SELECT c.order_id, c.session_id, c.search_id, e.channel, c.amount_cents
        FROM chosen c LEFT JOIN events e ON e.id=c.search_id''',
                (ATTRIBUTION_WINDOW,))
    con.execute('''INSERT INTO daily_orders(day, channel, order_count, amount_cents, group_key)
                   SELECT s.day, o.channel, COUNT(*), SUM(o.amount_cents),
                          CAST(s.day AS TEXT)||'|'||COALESCE(o.channel,'')
                   FROM orders o JOIN sessions s ON s.id=o.session_id
                   WHERE o.id IN (SELECT id FROM affected_orders)
                   GROUP BY s.day, o.channel
                   ON CONFLICT(group_key) DO UPDATE SET
                     order_count=daily_orders.order_count+excluded.order_count,
                     amount_cents=daily_orders.amount_cents+excluded.amount_cents''')


def apply(db: os.PathLike[str] | str, source: os.PathLike[str] | str) -> dict[str, int]:
    """Apply one strict JSONL batch atomically and return its post-commit revision."""
    if Path(source).resolve() == Path(db).resolve():
        raise ValueError('database and source must differ')
    con = _connect(db, 'rw')
    try:
        con.execute('BEGIN IMMEDIATE')
        try:
            revision = _meta(con)
            for statement in STAGE.split(';'):
                if statement.strip():
                    con.execute(statement)
            count = _stream_batch(con, source)
            if count == 0:
                con.execute('ROLLBACK')
                return {'records': 0, 'revision': revision}
            _capture_old_stage_values(con)
            _legacy_dirty_state(con)
            _remove_old_materialization(con)
            _apply_event_facts(con)
            _final_intervals(con)
            if con.execute('SELECT COUNT(*) FROM repair_bounds').fetchone()[0]:
                _rebuild_intervals(con)
                _reattribute_orders(con)
            con.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
            con.execute('COMMIT')
            return {'records': count, 'revision': revision + 1}
        except Exception:
            con.execute('ROLLBACK')
            raise
    finally:
        con.close()


def _write_json_string(stream, value: str) -> None:
    stream.write(json.dumps(value, ensure_ascii=False))


def _write_json_int(stream, value: int) -> None:
    stream.write(str(value))


def _write_nullable_string(stream, value: str | None) -> None:
    if value is None:
        stream.write('null')
    else:
        _write_json_string(stream, value)


def _write_report_stream(con: sqlite3.Connection, stream) -> None:
    revision = _meta(con)
    stream.write('{\n  "revision": ')
    _write_json_int(stream, revision)
    stream.write(',\n  "sessions": [')
    first = True
    for sid, user, start, end, event_count in con.execute(
            'SELECT id,user,start,end,event_count FROM sessions ORDER BY user,start,id'):
        stream.write('' if first else ',')
        first = False
        stream.write('\n    {')
        stream.write('"id": ')
        _write_json_string(stream, sid)
        stream.write(', "user": ')
        _write_json_string(stream, user)
        stream.write(', "start": ')
        _write_json_int(stream, start)
        stream.write(', "end": ')
        _write_json_int(stream, end)
        stream.write(', "event_count": ')
        _write_json_int(stream, event_count)
        stream.write('}')
    stream.write('],\n  "orders": [')
    first = True
    for oid, session_id, search_id, channel, amount in con.execute(
            'SELECT id,session_id,search_id,channel,amount_cents FROM orders ORDER BY id'):
        stream.write('' if first else ',')
        first = False
        stream.write('\n    {"id": ')
        _write_json_string(stream, oid)
        stream.write(', "session_id": ')
        _write_json_string(stream, session_id)
        stream.write(', "search_id": ')
        _write_nullable_string(stream, search_id)
        stream.write(', "channel": ')
        _write_nullable_string(stream, channel)
        stream.write(', "amount_cents": ')
        _write_json_int(stream, amount)
        stream.write('}')
    stream.write('],\n  "daily_sessions": [')
    first = True
    for day, session_count in con.execute('SELECT day,session_count FROM daily_sessions ORDER BY day'):
        stream.write('' if first else ',')
        first = False
        stream.write('\n    {"day": ')
        _write_json_int(stream, day)
        stream.write(', "session_count": ')
        _write_json_int(stream, session_count)
        stream.write('}')
    stream.write('],\n  "daily_orders": [')
    first = True
    for day, channel, order_count, amount in con.execute(
            'SELECT day,channel,order_count,amount_cents FROM daily_orders ORDER BY day, group_key'):
        stream.write('' if first else ',')
        first = False
        stream.write('\n    {"day": ')
        _write_json_int(stream, day)
        stream.write(', "channel": ')
        _write_nullable_string(stream, channel)
        stream.write(', "order_count": ')
        _write_json_int(stream, order_count)
        stream.write(', "amount_cents": ')
        _write_json_int(stream, amount)
        stream.write('}')
    stream.write(']\n}\n')


def report(db: os.PathLike[str] | str, output: os.PathLike[str] | str) -> dict[str, int]:
    """Publish a deterministic report from one committed revision without loading all rows."""
    dest = Path(output)
    if dest.is_symlink():
        raise ValueError('output cannot be a symlink')
    if dest.resolve() == Path(db).resolve():
        raise ValueError('database and output must differ')
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(db, 'ro')
    temporary_name = None
    try:
        con.execute('BEGIN')
        try:
            revision = _meta(con)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=dest.parent,
                                             delete=False, newline='\n') as stream:
                temporary_name = stream.name
                _write_report_stream(con, stream)
            con.execute('COMMIT')
        except Exception:
            con.execute('ROLLBACK')
            raise
    finally:
        con.close()
    try:
        os.replace(temporary_name, dest)
    except OSError:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
        raise
    return {'revision': revision}
