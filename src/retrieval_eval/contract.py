"""Offline production contract upgrade review: field diffs and per-consumer impact.

See docs/contract.md (contract impact section) for the business contract. Design notes:

- Each input file is parsed once, validated and folded into compact tuples; the parsed
  JSON tree is released before the next file is read, so peak memory tracks the logical
  model (fields + reads), never the JSON text or a second copy of it.
- Consumer impact joins field diffs with a path -> readers index, so the work grows
  with changed paths plus matching reads; no consumer x field Cartesian product is
  ever materialized.
- The report is a pure function of the validated model: diffs sorted by path,
  consumers sorted by id, issues sorted by (path, reason). Input ordering, key order
  and formatting never change the output bytes.
"""
from .common import require, ident, rows, read, save

TYPES = frozenset(['string', 'integer', 'number', 'boolean', 'object', 'array'])
_CANONICAL = {t: t for t in TYPES}  # one shared str object per known type
JSON_LIMIT = 64_000_000   # each input file at most 64 MB
FIELD_LIMIT = 100_000     # fields per version
CONSUMER_LIMIT = 20_000   # consumers per file
READ_LIMIT = 1_000        # reads per consumer

REMOVED_REQUIRED = 'removed_required_read'
REMOVED_OPTIONAL = 'removed_optional_read'
TYPE_NOT_ACCEPTED = 'type_not_accepted'
NULL_NOT_ACCEPTED = 'null_not_accepted'
BLOCKING = frozenset([REMOVED_REQUIRED, TYPE_NOT_ACCEPTED, NULL_NOT_ACCEPTED])


def _boolean(value):
    if type(value) is not bool:
        raise ValueError('expected boolean')
    return value


def _type(value):
    if not isinstance(value, str) or value not in TYPES:
        raise ValueError('unknown type: ' + repr(value))
    return _CANONICAL[value]


def _version(data):
    """Validate one contract version file into {path: (type, required, nullable)}."""
    require(data, 'dataset version fields')
    dataset, version = ident(data['dataset']), ident(data['version'])
    fields = {}
    for field in rows(data['fields'], FIELD_LIMIT):
        require(field, 'path type required nullable')
        path = ident(field['path'])
        if path in fields:
            raise ValueError('duplicate field path: ' + path)
        fields[path] = (_type(field['type']), _boolean(field['required']),
                        _boolean(field['nullable']))
    return dataset, version, fields


def _read_decl(data):
    """Validate one read declaration into (path, (accepted_types, required, accepts_null))."""
    require(data, 'path accepted_types required accepts_null')
    path = ident(data['path'])
    accepted = rows(data['accepted_types'], len(TYPES))
    if not accepted:
        raise ValueError('accepted_types cannot be empty')
    if len(set(accepted)) != len(accepted):
        raise ValueError('duplicate accepted type for path: ' + path)
    return path, (tuple(sorted(_type(t) for t in accepted)),
                  _boolean(data['required']), _boolean(data['accepts_null']))


def _consumers(data):
    """Validate the consumers file into {consumer_id: {path: read}}."""
    require(data, 'dataset consumers')
    dataset = ident(data['dataset'])
    consumers = {}
    for consumer in rows(data['consumers'], CONSUMER_LIMIT):
        require(consumer, 'id reads')
        cid = ident(consumer['id'])
        if cid in consumers:
            raise ValueError('duplicate consumer: ' + cid)
        reads = {}
        for record in rows(consumer['reads'], READ_LIMIT):
            path, definition = _read_decl(record)
            if path in reads:
                raise ValueError('duplicate read path for consumer ' + cid + ': ' + path)
            reads[path] = definition
        consumers[cid] = reads
    return dataset, consumers


def _snapshot(field):
    return {'type': field[0], 'required': field[1], 'nullable': field[2]}


def _diff(baseline, candidate):
    """Complete per-path diff: added, removed, and type/nullability changes."""
    entries = []
    for path in sorted(set(baseline) | set(candidate)):
        base, cand = baseline.get(path), candidate.get(path)
        if base is None:
            entries.append({'path': path, 'kind': 'added', 'candidate': _snapshot(cand)})
        elif cand is None:
            entries.append({'path': path, 'kind': 'removed', 'baseline': _snapshot(base)})
        else:
            changes = {}
            if base[0] != cand[0]:
                changes['type'] = {'from': base[0], 'to': cand[0]}
            if base[2] != cand[2]:
                changes['nullable'] = {'from': base[2], 'to': cand[2]}
            if changes:
                entries.append({'path': path, 'kind': 'changed', 'changes': changes})
    return entries


def judge(entry, candidate, reader):
    """Issues raised by one candidate diff entry for one consumer read of its path.

    candidate: the candidate field tuple (type, required, nullable); reader:
    (accepted_types, required, accepts_null). Removal blocks a required read and
    only warns on an optional one; a candidate type outside the reader's accepted
    set blocks; accepts_null only matters when the field turned nullable.
    """
    path = entry['path']
    if entry['kind'] == 'removed':
        return [{'path': path, 'reason': REMOVED_REQUIRED if reader[1] else REMOVED_OPTIONAL}]
    issues = []
    if candidate[0] not in reader[0]:
        issues.append({'path': path, 'reason': TYPE_NOT_ACCEPTED, 'candidate_type': candidate[0]})
    if entry['kind'] == 'changed':
        nullable = entry['changes'].get('nullable')
        if nullable and not nullable['from'] and nullable['to'] and not reader[2]:
            issues.append({'path': path, 'reason': NULL_NOT_ACCEPTED})
    return issues


def review(dataset, baseline_version, candidate_version, baseline, candidate, consumers):
    """Build the deterministic report from validated compact structures."""
    diffs = _diff(baseline, candidate)
    readers = {}
    for cid, reads in consumers.items():
        for path, definition in reads.items():
            readers.setdefault(path, []).append((cid, definition))
    issues = {cid: [] for cid in consumers}
    for entry in diffs:
        cand = candidate.get(entry['path'])
        for cid, definition in readers.get(entry['path'], ()):
            issues[cid].extend(judge(entry, cand, definition))
    reports, counts = [], {'blocked': 0, 'warning': 0, 'compatible': 0}
    for cid in sorted(consumers):
        found = sorted(issues[cid], key=lambda issue: (issue['path'], issue['reason']))
        if any(issue['reason'] in BLOCKING for issue in found):
            status = 'blocked'
        elif found:
            status = 'warning'
        else:
            status = 'compatible'
        counts[status] += 1
        reports.append({'id': cid, 'status': status, 'issues': found})
    kinds = {'added': 0, 'removed': 0, 'changed': 0}
    for entry in diffs:
        kinds[entry['kind']] += 1
    return {'schema_version': 1, 'dataset': dataset,
            'baseline_version': baseline_version, 'candidate_version': candidate_version,
            'field_diffs': diffs, 'consumers': reports,
            'summary': {'consumers_total': len(reports),
                        'blocked': counts['blocked'], 'warning': counts['warning'],
                        'compatible': counts['compatible'],
                        'fields_added': kinds['added'], 'fields_removed': kinds['removed'],
                        'fields_changed': kinds['changed']}}


def impact(baseline_path, candidate_path, consumers_path, output):
    """Validate the three inputs, build the report and publish it atomically."""
    data, _ = read(baseline_path, JSON_LIMIT)
    dataset, baseline_version, baseline = _version(data)
    del data
    data, _ = read(candidate_path, JSON_LIMIT)
    candidate_dataset, candidate_version, candidate = _version(data)
    del data
    if candidate_dataset != dataset:
        raise ValueError('dataset mismatch: baseline %s vs candidate %s'
                         % (dataset, candidate_dataset))
    data, _ = read(consumers_path, JSON_LIMIT)
    consumers_dataset, consumers = _consumers(data)
    del data
    if consumers_dataset != dataset:
        raise ValueError('dataset mismatch: dataset %s vs consumers %s'
                         % (dataset, consumers_dataset))
    report = review(dataset, baseline_version, candidate_version,
                    baseline, candidate, consumers)
    save(output, report)
    return report
