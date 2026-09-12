"""Validate bounded host judgments and evidence, not the correctness of semantics."""
import json


def parse(api, raw, query, snapshot, entries, load_text):
    try:
        if not isinstance(raw, str) or len(raw) > 60000:
            raise ValueError('语义审查须为不超过60000字符的JSON')
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {'snapshot_id', 'query', 'sources'}:
            raise ValueError('语义审查需要snapshot_id、query和sources')
        if value['snapshot_id'] != snapshot or value['query'] != query:
            raise ValueError('语义审查问题或快照已变化')
        sources = value['sources']
        if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
            raise ValueError('语义审查每次排序1至20份候选')
        known = {(e['document_id'], e['revision_id']): e for e in entries}
        reviewed, seen = [], set()
        for rank, source in enumerate(sources, 1):
            if not isinstance(source, dict) or set(source) != {'document_id', 'revision_id', 'relevance', 'reason', 'evidence'}:
                raise ValueError('审查项需要文档修订、相关性、理由及精确摘录依据')
            key = (source['document_id'], source['revision_id'])
            if not all(isinstance(v, str) for v in key) or key in seen or key not in known:
                raise ValueError('重复、未知或不可读的审查修订')
            seen.add(key)
            if source['relevance'] not in ('relevant', 'needs_full_text'):
                raise ValueError('仅对相关或待全文确认材料排序')
            if not isinstance(source['reason'], str) or not 1 <= len(source['reason'].strip()) <= 1000:
                raise ValueError('每项需1至1000字相关性理由')
            evidence = source['evidence']
            if not isinstance(evidence, dict) or set(evidence) != {'start', 'end', 'quote'}:
                raise ValueError('摘录需要start、end、quote')
            a, b, quote = evidence['start'], evidence['end'], evidence['quote']
            text = load_text(known[key])
            if type(a) is not int or type(b) is not int or not isinstance(quote, str) or not 0 <= a < b <= len(text) or b-a > 2000 or text[a:b] != quote:
                raise ValueError('审查摘录与修订全文不一致或超过2000字')
            reviewed.append({**source, 'semantic_rank': rank, 'reviewer': 'current_host_agent',
                             'evidence_exact_match_verified': True, 'semantic_relevance_verified': False})
        return reviewed
    except (ValueError, TypeError, KeyError) as exc:
        raise api.KBError(f'语义审查无效：{exc}') from exc
