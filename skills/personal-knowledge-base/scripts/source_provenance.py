"""Version-bound local source navigation; never certifies visual correctness."""
MAX_NAVIGATION_PAGES = 20

POLICY_FIELDS = ('material_role', 'authority_tier', 'adoption_status', 'source_risk', 'policy_version', 'authority_basis', 'policy_basis_sha256', 'original_protection')


def policy(item):
    nested = item.get('source_policy') or {}
    result = {key: item.get(key, nested.get(key, 'unknown')) for key in POLICY_FIELDS}
    if item.get('discovery_historical') or str(item.get('状态')) == '历史':
        result.update(usage_scope='historical_comparison_only', current_product_evidence_allowed=False)
    return result


def original_path(api, release, item):
    try:
        raw = release / str(item.get('original_file', ''))
        original = raw.resolve(strict=True)
        originals = release / 'originals'
        if originals.is_symlink() or raw.is_symlink() or original.parent != originals.resolve(strict=True):
            raise ValueError('原件路径不属于修订快照')
        expected = item.get('original_sha256') or item.get('sha256')
        if not expected or api.sha256_file(original) != expected:
            raise ValueError('原件哈希不一致')
        if item.get('sha256') and expected != item['sha256']:
            raise ValueError('原件与修订哈希不一致')
        return original
    except (OSError, ValueError, RuntimeError) as exc:
        raise api.KBError(f'原件校验失败：{exc}') from exc


def describe(api, release, item, extraction, start=None, end=None):
    original = original_path(api, release, item)
    text = extraction['text']
    pages = []
    total_pages = 0
    methods = set()
    for p in (extraction.get('meta') or {}).get('pages_detail', []):
        a, b, number = p.get('text_start'), p.get('text_end'), p.get('page')
        if type(a) is not int or type(b) is not int or type(number) is not int or number < 1:
            continue
        if start is not None and not (b > start and a < end):
            continue
        method = p.get('method') or 'unknown'
        methods.add(method)
        total_pages += 1
        if len(pages) >= MAX_NAVIGATION_PAGES:
            continue
        quality = p.get('quality') or {}
        quality = {k: quality[k] for k in ('readable', 'ocr_min_confidence', 'ocr_mean_confidence', 'ocr_content_min_confidence', 'ocr_content_mean_confidence', 'ocr_quality_status', 'ocr_numeric_accuracy_verified', 'chars', 'replacement_ratio', 'control_ratio') if k in quality}
        quality['reasons'] = list((p.get('quality') or {}).get('reasons') or [])[:10]
        corroborations = (p.get('quality') or {}).get('ocr_reference_corroborations') or []
        if corroborations:
            fields = ('line_index', 'status', 'kind', 'reference', 'reference_role',
                      'raw_ocr_confidence', 'ocr_numeric_accuracy_verified',
                      'semantic_accuracy_verified', 'basis')
            quality['ocr_reference_corroborations'] = [
                {k: (v[:300] if isinstance(v, str) else v)
                 for k, v in entry.items() if k in fields and type(v) in (str, int, float, bool, type(None))}
                for entry in corroborations[:10] if isinstance(entry, dict)]
            quality['ocr_reference_corroboration_count'] = len(corroborations)
            quality['ocr_reference_corroborations_truncated'] = len(corroborations) > 10
        overlap_start = max(a, start) if start is not None else None
        overlap_end = min(b, end) if start is not None else None
        pages.append({'physical_page': number, 'text_start': a, 'text_end': b,
                      'covered_text_range': {'start': overlap_start, 'end': overlap_end} if start is not None else None,
                      'page_uri': original.as_uri() + f'#page={number}',
                      'extraction_method': method, 'ocr': None if method == 'unknown' else 'ocr' in method.lower(),
                      'quality': quality, 'status': p.get('status', 'unknown'),
                      'warnings': [str(w)[:300] for w in p.get('warnings', [])[:10]],
                      'image_text_coverage': {k: v for k, v in (p.get('image_text_coverage') or {}).items() if k in ('status', 'word_coverage_verified')},
                      'original_page_verified': False})
    return {'document_id': item['document_id'], 'revision_id': item['revision_id'],
            'document_version': item.get('document_version') or item.get('版本') or 'unknown',
            'applicable_version': item.get('applicable_version') or 'unknown',
            'original_uri': original.as_uri(), 'original_sha256': item.get('original_sha256') or item.get('sha256'),
            'version_bound_snapshot': True, 'text_sha256': extraction['text_sha256'],
            'historical': bool(item.get('discovery_historical')) or str(item.get('状态')) == '历史',
            'extraction_revalidation_required': bool(item.get('discovery_extraction_revalidation_required')),
            'source_presence': {'missing': item.get('source_missing'),
                                'basis': 'recorded_at_import_not_live_check'},
            'text_range': {'start': start, 'end': end} if start is not None else None,
            'quote': text[start:end] if start is not None else None,
            'pages': pages, 'matching_page_count': total_pages,
            'pages_truncated': total_pages > len(pages), 'page_limit': MAX_NAVIGATION_PAGES,
            'page_mapping_note': '页列表仅返回前20个涉及页；被截断时缩小read-source范围或提交具体引文，勿把展示列表视作全部页。',
            'page_mapping_status': 'available' if pages else 'unknown',
            'extraction_methods': sorted(methods) or ['unknown'],
            'original_page_verified': False, 'verification_basis': 'extracted_text',
            'navigation_note': '链接绑定保存的原件修订，不表示源文件夹里的旧文件仍存在；#page为阅读器兼容提示，未验证Windows自动跳页。不能跳页时按physical_page手动定位；不提供高亮保证。',
            'verification_note': '字面核验不等于原页或事实核验；产品条件、数字、日期和否定词需人工查看原页及上下文。'}
