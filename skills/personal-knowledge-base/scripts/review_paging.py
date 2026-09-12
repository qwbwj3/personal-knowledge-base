"""Snapshot-bound display of complete eligible review chunks; never a verdict gate.

Verification consumes the full eligible pool. This independent display iterator
lets the host read every original chunk needed for semantic checks without
pretending a truncated response is the entire pool.
"""
from __future__ import annotations
from collections import defaultdict
import hashlib
from review_protocol import digest

PAGE_CHUNKS = 20
SCHEMA = 'personal-kb.review-source-cursor.v2'


def page(evidence: list[dict], sources: list[dict], snapshot_id: str,
         resolution: dict, query: str, properties: list[str], cursor: dict | None = None) -> tuple[dict, dict]:
    index={(str(s.get('document_id')),str(s.get('revision_id'))):s for s in sources}
    revisions=[]
    for item in evidence:
        source=index.get((str(item.get('document_id')),str(item.get('revision_id'))))
        if item.get('资料类型')=='产品资料' and source and source.get('qualification')=='evidence' and source.get('状态') in {'当前','有效','生效'}:
            chunks=source.get('chunks') or []
            if chunks: revisions.append((item,chunks))
    binding={'schema':SCHEMA,'snapshot_id':snapshot_id,'target_digest':digest(resolution),
             'input_id':hashlib.sha256(query.encode('utf-8')).hexdigest(), 'properties':sorted(set(properties))}
    ri=ci=0
    if cursor is not None:
        fields=set(binding)|{'next_revision_index','next_chunk_index'}
        if not isinstance(cursor,dict) or set(cursor)!=fields or any(cursor.get(k)!=v for k,v in binding.items()):
            raise ValueError('invalid_or_stale_review_source_cursor')
        ri,ci=cursor['next_revision_index'],cursor['next_chunk_index']
        if type(ri) is not int or type(ci) is not int or not 0<=ri<len(revisions) or not 0<=ci<len(revisions[ri][1]):
            raise ValueError('review_source_cursor_out_of_range')
    start={'revision_index':ri,'chunk_index':ci}
    grouped=defaultdict(list); emitted=0
    while ri<len(revisions) and emitted<PAGE_CHUNKS:
        original,chunks=revisions[ri]; item=dict(original); selected=[]
        while ci<len(chunks) and emitted<PAGE_CHUNKS:
            chunk=chunks[ci]
            selected.append({'chunk_id':chunk['chunk_id'],'index':chunk.get('index'),'start':0,'end':len(chunk['text']),'text':chunk['text']})
            emitted+=1; ci+=1
        item['review_source_chunks']=selected
        # Compatibility topic view references the same page chunks, not the
        # pre-pagination best-chunk list (which repeated unseen chunks).
        item['topic_evidence']={'完整审核原文':selected}
        grouped['产品资料'].append(item)
        if ci==len(chunks): ri+=1; ci=0
    for item in evidence:
        if item.get('资料类型')!='产品资料' and len(grouped[item['资料类型']])<3:
            grouped[item['资料类型']].append(item)
    next_cursor={**binding,'next_revision_index':ri,'next_chunk_index':ci} if ri<len(revisions) else None
    display={'start':start,'next':{'revision_index':ri,'chunk_index':ci},'displayed_chunks':emitted,
             'total_revisions':len(revisions),'total_chunks':sum(len(c) for _,c in revisions),
             'complete':next_cursor is None,'verification_uses_full_pool':True}
    return dict(grouped), {'next_cursor':next_cursor,'source_display':display}
