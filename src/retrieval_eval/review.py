"""Human arbitration review pack: anchor-versus-candidate divergence samples.

See docs/contract.md (review pack section) for the business contract. Design notes:

- The warehouse is opened read-only and one deferred read transaction pins one complete
  revision (WAL snapshot).  The divergence scan, the coverage units, the selection and
  the streamed sample payload all read that same snapshot, so a concurrently committing
  writer is observed either as the complete old revision or the complete new one.
- Facts are streamed query by query: only one query's judgments and its runs (truncated
  to ``min(k, top)`` positions) are held in Python at a time.  Divergence cells, coverage
  units and per-query selection keys live in SQLite TEMP tables (``temp_store=FILE``),
  never in Python collections.
- A coverage unit is every (group, candidate system) pair, including pairs whose pool of
  divergence samples is smaller than the floor; those are reported as unqualified and
  never as met, and do not count toward ``coverage_complete``.
- Selection keeps one ordered key row per unselected query.  A unit's coverage moving
  0 -> 1 or floor-1 -> floor changes the keys of the other queries covering it; later
  moves change nothing, so bulk update work stays proportional to the cells.
- The report is streamed into a temporary file next to the destination and atomically
  replaced after the read transaction commits.  Every array has a total order, and the
  JSON shape matches ``json.dump(..., indent=2, sort_keys=True)``, so the same revision
  and parameters always produce identical bytes.
"""
import json
import operator
import os
import sqlite3
import tempfile
from pathlib import Path

from .common import ident, number, read, require, rows
from . import warehouse

JSON_LIMIT = 32_000_000        # groups file at most 32 MB
GROUP_LIMIT = 10_000           # groups per file; the pack lists groups x candidates
GROUP_QUERY_LIMIT = 200_000    # queries listed in one group
TOTAL_QUERY_LIMIT = 200_000    # grouped queries per file
UNIT_LIMIT = 2_000_000         # coverage units (groups x candidate systems)

TOP_LIMIT = 100
BUDGET_LIMIT = 500
FLOOR_LIMIT = 20

RELEVANT = 'relevant'
IRRELEVANT = 'irrelevant'
UNJUDGED = 'unjudged'

_CELL_BATCH = 5000

_TEMP_SCHEMA = """
CREATE TEMP TABLE groups(
  grp TEXT PRIMARY KEY, query_count INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID;
CREATE TEMP TABLE grouped(
  query TEXT PRIMARY KEY, grp TEXT NOT NULL) WITHOUT ROWID;
CREATE TEMP TABLE anchor_queries(query TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TEMP TABLE cells(
  query TEXT NOT NULL, grp TEXT NOT NULL, system TEXT NOT NULL, diff INTEGER NOT NULL,
  PRIMARY KEY(query, system)) WITHOUT ROWID;
CREATE TEMP TABLE units(
  grp TEXT NOT NULL, system TEXT NOT NULL, candidate_count INTEGER NOT NULL,
  cov INTEGER NOT NULL, qualified INTEGER NOT NULL,
  PRIMARY KEY(grp, system)) WITHOUT ROWID;
CREATE TEMP TABLE keys(
  query TEXT PRIMARY KEY, fill INTEGER NOT NULL, first INTEGER NOT NULL,
  total INTEGER NOT NULL, diff INTEGER NOT NULL) WITHOUT ROWID;
CREATE TEMP TABLE selected(query TEXT PRIMARY KEY) WITHOUT ROWID;
"""


def _load_groups(con, path):
    """Validate the groups file into the TEMP tables, without a Python-side index.

    Groups and their queries are streamed into TEMP tables whose primary keys are the
    uniqueness constraints (a group id may appear once, a query may belong to exactly
    one group).  The parsed JSON list is released before the warehouse scan starts.
    """
    data, _ = read(path, JSON_LIMIT)
    require(data, 'groups')
    listed = rows(data['groups'], GROUP_LIMIT)
    for group in listed:
        require(group, 'id queries')
        gid = ident(group['id'])
        try:
            con.execute('INSERT INTO groups VALUES(?, 0)', (gid,))
        except sqlite3.IntegrityError:
            raise ValueError('duplicate group id: ' + gid) from None
        for value in rows(group['queries'], GROUP_QUERY_LIMIT):
            qid = ident(value)
            try:
                con.execute('INSERT INTO grouped VALUES(?, ?)', (qid, gid))
            except sqlite3.IntegrityError:
                raise ValueError('query belongs to more than one group: ' + qid) from None
    del data, listed
    con.execute('UPDATE groups SET query_count = '
                '(SELECT COUNT(*) FROM grouped g WHERE g.grp = groups.grp)')
    if con.execute('SELECT COUNT(*) FROM grouped').fetchone()[0] > TOTAL_QUERY_LIMIT:
        raise ValueError('too many grouped queries')
    sizes = con.execute('SELECT grp, query_count FROM groups ORDER BY grp').fetchall()
    _verify_groups(con)
    return sizes


def _verify_groups(con):
    """The partition must name exactly the warehouse's queries, no more and no fewer."""
    unknown = con.execute('''SELECT g.query FROM grouped g
        LEFT JOIN queries q ON q.query = g.query
        WHERE q.query IS NULL ORDER BY g.query LIMIT 1''').fetchone()
    if unknown is not None:
        raise ValueError('groups reference unknown query: ' + unknown[0])
    missing = con.execute('''SELECT q.query FROM queries q
        LEFT JOIN grouped g ON g.query = q.query
        WHERE g.query IS NULL ORDER BY q.query LIMIT 1''').fetchone()
    if missing is not None:
        raise ValueError('query missing from groups: ' + missing[0])


def _state_bytes(docs, status, prefix):
    """Tri-state codes (0 unjudged, 1 relevant, 2 irrelevant) of the truncated run."""
    return bytes(status.get(doc, 0) for doc in docs[:prefix])


def _state_name(code):
    return RELEVANT if code == 1 else (IRRELEVANT if code == 2 else UNJUDGED)


def _diff_positions(left, right):
    shared = min(len(left), len(right))
    found = [index for index in range(shared) if left[index] != right[index]]
    found.extend(range(shared, max(len(left), len(right))))
    return found


_NE = operator.ne


def _diff_count(left, right):
    """Number of differing tri-state positions, counting a missing side as differing."""
    shared = min(len(left), len(right))
    return sum(map(_NE, left[:shared], right[:shared])) + abs(len(left) - len(right))


def _scan(con, anchor, prefix):
    """Stream comparisons and stage one divergence cell per differing candidate."""
    con.execute('INSERT INTO anchor_queries SELECT query FROM runs WHERE system = ?', (anchor,))
    cursor = con.execute('''SELECT r.query, g.grp, r.system, r.docs_json
        FROM anchor_queries a JOIN runs r ON r.query = a.query
        JOIN grouped g ON g.query = r.query ORDER BY r.query, r.system''')
    judgments = con.cursor()
    current, grp, status, states, pending = None, None, None, {}, []
    compared = 0

    def flush():
        anchor_states = states.get(anchor)
        if anchor_states is None:
            return
        for system, candidate_states in states.items():
            if system == anchor or candidate_states == anchor_states:
                continue
            pending.append((current, grp, system,
                            _diff_count(anchor_states, candidate_states)))
            if len(pending) >= _CELL_BATCH:
                con.executemany('INSERT INTO cells VALUES(?, ?, ?, ?)', pending)
                pending.clear()

    for qid, group, system, payload in cursor:
        if qid != current:
            if current is not None:
                flush()
                if len(states) > 1:
                    compared += 1
            current, grp, states = qid, group, {}
            status = {doc: (1 if grade > 0 else 2) for doc, grade in judgments.execute(
                'SELECT doc, grade FROM judgments WHERE query = ?', (qid,))}
        states[system] = _state_bytes(json.loads(payload), status, prefix)
    if current is not None:
        flush()
        if len(states) > 1:
            compared += 1
    if pending:
        con.executemany('INSERT INTO cells VALUES(?, ?, ?, ?)', pending)
    con.execute('CREATE INDEX cells_unit_index ON cells(grp, system, query)')
    return compared


def _units(con, anchor, floor):
    """Materialize every (group, candidate system) unit and the per-query keys.

    ``fill`` counts qualified units at coverage ``floor - 1`` (one more pick meets
    them), ``first`` counts qualified units at coverage 0, ``total`` counts every
    unit the query can add a sample to, and ``diff`` sums its position differences.
    The full group x candidate cross-product is reported, so its size is capped
    explicitly instead of silently truncating the unit list.
    """
    groups, candidates = con.execute(
        'SELECT (SELECT COUNT(*) FROM groups), (SELECT COUNT(*) FROM systems)').fetchone()
    if groups * (candidates - 1) > UNIT_LIMIT:
        raise ValueError('too many coverage units: %d groups x %d candidates'
                         % (groups, candidates - 1))
    con.execute('''INSERT INTO units(grp, system, candidate_count, cov, qualified)
        SELECT g.grp, s.system, COALESCE(c.total, 0), 0,
               CASE WHEN COALESCE(c.total, 0) >= ? THEN 1 ELSE 0 END
        FROM groups g CROSS JOIN systems s
        LEFT JOIN (SELECT grp, system, COUNT(*) AS total FROM cells GROUP BY grp, system) c
               ON c.grp = g.grp AND c.system = s.system
        WHERE s.system <> ?''', (floor, anchor))
    con.execute('''INSERT INTO keys(query, fill, first, total, diff)
        SELECT c.query,
               SUM(CASE WHEN u.qualified = 1 AND cov = ? THEN 1 ELSE 0 END),
               SUM(u.qualified), COUNT(*), SUM(c.diff)
        FROM cells c JOIN units u ON u.grp = c.grp AND u.system = c.system
        GROUP BY c.query''', (floor - 1,))
    con.execute('''CREATE INDEX keys_order
        ON keys(fill DESC, first DESC, total DESC, diff DESC, query ASC)''')
    return con.execute('SELECT COUNT(*) FROM keys').fetchone()[0]


def _adjust(con, column, delta, grp, system):
    con.execute('UPDATE keys SET %s = %s + ? WHERE query IN '
                '(SELECT query FROM cells WHERE grp = ? AND system = ?)' % (column, column),
                (delta, grp, system))


def _select(con, budget, floor):
    """Greedy coverage selection; keys move only on unit threshold crossings."""
    chosen = []
    for _ in range(budget):
        row = con.execute('''SELECT query FROM keys
            ORDER BY fill DESC, first DESC, total DESC, diff DESC, query ASC
            LIMIT 1''').fetchone()
        if row is None:
            break
        qid = row[0]
        con.execute('DELETE FROM keys WHERE query = ?', (qid,))
        con.execute('INSERT INTO selected VALUES(?)', (qid,))
        chosen.append(qid)
        for grp, system in con.execute(
                'SELECT grp, system FROM cells WHERE query = ? ORDER BY grp, system',
                (qid,)).fetchall():
            qualified, cov = con.execute(
                'SELECT qualified, cov FROM units WHERE grp = ? AND system = ?',
                (grp, system)).fetchone()
            updated = cov + 1
            con.execute('UPDATE units SET cov = ? WHERE grp = ? AND system = ?',
                        (updated, grp, system))
            if not qualified:
                continue
            if cov == 0:  # the unit gains its first selected sample
                _adjust(con, 'first', -1, grp, system)
            change = ((1 if updated == floor - 1 else 0)
                      - (1 if cov == floor - 1 else 0))
            if change:  # the unit enters or leaves the "one pick fills it" state
                _adjust(con, 'fill', change, grp, system)
    return chosen


def _unit_rows(con, floor):
    for grp, system, count, cov, qualified in con.execute(
            '''SELECT grp, system, candidate_count, cov, qualified FROM units
               ORDER BY grp, system'''):
        yield {'group': grp, 'system': system, 'candidate_count': count,
               'target': floor, 'selected': cov, 'qualified': bool(qualified),
               'met': bool(qualified) and cov >= floor}


def _sample_rows(con, anchor, prefix):
    judgments = con.cursor()
    runs = con.cursor()
    for (qid,) in con.execute('SELECT query FROM selected ORDER BY query').fetchall():
        status = {doc: (1 if grade > 0 else 2) for doc, grade in judgments.execute(
            'SELECT doc, grade FROM judgments WHERE query = ?', (qid,))}
        ranked = [(system, json.loads(payload)) for system, payload in runs.execute(
            'SELECT system, docs_json FROM runs WHERE query = ? ORDER BY system', (qid,))]
        anchor_docs = next((docs for system, docs in ranked if system == anchor), None)
        anchor_states = _state_bytes(anchor_docs, status, prefix)
        systems = []
        for system, docs in ranked:
            if system == anchor:
                continue
            candidate_states = _state_bytes(docs, status, prefix)
            if candidate_states == anchor_states:
                continue
            systems.append({
                'system': system,
                'anchor_docs': anchor_docs[:len(anchor_states)],
                'anchor_states': [_state_name(code) for code in anchor_states],
                'candidate_docs': docs[:len(candidate_states)],
                'candidate_states': [_state_name(code) for code in candidate_states],
                'diff_positions': _diff_positions(anchor_states, candidate_states)})
        grp = con.execute('SELECT grp FROM grouped WHERE query = ?', (qid,)).fetchone()[0]
        yield {'query': qid, 'group': grp, 'systems': systems}


def _emit(value, level, stream):
    """Write one JSON value exactly as ``json.dump(indent=2, sort_keys=True)`` would."""
    if isinstance(value, dict):
        if not value:
            stream.write('{}')
            return
        items = sorted(value.items())
        stream.write('{\n')
        for index, (key, item) in enumerate(items):
            stream.write('  ' * (level + 1) + json.dumps(key, ensure_ascii=False) + ': ')
            _emit(item, level + 1, stream)
            stream.write(',\n' if index < len(items) - 1 else '\n')
        stream.write('  ' * level + '}')
    elif isinstance(value, list):
        if not value:
            stream.write('[]')
            return
        stream.write('[\n')
        for index, item in enumerate(value):
            stream.write('  ' * (level + 1))
            _emit(item, level + 1, stream)
            stream.write(',\n' if index < len(value) - 1 else '\n')
        stream.write('  ' * level + ']')
    else:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _stream_array(stream, level, items):
    stream.write('[\n')
    first = True
    for item in items:
        if not first:
            stream.write(',\n')
        stream.write('  ' * (level + 1))
        _emit(item, level + 1, stream)
        first = False
    if not first:
        stream.write('\n')
    stream.write('  ' * level + ']')


def _write_report(con, stream, header, floor, anchor, prefix):
    keys = sorted(list(header) + ['samples', 'units'])
    stream.write('{\n')
    for index, key in enumerate(keys):
        stream.write('  ' + json.dumps(key, ensure_ascii=False) + ': ')
        if key == 'units':
            _stream_array(stream, 1, _unit_rows(con, floor))
        elif key == 'samples':
            _stream_array(stream, 1, _sample_rows(con, anchor, prefix))
        else:
            _emit(header[key], 1, stream)
        stream.write(',\n' if index < len(keys) - 1 else '\n')
    stream.write('}\n')


def review(db, groups_path, anchor, top, budget, floor, output):
    """Build and atomically publish one arbitration review pack."""
    anchor = ident(anchor)
    top = number(top, 1, TOP_LIMIT)
    budget = number(budget, 1, BUDGET_LIMIT)
    floor = number(floor, 1, FLOOR_LIMIT)
    dest = Path(output)
    if dest.is_symlink():
        raise ValueError('output cannot be a symlink')
    resolved = dest.resolve()
    if resolved == Path(db).resolve() or resolved == Path(groups_path).resolve():
        raise ValueError('input and output must differ')
    con = warehouse._connect(db, 'ro')
    temporary = None
    try:
        con.execute('BEGIN')
        try:
            meta = warehouse._meta(con)
            for statement in _TEMP_SCHEMA.split(';'):
                if statement.strip():
                    con.execute(statement)
            k, revision = meta['k'], meta['revision']
            prefix = min(k, top)
            if con.execute('SELECT 1 FROM systems WHERE system = ?', (anchor,)).fetchone() is None:
                raise ValueError('unknown anchor system: ' + anchor)
            candidates = [system for (system,) in con.execute(
                'SELECT system FROM systems ORDER BY system') if system != anchor]
            sizes = _load_groups(con, groups_path)
            compared = _scan(con, anchor, prefix)
            _units(con, anchor, floor)
            chosen = _select(con, budget, floor)

            unit_count, qualified, met = con.execute('''SELECT COUNT(*),
                COALESCE(SUM(qualified), 0),
                COALESCE(SUM(CASE WHEN qualified = 1 AND cov >= ? THEN 1 ELSE 0 END), 0)
                FROM units''', (floor,)).fetchone()
            cells = con.execute('SELECT COUNT(*) FROM cells').fetchone()[0]
            divergence_queries = con.execute(
                'SELECT COUNT(DISTINCT query) FROM cells').fetchone()[0]
            summary = {
                'query_count': con.execute('SELECT COUNT(*) FROM grouped').fetchone()[0],
                'group_count': len(sizes),
                'compared_queries': compared,
                'divergence_queries': divergence_queries,
                'divergence_cells': cells,
                'divergence_units': unit_count,
                'qualified_units': qualified,
                'met_units': met,
                'selected_queries': len(chosen),
                'budget': budget,
                'coverage_complete': met == qualified,
            }
            header = {
                'schema_version': 1,
                'report': 'review-pack',
                'revision': revision,
                'k': k,
                'anchor': anchor,
                'top': top,
                'budget': budget,
                'cell_floor': floor,
                'groups': [{'id': gid, 'query_count': count}
                           for gid, count in sorted(sizes)],
                'systems': {'anchor': anchor, 'candidates': candidates},
                'summary': summary,
            }
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=dest.parent,
                                             delete=False, newline='\n') as stream:
                temporary = stream.name
                _write_report(con, stream, header, floor, anchor, prefix)
            con.execute('COMMIT')
        except Exception:
            con.execute('ROLLBACK')
            raise
    except Exception:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        raise
    finally:
        con.close()
    try:
        os.replace(temporary, dest)
    except Exception:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        raise
    return {'revision': revision, 'anchor': anchor, 'top': top, 'budget': budget,
            'cell_floor': floor, 'selected_queries': len(chosen),
            'coverage_complete': summary['coverage_complete']}
