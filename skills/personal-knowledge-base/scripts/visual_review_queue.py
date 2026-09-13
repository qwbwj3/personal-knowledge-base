"""Read-only visual work queue from a verified current catalog, not audit history.

Callers must hold the maintenance lock and validate authorization/current release.
No OCR, implicit acceptance, deletion or rewriting of requests/decisions here.
"""
from __future__ import annotations
from pathlib import Path


def collect(store, catalog=None):
    from pdf_pipeline import EXTRACTION_VERSION
    from visual_review import digest
    catalog = catalog or {}
    ledger = store.ledger()
    decisions = {key: row for key, row in ledger['decisions'].items()
                 if key not in ledger['revoked'] and row.get('status') == 'accepted'
                 and digest(row) == key}
    # Only current/lead projections, never arbitrary historical snapshots.
    readable, invalid_certificates = set(), set()
    for item in [*catalog.get('current', []), *catalog.get('leads', [])]:
        meta = item.get('extraction') or {}
        identity = (item.get('source_relative'), item.get('sha256'), meta.get('extraction_version'))
        if meta.get('complete_text_coverage') is True and meta.get('extraction_version') == EXTRACTION_VERSION:
            if store.valid_meta(meta, str(item.get('sha256'))):
                readable.add(identity)
            else:
                invalid_certificates.update(c.get('request_id') for c in meta.get('visual_review_decisions', []))
    # A new failed extraction wins over older accepted content of the same bytes.
    active = {}
    for failure in catalog.get('maintenance', []):
        meta = failure.get('extraction') or {}
        refs = {p['request_id'] for p in meta.get('visual_review_requests', []) if p.get('request_id')}
        if refs:
            active.setdefault(failure.get('file'), set()).update(refs)
    requests = {}
    hashes, source_unavailable = {}, set()
    for path in sorted((store.root / 'requests').glob('*.json'), key=lambda p: (p.stat().st_mtime_ns, p.name)):
        req = store.request(path.stem, verify_source=False)
        identity = (req['source_relative'], req['source_sha256'], req['extraction_version'])
        if req['extraction_version'] != EXTRACTION_VERSION:
            continue
        source_key = identity[:2]
        if source_key not in hashes:
            try:
                store.source(*source_key)
                hashes[source_key] = True
            except (OSError, ValueError):
                hashes[source_key] = False
                source_unavailable.add(source_key)
        if not hashes[source_key]:
            continue
        ref = req['request_id']
        if req['source_relative'] in active:
            if ref not in active[req['source_relative']] and ref not in invalid_certificates:
                continue
        elif identity in readable and ref not in invalid_certificates:
            continue
        # For pre-release requests use the most recent record; never accept by
        # filename/page alone. Published failures provide exact request identities.
        key = (*identity, req['page'])
        candidate = {'request_id': ref, 'file': req['source_relative'], 'page': req['page'],
                     'source_sha256': req['source_sha256'], 'extraction_version': req['extraction_version']}
        current = requests.get(key)
        if current and current['request_id'] in invalid_certificates and ref not in invalid_certificates:
            continue
        requests[key] = candidate
    items = []
    for row in requests.values():
        accepted = any(v.get('request_id') == row['request_id'] and v.get('source_sha256') == row['source_sha256']
                       for v in decisions.values())
        items.append({**row, 'state': 'awaiting_update' if accepted else 'needs_review',
                      'next_action': 'update' if accepted else 'visual-review --prepare ' + row['request_id']})
    items.sort(key=lambda v: (v['file'], v['page'], v['request_id']))
    return {'items': items, 'pending_count': sum(v['state'] == 'needs_review' for v in items),
            'awaiting_update_count': sum(v['state'] == 'awaiting_update' for v in items),
            'source_revision_unavailable_count': len(source_unavailable),
            'queue_id': digest({'release': catalog.get('release_id'), 'ledger': ledger, 'items': items}),
            'basis': 'verified_current_catalog_and_exact_revision_requests'}


def paginate(queue, *, limit=20, offset=0, expected_queue_id=None):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('--limit must be 1..100 and --offset must be nonnegative')
    if expected_queue_id is not None and expected_queue_id != queue['queue_id']:
        raise ValueError('Visual queue changed; restart pagination from --offset 0')
    items = queue['items']
    end = min(offset + limit, len(items))
    return {**queue, 'items': items[offset:end], 'items_total': len(items),
            'items_returned': len(items[offset:end]), 'offset': offset, 'limit': limit,
            'items_truncated': end < len(items), 'has_more': end < len(items),
            'next_offset': end if end < len(items) else None}
