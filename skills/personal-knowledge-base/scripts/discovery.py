"""Read-only discovery and source reading, independent of factual qualification.

The immutable release and latest control remain authoritative. BM25 is a
persistent disposable derived index, never another source ledger. Scores select candidates,
not products or supported claims. No filename/topic/insurance aliases are added.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3

import search_index
import source_provenance
import semantic_review

REQUEST_SCHEMA = 'personal-kb.source-read-request.v1'
PAGE_CHARS = 6000


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def view(api, args):
    record, root, config = api.select_kb(api.state_home(args), args.name, args.kb_id)
    pointer, release, control = api.current_commit(root, config)
    if config.get('authorization', {}).get('model_context') is not True or any(
        control.get('authorization', {}).get(k) is not True for k in ('read', 'model_context')
    ):
        raise api.KBError('读取或有限原文进入模型的授权已撤销')
    catalog = api.read_json(release / 'catalog.json')
    entries, stopped = readable_entries(api, catalog, control, bool(getattr(args, 'include_history', False)))
    snapshot = digest({'pointer': pointer, 'control': control,
                       'include_history': bool(getattr(args, 'include_history', False)),
                       'entries': sorted((str(e.get('document_id')), str(e.get('revision_id')),
                                          str(e.get('extraction_id')), str(e.get('状态')))
                                         for e in entries)})
    return record, release, entries, snapshot, stopped


def readable_entries(api, catalog, control, include_history):
    active = [x for x in catalog.get('current', []) + catalog.get('leads', [])
              if isinstance(x, dict)]
    active_keys = {(x.get('document_id'), x.get('revision_id')) for x in active}
    candidates = list(active)
    if include_history:
        candidates += [{**x, 'discovery_historical': True} for x in catalog.get('snapshots', [])
                       if (x.get('document_id'), x.get('revision_id')) not in active_keys]
    candidates, stopped = api.restrictive_filter(candidates, control, include_history=include_history)
    readable = []
    for raw in candidates:
        historical = bool(raw.get('discovery_historical')) or str(raw.get('状态')) == '历史'
        if str(raw.get('状态')) in {'停用', '已停用', '废弃', '失效'} or (historical and not include_history):
            continue
        current_extraction = api.extraction_usable(raw)
        # An explicitly requested historical comparison may read a complete legacy
        # extraction. This does NOT requalify it as current evidence or re-extract
        # a same-named live file. Text and original hashes are checked on reading.
        legacy_complete = (raw.get('extraction') or {}).get('complete_text_coverage') is True
        if not current_extraction and not (historical and include_history and legacy_complete):
            continue
        item = dict(raw)
        if historical:
            item.update({'discovery_historical': True, '状态': '历史', 'qualification': 'inactive',
                         'discovery_extraction_revalidation_required': not current_extraction})
        readable.append(item)
    unique = {(x.get('document_id'), x.get('revision_id')): x for x in readable}
    entries = list(unique.values())
    return entries, stopped


def source_text(api, item, cache):
    extraction = cache.get(str(item.get('extraction_id'))) or cache.get(str(item.get('sha256')))
    if not isinstance(extraction, dict) or not isinstance(extraction.get('text'), str):
        raise api.KBError('资料缺少可验证全文缓存，请更新后重试')
    text = extraction['text']
    actual = hashlib.sha256(text.encode()).hexdigest()
    if actual != extraction.get('text_sha256') or (
        item.get('extracted_text_sha256') and actual != item['extracted_text_sha256']
    ):
        raise api.KBError('全文缓存与文档修订不一致')
    return text, extraction


def guard_token(api, args):
    _, root, config = api.select_kb(api.state_home(args), args.name, args.kb_id)
    return digest([config, api.read_json(root / 'current.json'), api.visual_review.revision(root)])


def maintain(api, root, config):
    """Normal update/repair path; failures never roll back authoritative data."""
    try:
        control = api.effective_control(root, config)
        if any(config.get('authorization', {}).get(k) is not True or
               control.get('authorization', {}).get(k) is not True for k in ('read', 'model_context')):
            return {'status': 'not_authorized', 'message': '授权已撤销，未读取或建立搜索索引。'}
        _, release, control = api.current_commit(root, config)
        catalog = api.read_json(release / 'catalog.json')
        entries, _ = readable_entries(api, catalog, control, True)
        cache = api.read_json(release / 'extractions.json')['sources']
        return search_index.rank(root, entries, lambda e: source_text(api, e, cache)[0], prune=True)
    except (api.KBError, OSError, ValueError, sqlite3.Error) as exc:
        return {'status': 'needs_attention', 'message': '知识数据已保留，搜索索引维护未完成；请重试更新我的资料库。',
                'detail': str(exc)}


MAX_QUERY_ATTEMPTS = 3
MAX_REWRITE_CHARS = 500


def query_plan(api, args):
    """Validate host-supplied lexical rewrites, never infer semantic relevance."""
    raw = getattr(args, 'query_plan_json', None)
    if raw is None:
        return [args.query]
    try:
        if not isinstance(raw, str) or len(raw) > 8000:
            raise ValueError('查询计划过大或不是JSON字符串')
        plan = json.loads(raw)
        if not isinstance(plan, dict) or set(plan) != {'rewrites'}:
            raise ValueError('查询计划仅接受rewrites字段')
        rewrites = plan['rewrites']
        if not isinstance(rewrites, list) or len(rewrites) > MAX_QUERY_ATTEMPTS - 1:
            raise ValueError('最多提供两个宿主改写，总共最多三次词法查询')
        seen = {''.join(args.query.split()).casefold()}
        queries = [args.query]
        for rewrite in rewrites:
            if not isinstance(rewrite, str) or not 1 <= len(rewrite.strip()) <= MAX_REWRITE_CHARS:
                raise ValueError('每个改写须为1至500字符的文本')
            key = ''.join(rewrite.split()).casefold()
            if key in seen:
                raise ValueError('改写不得与原问题或其他改写重复')
            seen.add(key)
            queries.append(rewrite.strip())
        return queries
    except (ValueError, TypeError) as exc:
        raise api.KBError(f'查询计划无效：{exc}') from exc


def host_workflow(queries):
    return {
        'semantic_reviewer': 'current_host_agent',
        'automatic_semantic_recall': False,
        'max_lexical_queries_per_question': MAX_QUERY_ATTEMPTS,
        'queries_used_this_call': len(queries),
        'remaining_queries_if_first_call': MAX_QUERY_ATTEMPTS - len(queries),
        'budget_enforcement': 'per_call_by_backend; across_calls_by_host',
        'required_candidate_checks': ['对象和主题对应', '所问关系或字段有线索', '版本和条件适用'],
        'rewrite_policy': '依据用户问题，换常用说法、拆出核心动作/对象/字段；不添加猜测答案或隐藏标准答案。最多两个改写，不扩展授权范围。',
        'semantic_submission': 'call --semantic-review-json接收当前Agent有序判断及精确摘录；保留理由并机械核验身份/位置，不证明语义正确。',
        'next_action': '逐一审查候选，记录相关/不相关/待全文确认及理由；不按排名自动选第一名。仅选择相关或待全文确认资料，按selection-json和read-source续读至complete=true。',
        'retry_action': '若首次未提供改写且候选无关/缺字段，剩余额度内每次以一个新改写作--query且不附查询计划；宿主累计所有查询，最多三次。',
        'stop_condition': '预算耗尽或全文无证据时明确资料未覆盖，拒绝猜答；无关第一名不是证据。',
        'answer_gate': '全文支持用户所问对象/关系/条件后才能回答，引用来源及位置；verify-citations只核验字面，不核验语义。',
        'measurement': '首次候选命中与宿主全文阅读后回答成功分别记录；返回候选不算回答成功。',
    }


def discover(api, args):
    if not args.query or not args.query.strip() or len(args.query) > 4000:
        raise api.KBError('请提供1至4000字的资料任务')
    if not 1 <= args.limit <= 20 or args.offset < 0:
        raise api.KBError('候选页参数无效')
    queries = query_plan(api, args)
    initial_guard = guard_token(api, args)
    record, release, entries, snapshot, stopped = view(api, args)
    cache = api.read_json(release / 'extractions.json')['sources']
    try:
        ranking, attempts, truncated = {}, [], False
        for attempt_id, query in enumerate(queries):
            hits, chunks, limited, metrics = search_index.rank(
                release.parent.parent, entries, lambda e: source_text(api, e, cache)[0], query)
            attempts.append({'attempt': attempt_id, 'query': query, 'matched_count': len(hits),
                             'retrieval_truncated': limited, 'search_index': metrics})
            truncated = truncated or limited
            for rank, (i, hit) in enumerate(hits.items(), 1):
                if i not in ranking:
                    ranking[i] = {**hit, 'fusion_score': 0.0, 'retrieval_hits': []}
                ranking[i]['fusion_score'] += 1.0 / (60 + rank)
                ranking[i]['retrieval_hits'].append({'attempt': attempt_id, 'rank': rank, **hit})
        if len(queries) > 1:
            ranking = dict(sorted(ranking.items(), key=lambda pair: (-pair[1]['fusion_score'], pair[0])))
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise api.KBError(f'搜索索引或查询未完成：{exc}；请重试或更新我的资料库') from exc
    results = []
    for i, score in ranking.items():
        e = entries[i]
        text, extraction = source_text(api, e, cache)
        excerpt_start = score.get('excerpt_start', 0)
        excerpt = score.get('excerpt', '')
        if type(excerpt_start) is not int or not isinstance(excerpt, str) or not 0 <= excerpt_start <= len(text):
            raise api.KBError('候选摘录位置无效，请更新搜索索引')
        excerpt_end = excerpt_start + len(excerpt)
        if text[excerpt_start:excerpt_end] != excerpt:
            raise api.KBError('候选摘录与修订全文不一致，请更新搜索索引')
        provenance = source_provenance.describe(api, release, e, extraction, excerpt_start, excerpt_end)
        results.append({
            'document_id': e['document_id'], 'revision_id': e['revision_id'],
            'file': e['source_relative'], '资料类型': e.get('资料类型'),
            '状态': e.get('状态'), 'qualification': e.get('qualification'),
            '证据级别': e.get('证据级别'), 'historical': bool(e.get('discovery_historical')),
            'product_name': e.get('product_name'), 'applicable_version': e.get('applicable_version'),
            'semantic_relevance_verified': False, 'factual_support_verified': False,
            'source_instruction_authority': 'none', 'source_policy': source_provenance.policy(e),
            'provenance': provenance, **score,
            'read_request': {'schema': REQUEST_SCHEMA, 'snapshot_id': snapshot,
                             'document_id': e['document_id'], 'revision_id': e['revision_id'],
                             'include_history': bool(args.include_history), 'offset': 0},
        })
    if guard_token(api, args) != initial_guard:
        raise api.KBError('查询期间资料或授权已变化，请重新发现资料')
    page = results[args.offset:args.offset + args.limit]
    api.emit({'schema': 'personal-kb.discovery.v1', 'status': 'candidates' if page else 'no_candidates',
              'read_only': True, 'query': args.query, 'snapshot_id': snapshot,
              'knowledge_base': {'id': record['id'], 'name': record['name']},
              'discoverable_count': len(entries), 'matched_count': len(results), 'indexed_chunks': chunks,
              'retrieval_truncated': truncated, 'candidates': page,
              'search_index': metrics, 'derived_cache_write_allowed': True,
              'query_attempts': attempts, 'host_workflow': host_workflow(queries),
              'ranking_method': 'reciprocal_rank_fusion' if len(queries) > 1 else 'bm25',
              'read_only_scope': 'authoritative_sources_releases_and_control',
              'next_offset': args.offset + args.limit if args.offset + args.limit < len(results) else None,
              'restricted_count': len(stopped),
              'instruction': '这些是候选，不代表对象已确认或事实已核验。按任务选材并调用read-source读完整原文；无关候选不可替代用户对象。保留原状态与来源等级，产品事实、业务经验和表达分开。'})


def read_source(api, args):
    try:
        request = json.loads(args.request_json)
        required = {'schema', 'snapshot_id', 'document_id', 'revision_id', 'include_history', 'offset'}
        if not isinstance(request, dict) or set(request) != required or request['schema'] != REQUEST_SCHEMA:
            raise ValueError('invalid_source_request')
        if type(request['include_history']) is not bool or type(request['offset']) is not int or request['offset'] < 0:
            raise ValueError('invalid_source_offset_or_history')
    except (ValueError, TypeError) as exc:
        raise api.KBError(str(exc)) from exc
    args.include_history = request['include_history']
    initial_guard = guard_token(api, args)
    _, release, entries, snapshot, _ = view(api, args)
    if snapshot != request['snapshot_id']:
        raise api.KBError('资料或授权快照已变化，请重新发现资料')
    item = next((e for e in entries if e.get('document_id') == request['document_id']
                 and e.get('revision_id') == request['revision_id']), None)
    if item is None:
        raise api.KBError('请求的修订不属于当前授权可读集合')
    # Validate original in addition to release-manifest verification. Never follow a supplied path.
    original = source_provenance.original_path(api, release, item)
    cache = api.read_json(release / 'extractions.json')['sources']
    text, extraction = source_text(api, item, cache)
    start = request['offset']
    if start >= len(text):
        raise api.KBError('全文读取偏移超出范围')
    end = min(len(text), start + PAGE_CHARS)
    provenance = source_provenance.describe(api, release, item, extraction, start, end)
    if guard_token(api, args) != initial_guard:
        raise api.KBError('读取期间资料或授权已变化，请重新发现资料')
    api.emit({'schema': 'personal-kb.source-read.v1', 'status': 'ready', 'read_only': True,
              'snapshot_id': snapshot, 'document_id': item['document_id'], 'revision_id': item['revision_id'],
              'file': item['source_relative'], 'original_file': str(original), 'sha256': item['sha256'],
              '资料类型': item.get('资料类型'), '状态': item.get('状态'), 'qualification': item.get('qualification'),
              '证据级别': item.get('证据级别'), 'historical': bool(item.get('discovery_historical')),
              'factual_support_verified': False, 'source_instruction_authority': 'none',
              'text': text[start:end], 'start': start, 'end': end, 'total_characters': len(text),
              'text_sha256': extraction['text_sha256'], 'complete': end == len(text),
              'source_policy': source_provenance.policy(item),
              'provenance': provenance,
              'extraction_coverage': api.extraction_summary(extraction.get('meta', {})),
              'source_pages': [p['page'] for p in extraction.get('meta', {}).get('pages_detail', [])
                               if p['text_end'] > start and p['text_start'] < end],
              'next_request': {**request, 'offset': end} if end < len(text) else None})


def task_materials(api, args):
    """Normal query/generate/review entry: discover first; explicit document selection next.

    Target-intent names are context, never a precondition to discover/read.
    Selection binds document revisions, not an inferred product identity.
    """
    args.limit = 7
    args.offset = 0
    args.include_history = bool(getattr(args, 'include_history', False))
    if args.material_request_json:
        args.request_json = args.material_request_json
        return read_source(api, args)
    if args.generation_cursor_json:
        raise api.KBError('旧创作分页协议已替换；请使用discover分页或重新按任务发现资料')
    if any(getattr(args, field, None) for field in ('review_plan_file', 'semantic_results_file', 'review_source_cursor_json')):
        raise api.KBError('旧审核提交协议只用于--legacy-contract兼容。正常审核先选材读全文，再用verify-citations校验引用；语义结论由当前Agent逐项说明。')
    review_raw = getattr(args, 'semantic_review_json', None)
    if review_raw and args.selection_json:
        raise api.KBError('semantic-review-json和selection-json不能同时提交；语义审查自带有序选材身份')
    if not args.selection_json and not review_raw:
        return discover(api, args)
    initial_guard = guard_token(api, args)
    record, release, entries, snapshot, _ = view(api, args)
    cache = api.read_json(release / 'extractions.json')['sources']
    reviews = semantic_review.parse(api, review_raw, args.query, snapshot, entries,
                                   lambda e: source_text(api, e, cache)[0]) if review_raw else []
    try:
        selection = ({'snapshot_id': snapshot, 'sources': [{k: r[k] for k in ('document_id', 'revision_id')} for r in reviews]}
                     if review_raw else json.loads(args.selection_json))
        if not isinstance(selection, dict) or set(selection) != {'snapshot_id', 'sources'} or selection['snapshot_id'] != snapshot:
            raise ValueError('资料或授权快照变化，请重新发现并选择资料')
        sources = selection['sources']
        if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
            raise ValueError('每次明确选择1至20份相关资料，不把全库当作选择')
        selected = []
        seen = set()
        for source in sources:
            if not isinstance(source, dict) or set(source) != {'document_id', 'revision_id'}:
                raise ValueError('选择必须使用返回的文档与修订身份')
            key = (source['document_id'], source['revision_id'])
            if not all(isinstance(v, str) for v in key) or key in seen:
                raise ValueError('文档选择重复或格式无效')
            seen.add(key)
            entry = next((e for e in entries if (e['document_id'], e['revision_id']) == key), None)
            if entry is None:
                raise ValueError('所选文档修订不在当前授权可读范围')
            selected.append(entry)
    except (ValueError, TypeError, KeyError) as exc:
        raise api.KBError(str(exc)) from exc
    groups, ordered = {}, []
    for index, e in enumerate(selected):
        item = {key: e.get(key) for key in ('document_id', 'revision_id', 'source_relative',
                '资料类型', '状态', 'qualification', '证据级别', 'product_name', 'applicable_version')}
        _, extraction = source_text(api, e, cache)
        item.update({'source_policy': source_provenance.policy(e),
                     'provenance': source_provenance.describe(api, release, e, extraction),
                     'selection_rank': index + 1, 'semantic_review': reviews[index] if reviews else None,
                     'historical': bool(e.get('discovery_historical')), 'factual_support_verified': False,
                     'source_instruction_authority': 'none',
                     'read_request': {'schema': REQUEST_SCHEMA, 'snapshot_id': snapshot,
                        'document_id': e['document_id'], 'revision_id': e['revision_id'],
                        'include_history': args.include_history, 'offset': 0}})
        groups.setdefault(str(e.get('资料类型')), []).append(item)
        ordered.append(item)
    if guard_token(api, args) != initial_guard:
        raise api.KBError('选材期间资料或授权已变化，请重新发现资料')
    api.emit({'schema': 'personal-kb.task-materials.v1', 'status': 'materials_selected',
              'read_only': True, 'task': args.task, 'query': args.query, 'snapshot_id': snapshot,
              'knowledge_base': {'id': record['id'], 'name': record['name']}, 'groups': groups,
              'selected_count': len(selected), 'product_identity_confirmed': False,
              'ordered_materials': ordered, 'ranking_method': 'host_semantic_judgment' if reviews else 'explicit_selection_order',
              'semantic_review_complete': False,
              'authority_rule': '相关性与资料权威分开；写作任务不强制产品第一。产品事实使用对应对象/版本/条件的已采用正式资料，待采用、待核验、笔记和文案不得覆盖正式依据。宿主理由仅被记录，语义正确性未经后端证明。',
              'review_guidance': ({'semantic_reviewer': 'current_host_agent',
                  'required_checks': ['对象及版本是否对应', '逐项事实与原文是否一致', '条件例外和限定语是否完整',
                                      '冲突或未覆盖是否说明', '个人经验表达是否误当产品事实'],
                  'citation_check': 'verify-citations仅证明引用字面和位置准确，不证明所述事实成立',
                  'must_not_claim': '不得把materials_selected、引用准确或低置信标签变化说成审核全部通过'}
                 if args.task == 'review' else None),
              'instruction': '选择仅绑定资料，不认定产品身份或事实。用read-source读完所需原文后，由当前Agent分别处理产品事实、本人经验和表达；不相关/旧版/冲突不混用，未覆盖不补猜。'})


def verify_citations(api, args):
    """Mechanical provenance only; no keyword or automatic semantic verdicts."""
    try:
        request = json.loads(args.request_json)
        if not isinstance(request, dict) or set(request) != {'snapshot_id', 'include_history', 'citations'}:
            raise ValueError('invalid_citation_request')
        if type(request['include_history']) is not bool:
            raise ValueError('invalid_history_flag')
        citations = request['citations']
        if not isinstance(citations, list) or not 1 <= len(citations) <= 50:
            raise ValueError('每次核验1至50项引用')
        if sum(len(c.get('quote', '')) if isinstance(c, dict) and isinstance(c.get('quote'), str) else 0 for c in citations) > 18000:
            raise ValueError('引用超过本次核验预算，请分批提交')
    except (ValueError, TypeError) as exc:
        raise api.KBError(str(exc)) from exc
    args.include_history = request['include_history']
    initial_guard = guard_token(api, args)
    _, release, entries, snapshot, _ = view(api, args)
    if request['snapshot_id'] != snapshot:
        raise api.KBError('引用快照已过期，请重新选材读取')
    cache = api.read_json(release / 'extractions.json')['sources']
    validated = []
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != {'document_id', 'revision_id', 'start', 'end', 'quote'}:
            raise api.KBError('引用需要精确文档、修订、全文位置与原文')
        item = next((e for e in entries if e['document_id'] == citation['document_id'] and e['revision_id'] == citation['revision_id']), None)
        if item is None:
            raise api.KBError('引用修订不在当前授权范围')
        text, extraction = source_text(api, item, cache)
        start, end, quote = citation['start'], citation['end'], citation['quote']
        if type(start) is not int or type(end) is not int or not isinstance(quote, str) or not 0 <= start < end <= len(text) or text[start:end] != quote:
            raise api.KBError('引用原文与声明的位置不一致')
        validated.append({**citation, 'file': item['source_relative'], 'exact_quote_verified': True,
                          'historical': bool(item.get('discovery_historical')), '状态': item.get('状态'),
                          'qualification': item.get('qualification'), 'factual_support_verified': False,
                          'source_policy': source_provenance.policy(item),
                          'verification_basis': 'extracted_text',
                          'original_page_verified': False,
                          'provenance': source_provenance.describe(api, release, item, extraction, start, end)})
    if guard_token(api, args) != initial_guard:
        raise api.KBError('引文核验期间资料或授权已变化，请重新读取')
    api.emit({'schema': 'personal-kb.citation-check.v1', 'status': 'quotes_verified', 'read_only': True,
              'snapshot_id': snapshot, 'citations': validated, 'semantic_review_complete': False,
              'instruction': '仅证明这些字句来自所选修订与位置。当前Agent仍须核对对象、语义、条件、例外、版本与冲突；引用准确不等于结论正确。'})
