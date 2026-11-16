"""Revisioned SQLite evaluation warehouse: batch facts, materialized metrics, sparse reports.

See docs/contract.md (warehouse sections) for the business contract. Design notes:

- One SQLite transaction per batch covers facts, materialized scores, aggregates and the
  revision counter, so any failure (including the last JSONL line) rolls back completely.
- Batch records are streamed line by line (<=1 MiB each) into TEMP staging tables whose
  primary keys enforce intra-batch uniqueness; sorting/dedup happens in SQL, never by
  loading the whole batch or warehouse into Python lists.
- scores holds exactly one row per explicit run (no placeholder pairs for missing runs).
  aggregates holds per-system sums over those rows; macro denominators are warehouse-wide
  counters in meta (all queries for precision, R>0 queries for the other three), so a
  judgment revision that moves a query across R=0 changes every system's eligibility
  without touching systems that have no run on that query.
- Rescoring is driven by the dirty set only: queries whose judgments were replaced rescore
  all of their final explicit runs, run upserts rescore their own pair, drops remove their
  pair. Unrelated pairs are never re-evaluated.
"""
import json, math, os, sqlite3
from pathlib import Path
from urllib.parse import quote
from .common import require, ident, number, rows, save
from .metrics import evaluate

LINE_LIMIT = 1024 * 1024  # one JSONL record at most 1 MiB

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE queries(query TEXT PRIMARY KEY, relevant_total INTEGER NOT NULL, judgment_count INTEGER NOT NULL);
CREATE TABLE judgments(query TEXT NOT NULL, doc TEXT NOT NULL, grade INTEGER NOT NULL,
  PRIMARY KEY(query, doc)) WITHOUT ROWID;
CREATE TABLE systems(system TEXT PRIMARY KEY);
CREATE TABLE runs(system TEXT NOT NULL, query TEXT NOT NULL, docs_json TEXT NOT NULL,
  PRIMARY KEY(system, query)) WITHOUT ROWID;
CREATE INDEX runs_by_query ON runs(query);
CREATE TABLE scores(system TEXT NOT NULL, query TEXT NOT NULL,
  precision REAL NOT NULL, recall REAL, ap REAL, ndcg REAL,
  hits INTEGER NOT NULL, retrieved INTEGER NOT NULL, judged INTEGER NOT NULL,
  unjudged_json TEXT NOT NULL, PRIMARY KEY(system, query)) WITHOUT ROWID;
CREATE INDEX scores_by_query ON scores(query);
CREATE TABLE aggregates(system TEXT PRIMARY KEY, queries_with_runs INTEGER NOT NULL,
  sum_precision REAL NOT NULL, sum_recall REAL NOT NULL, sum_ap REAL NOT NULL, sum_ndcg REAL NOT NULL);
"""

STAGE = """
CREATE TEMP TABLE stage_query(qid TEXT PRIMARY KEY, relevant_total INTEGER NOT NULL, judgment_count INTEGER NOT NULL);
CREATE TEMP TABLE stage_judgment(qid TEXT NOT NULL, doc TEXT NOT NULL, grade INTEGER NOT NULL,
  PRIMARY KEY(qid, doc)) WITHOUT ROWID;
CREATE TEMP TABLE stage_system(sid TEXT PRIMARY KEY);
CREATE TEMP TABLE stage_run(system TEXT NOT NULL, query TEXT NOT NULL, is_drop INTEGER NOT NULL, docs_json TEXT,
  PRIMARY KEY(system, query)) WITHOUT ROWID;
"""

META_KEYS = ('k', 'revision', 'total_queries', 'relevant_queries')


def _connect(db, mode):
    """Open an existing warehouse without ever creating a new file (mode ro/rw)."""
    uri = 'file:' + quote(os.path.abspath(str(db))) + '?mode=' + mode
    con = sqlite3.connect(uri, uri=True, isolation_level=None)
    con.execute('PRAGMA busy_timeout=5000')
    con.execute('PRAGMA temp_store=FILE')
    return con


def _meta(con):
    try:
        pairs = dict(con.execute('SELECT key, value FROM meta'))
    except sqlite3.Error:
        raise ValueError('not a retrieval-eval warehouse: meta table missing') from None
    if pairs.get('schema') != 1 or any(type(pairs.get(key)) is not int for key in META_KEYS):
        raise ValueError('not a retrieval-eval warehouse: bad meta contents')
    return pairs


def init(db, k):
    """Create a new warehouse file; fails cleanly (no leftover file) if anything goes wrong."""
    k = number(k, 1, 1000)
    path = Path(db)
    if os.path.lexists(path):
        raise ValueError('database already exists: ' + str(db))
    if str(path.parent) and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)
    try:
        con = sqlite3.connect(str(path), isolation_level=None)
        try:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA busy_timeout=5000')
            con.execute('BEGIN')
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    con.execute(statement)
            con.executemany('INSERT INTO meta VALUES(?, ?)',
                            [('schema', 1), ('k', k), ('revision', 0), ('total_queries', 0), ('relevant_queries', 0)])
            con.execute('COMMIT')
        finally:
            con.close()
    except Exception:
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(str(path) + suffix)
            except OSError:
                pass
        raise
    return {'k': k, 'revision': 0}


def _parse_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result

    def invalid(value):
        raise ValueError('nonfinite JSON number: ' + value)

    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('nonfinite JSON number: ' + value)
        return result

    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid, parse_float=finite)


def _insert(con, sql, args, what):
    try:
        con.execute(sql, args)
    except sqlite3.IntegrityError:
        raise ValueError('duplicate ' + what) from None


def _stage_record(con, obj):
    if not isinstance(obj, dict):
        raise ValueError('expected a JSON object')
    kind = obj.get('kind')
    if kind == 'query':
        require(obj, 'kind id judgments')
        qid = ident(obj['id'])
        items, seen = [], set()
        for judgment in rows(obj['judgments'], 1000):
            require(judgment, 'doc grade')
            doc, grade = ident(judgment['doc']), number(judgment['grade'], 0, 3)
            if doc in seen:
                raise ValueError('duplicate document judgment')
            seen.add(doc)
            items.append((qid, doc, grade))
        _insert(con, 'INSERT INTO stage_query VALUES(?, ?, ?)',
                (qid, sum(1 for _, _, grade in items if grade > 0), len(items)), 'query in batch: ' + qid)
        con.executemany('INSERT INTO stage_judgment VALUES(?, ?, ?)', items)
    elif kind == 'system':
        require(obj, 'kind id')
        sid = ident(obj['id'])
        _insert(con, 'INSERT INTO stage_system VALUES(?)', (sid,), 'system in batch: ' + sid)
    elif kind == 'run':
        require(obj, 'kind system query docs')
        sid, qid = ident(obj['system']), ident(obj['query'])
        docs = [ident(doc) for doc in rows(obj['docs'], 1000)]
        if len(set(docs)) != len(docs):
            raise ValueError('duplicate ranked document')
        _insert(con, 'INSERT INTO stage_run VALUES(?, ?, 0, ?)',
                (sid, qid, json.dumps(docs, ensure_ascii=False, separators=(',', ':'))),
                'run for (system, query) in batch: ' + sid + ' / ' + qid)
    elif kind == 'drop_run':
        require(obj, 'kind system query')
        sid, qid = ident(obj['system']), ident(obj['query'])
        _insert(con, 'INSERT INTO stage_run VALUES(?, ?, 1, NULL)', (sid, qid),
                'run for (system, query) in batch: ' + sid + ' / ' + qid)
    else:
        raise ValueError('unknown record kind: ' + repr(kind))


def _stream_batch(con, source):
    count = 0
    with open(source, 'rb') as stream:
        while True:
            chunk = stream.readline(LINE_LIMIT + 2)
            if not chunk:
                return count
            line = chunk[:-1] if chunk.endswith(b'\n') else chunk
            count += 1
            try:
                if len(line) > LINE_LIMIT:
                    raise ValueError('line exceeds 1 MiB')
                if not line.strip():
                    raise ValueError('blank line')
                _stage_record(con, _parse_json(line.decode('utf-8')))
            except ValueError as error:
                raise ValueError('line %d: %s' % (count, error)) from None


def _validate_references(con):
    row = con.execute('''SELECT sr.system FROM stage_run sr
        WHERE NOT EXISTS(SELECT 1 FROM systems s WHERE s.system = sr.system)
          AND NOT EXISTS(SELECT 1 FROM stage_system ss WHERE ss.sid = sr.system) LIMIT 1''').fetchone()
    if row:
        raise ValueError('unknown system: ' + row[0])
    row = con.execute('''SELECT sr.query FROM stage_run sr
        WHERE NOT EXISTS(SELECT 1 FROM queries q WHERE q.query = sr.query)
          AND NOT EXISTS(SELECT 1 FROM stage_query sq WHERE sq.qid = sr.query) LIMIT 1''').fetchone()
    if row:
        raise ValueError('unknown query: ' + row[0])


def _capture_old_scores(con):
    con.execute('''CREATE TEMP TABLE old_scores AS
        SELECT system, query, precision, recall, ap, ndcg FROM scores
        WHERE query IN (SELECT qid FROM stage_query)
        UNION
        SELECT system, query, precision, recall, ap, ndcg FROM scores
        WHERE (system, query) IN (SELECT system, query FROM stage_run)''')
    con.execute('CREATE INDEX old_scores_pair ON old_scores(system, query)')


def _apply_facts(con):
    con.execute('INSERT OR IGNORE INTO systems SELECT sid FROM stage_system')
    con.execute('''INSERT OR IGNORE INTO aggregates
        (system, queries_with_runs, sum_precision, sum_recall, sum_ap, sum_ndcg)
        SELECT sid, 0, 0.0, 0.0, 0.0, 0.0 FROM stage_system''')
    new_queries = con.execute('''SELECT COUNT(*) FROM stage_query sq
        WHERE NOT EXISTS(SELECT 1 FROM queries q WHERE q.query = sq.qid)''').fetchone()[0]
    old_relevant = con.execute('''SELECT COUNT(*) FROM queries
        WHERE relevant_total > 0 AND query IN (SELECT qid FROM stage_query)''').fetchone()[0]
    new_relevant = con.execute('SELECT COUNT(*) FROM stage_query WHERE relevant_total > 0').fetchone()[0]
    con.execute('''INSERT INTO queries(query, relevant_total, judgment_count)
        SELECT qid, relevant_total, judgment_count FROM stage_query WHERE 1
        ON CONFLICT(query) DO UPDATE SET relevant_total = excluded.relevant_total,
            judgment_count = excluded.judgment_count''')
    con.execute('DELETE FROM judgments WHERE query IN (SELECT qid FROM stage_query)')
    con.execute('INSERT INTO judgments SELECT qid, doc, grade FROM stage_judgment')
    con.execute('DELETE FROM runs WHERE (system, query) IN (SELECT system, query FROM stage_run WHERE is_drop = 1)')
    con.execute('''INSERT INTO runs(system, query, docs_json)
        SELECT system, query, docs_json FROM stage_run WHERE is_drop = 0 AND 1
        ON CONFLICT(system, query) DO UPDATE SET docs_json = excluded.docs_json''')
    con.execute('DELETE FROM scores WHERE query IN (SELECT qid FROM stage_query)')
    con.execute('DELETE FROM scores WHERE (system, query) IN (SELECT system, query FROM stage_run)')
    return new_queries, new_relevant - old_relevant


def _rescore(con, k):
    """Score each dirty pair exactly once and accumulate per-system aggregate deltas."""
    deltas = {}

    def touch(system):
        delta = deltas.get(system)
        if delta is None:
            delta = deltas[system] = [0, 0.0, 0.0, 0.0, 0.0]
        return delta

    def subtract(delta, old):
        delta[1] -= old[0]
        for index, value in ((2, old[1]), (3, old[2]), (4, old[3])):
            if value is not None:
                delta[index] -= value

    def add_metrics(delta, metrics):
        delta[1] += metrics['precision_at_k']
        for index, key in ((2, 'recall_at_k'), (3, 'average_precision_at_k'), (4, 'ndcg_at_k')):
            if metrics[key] is not None:
                delta[index] += metrics[key]

    def score_pair(system, qid, judgments, docs_json, old):
        metrics = evaluate(judgments, json.loads(docs_json), k)
        con.execute('''INSERT INTO scores
            (system, query, precision, recall, ap, ndcg, hits, retrieved, judged, unjudged_json)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (system, qid, metrics['precision_at_k'], metrics['recall_at_k'],
             metrics['average_precision_at_k'], metrics['ndcg_at_k'], metrics['hits_at_k'],
             metrics['retrieved_at_k'], metrics['judged_at_k'],
             json.dumps(metrics['unjudged_docs_at_k'], ensure_ascii=False, separators=(',', ':'))))
        delta = touch(system)
        if old is None:
            delta[0] += 1
        else:
            subtract(delta, old)
        add_metrics(delta, metrics)

    # Queries whose judgments were replaced: rescore every explicit run they finally have.
    current_qid, judgments = None, None
    for qid, system, docs_json, old_p, old_r, old_a, old_n in con.execute(
            '''SELECT r.query, r.system, r.docs_json, o.precision, o.recall, o.ap, o.ndcg
               FROM runs r LEFT JOIN old_scores o ON o.system = r.system AND o.query = r.query
               WHERE r.query IN (SELECT qid FROM stage_query) ORDER BY r.query, r.system'''):
        if qid != current_qid:
            current_qid = qid
            judgments = dict(con.execute('SELECT doc, grade FROM judgments WHERE query = ?', (qid,)))
        old = None if old_p is None else (old_p, old_r, old_a, old_n)
        score_pair(system, qid, judgments, docs_json, old)

    # Run upserts whose query kept its judgments: rescore exactly that pair.
    current_qid, judgments = None, None
    for system, qid, docs_json, old_p, old_r, old_a, old_n in con.execute(
            '''SELECT sr.system, sr.query, sr.docs_json, o.precision, o.recall, o.ap, o.ndcg
               FROM stage_run sr LEFT JOIN old_scores o ON o.system = sr.system AND o.query = sr.query
               WHERE sr.is_drop = 0 AND NOT EXISTS(SELECT 1 FROM stage_query sq WHERE sq.qid = sr.query)
               ORDER BY sr.query, sr.system'''):
        if qid != current_qid:
            current_qid = qid
            judgments = dict(con.execute('SELECT doc, grade FROM judgments WHERE query = ?', (qid,)))
        old = None if old_p is None else (old_p, old_r, old_a, old_n)
        score_pair(system, qid, judgments, docs_json, old)

    # Dropped runs withdraw their old contribution; unknown pairs are no-ops.
    for system, qid, old_p, old_r, old_a, old_n in con.execute(
            '''SELECT sr.system, sr.query, o.precision, o.recall, o.ap, o.ndcg
               FROM stage_run sr JOIN old_scores o ON o.system = sr.system AND o.query = sr.query
               WHERE sr.is_drop = 1 ORDER BY sr.system, sr.query'''):
        delta = touch(system)
        delta[0] -= 1
        subtract(delta, (old_p, old_r, old_a, old_n))

    for system, delta in deltas.items():
        con.execute('''INSERT INTO aggregates
            (system, queries_with_runs, sum_precision, sum_recall, sum_ap, sum_ndcg)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(system) DO UPDATE SET
                queries_with_runs = queries_with_runs + excluded.queries_with_runs,
                sum_precision = sum_precision + excluded.sum_precision,
                sum_recall = sum_recall + excluded.sum_recall,
                sum_ap = sum_ap + excluded.sum_ap,
                sum_ndcg = sum_ndcg + excluded.sum_ndcg''', (system, *delta))


def apply(db, source):
    """Apply one JSONL batch atomically; returns {'records', 'revision'} after commit."""
    con = _connect(db, 'rw')
    try:
        con.execute('BEGIN IMMEDIATE')
        try:
            meta = _meta(con)
            for statement in STAGE.split(';'):
                if statement.strip():
                    con.execute(statement)
            count = _stream_batch(con, source)
            if count == 0:
                con.execute('ROLLBACK')
                return {'records': 0, 'revision': meta['revision']}
            _validate_references(con)
            _capture_old_scores(con)
            new_queries, relevant_delta = _apply_facts(con)
            _rescore(con, meta['k'])
            con.execute("UPDATE meta SET value = value + 1 WHERE key = 'revision'")
            con.execute("UPDATE meta SET value = value + ? WHERE key = 'total_queries'", (new_queries,))
            con.execute("UPDATE meta SET value = value + ? WHERE key = 'relevant_queries'", (relevant_delta,))
            con.execute('COMMIT')
            return {'records': count, 'revision': meta['revision'] + 1}
        except Exception:
            con.execute('ROLLBACK')
            raise
    finally:
        con.close()


def _macro(total, eligible):
    return {'value': total / eligible if eligible else None, 'eligible_queries': eligible}


def _system_entry(con, sid, total_queries, relevant_queries):
    qwr, sum_p, sum_r, sum_a, sum_n = con.execute(
        'SELECT queries_with_runs, sum_precision, sum_recall, sum_ap, sum_ndcg FROM aggregates WHERE system = ?',
        (sid,)).fetchone()
    return {'system': sid, 'query_count': total_queries, 'queries_with_runs': qwr,
            'macro': {'precision_at_k': _macro(sum_p, total_queries),
                      'recall_at_k': _macro(sum_r, relevant_queries),
                      'average_precision_at_k': _macro(sum_a, relevant_queries),
                      'ndcg_at_k': _macro(sum_n, relevant_queries)}}


def _query_rows(con, sid):
    for qid, relevant, prec, rec, ap, ndcg, hits, retrieved, judged, unjudged in con.execute(
            '''SELECT q.query, q.relevant_total, s.precision, s.recall, s.ap, s.ndcg,
                      s.hits, s.retrieved, s.judged, s.unjudged_json
               FROM queries q LEFT JOIN scores s ON s.system = ? AND s.query = q.query
               ORDER BY q.query''', (sid,)):
        if prec is None:  # no explicit run: synthesize the empty ranking under current judgments
            eligible = relevant > 0
            yield {'query': qid, 'has_run': False, 'precision_at_k': 0.0,
                   'recall_at_k': 0.0 if eligible else None,
                   'average_precision_at_k': 0.0 if eligible else None,
                   'ndcg_at_k': 0.0 if eligible else None,
                   'relevant_total': relevant, 'hits_at_k': 0, 'retrieved_at_k': 0,
                   'judged_at_k': 0, 'unjudged_docs_at_k': []}
        else:
            yield {'query': qid, 'has_run': True, 'precision_at_k': prec, 'recall_at_k': rec,
                   'average_precision_at_k': ap, 'ndcg_at_k': ndcg, 'relevant_total': relevant,
                   'hits_at_k': hits, 'retrieved_at_k': retrieved, 'judged_at_k': judged,
                   'unjudged_docs_at_k': json.loads(unjudged)}


def report(db, output, system=None):
    """Write the deterministic JSON report built only from persisted state and sparse defaults."""
    if system is not None:
        ident(system)
    con = _connect(db, 'ro')
    try:
        con.execute('BEGIN')
        try:
            meta = _meta(con)
            total_queries, relevant_queries = meta['total_queries'], meta['relevant_queries']
            if system is not None:
                if not con.execute('SELECT 1 FROM systems WHERE system = ?', (system,)).fetchone():
                    raise ValueError('unknown system: ' + system)
                entry = _system_entry(con, system, total_queries, relevant_queries)
                entry['queries'] = list(_query_rows(con, system))
                systems = [entry]
            else:
                ids = [row[0] for row in con.execute('SELECT system FROM systems ORDER BY system')]
                systems = [_system_entry(con, sid, total_queries, relevant_queries) for sid in ids]
            result = {'schema_version': 1, 'k': meta['k'], 'revision': meta['revision'],
                      'query_count': total_queries, 'systems': systems}
            con.execute('COMMIT')
        except Exception:
            con.execute('ROLLBACK')
            raise
    finally:
        con.close()
    save(output, result)
    return result
