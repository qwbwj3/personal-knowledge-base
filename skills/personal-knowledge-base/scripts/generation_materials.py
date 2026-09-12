"""Bounded lexical generation retrieval over backend-qualified material only.

No model, filename ranking, fixed topic examples or numeric-density bonus.
Character ngrams avoid requiring a Chinese tokenizer. This is not a semantic
retriever: unmatched paraphrases are disclosed, not filled with unrelated files.
The page cap is unchanged (three personal docs/five product docs per group).
"""
from __future__ import annotations
import math
import re
import unicodedata
from collections import Counter, defaultdict

# Task framing words carry no subject evidence. Not a product/topic vocabulary.
FRAMING = ('个人知识库','知识库','同主题','历史口播','历史文章','业务案例','个人内容','个人表达',
           '产品资料','业务资料','个人资料','完整历史文章','完整作品','我的','结合','参考','使用',
           '整理','一篇','一段','一条','这篇','口播','文章','资料','内容','案例','生成','写作')

def relevance_text(text: str) -> str:
    # Explicit non-topic scope is not positive subject evidence. Keep original
    # quotations unchanged; this normalization is used only for lexical scoring.
    value=unicodedata.normalize('NFKC',text).casefold()
    value=re.sub(r'(?:与|和|跟)[^，。！？；;\n]*无关', ' ', value)
    return re.sub(r'(?:不涉及|不讨论|不包含|不要使用|不参考|不使用)[^，。！？；;\n]*', ' ', value)

def query_terms(query: str) -> set[str]:
    value=relevance_text(query)
    for word in sorted(FRAMING,key=len,reverse=True): value=value.replace(word,' ')
    terms=set()
    for span in re.findall(r'[\u3400-\u9fff]+|[a-z][a-z0-9_-]+',value):
        if span.isascii():
            if len(span)>2: terms.add(span)
        else:
            for n in range(2,min(8,len(span))+1):
                terms.update(span[i:i+n] for i in range(len(span)-n+1))
    return terms

def lexical_scores(query: str, texts: list[str]) -> list[dict]:
    terms=query_terms(query)
    normalized=[relevance_text(t) for t in texts]
    df=Counter(term for text in normalized for term in terms if term in text)
    result=[]
    for text in normalized:
        matches=[term for term in terms if term in text]
        # A single generic two-character overlap is not enough to select a source.
        topical=any(len(t)>=3 for t in matches) or len({t for t in matches if len(t)==2})>=2
        value=sum((len(t)-1)**2 * (1+math.log((len(texts)+1)/(df[t]+1))) for t in matches) if topical else 0
        result.append({'score':round(value,6),'matched_terms':sorted(matches,key=lambda t:(-len(t),t))[:12] if topical else []})
    return result

def rank(query: str, entries: list[dict], sources: list[dict], authority_rank) -> tuple[dict,dict]:
    bodies={(str(s.get('sha256')),str(s.get('document_id'))):s.get('chunks',[]) for s in sources}
    texts=['\n'.join(str(c['text']) for c in bodies.get((str(e.get('sha256')),str(e.get('document_id'))),[])) for e in entries]
    scores=lexical_scores(query,texts); grouped=defaultdict(list); excluded=[]; candidates=Counter()
    for entry,match in zip(entries,scores):
        kind=entry['资料类型']; candidates[kind]+=1
        if kind=='当前规则与决定':
            # Current rules apply across tasks; report this separately from relevance.
            match={'score':0,'matched_terms':[],'reason':'current_rules_not_relevance_claim'}
        elif match['score']<=0:
            excluded.append({'document_id':entry.get('document_id'),'reason':'no_substantive_lexical_match'}); continue
        chunks=bodies.get((str(entry.get('sha256')),str(entry.get('document_id'))),[])
        chunk_scores=lexical_scores(query,[str(c['text']) for c in chunks])
        chosen=sorted(zip(chunks,chunk_scores),key=lambda x:(-x[1]['score'],x[0].get('index',0)))[:2]
        selected=[c for c,cs in chosen if cs['score']>0 or kind=='当前规则与决定']
        item={**entry,'generation_relevance':match,'topic_evidence':{'创作任务材料':[{'chunk_id':c['chunk_id'],'index':c.get('index'),'start':0,'text':c['text'],'source_pages':c.get('source_pages',[])} for c in selected]}}
        grouped[kind].append(item)
    for kind,items in grouped.items():
        items.sort(key=lambda e:((authority_rank(str(e.get('证据级别',''))) if kind=='产品资料' else 0),-e['generation_relevance']['score'],str(e.get('sha256')),str(e.get('document_id'))))
    return dict(grouped),{'method':'qualified-lexical-char-ngrams-v1','semantic_relevance_verified':False,'candidate_count':dict(candidates),'relevant_count':{k:len(v) for k,v in grouped.items()},'excluded':excluded}

def page(grouped: dict, snapshot_id: str, cursor: dict | None) -> tuple[dict,dict]:
    offsets={}
    if cursor is not None:
        if (not isinstance(cursor,dict) or set(cursor)!={'schema','snapshot_id','offsets'} or cursor.get('schema')!='personal-kb.generation-cursor.v1' or cursor.get('snapshot_id')!=snapshot_id or not isinstance(cursor.get('offsets'),dict)):
            raise ValueError('invalid_or_stale_generation_cursor')
        offsets=cursor['offsets']
        if not offsets or any(k not in grouped or type(v) is not int or v<0 or v>=len(grouped[k]) for k,v in offsets.items()):
            raise ValueError('generation_cursor_out_of_range')
    selected={}; next_offsets={}
    kinds=list(offsets) if cursor is not None else list(grouped)
    for kind in kinds:
        offset=offsets.get(kind,0); limit=5 if kind=='产品资料' else 3
        selected[kind]=grouped[kind][offset:offset+limit]
        if offset+limit<len(grouped[kind]): next_offsets[kind]=offset+limit
    return selected,{'snapshot_id':snapshot_id,'complete':not next_offsets,'next_cursor':{'schema':'personal-kb.generation-cursor.v1','snapshot_id':snapshot_id,'offsets':next_offsets} if next_offsets else None,'selected_count':{k:len(v) for k,v in selected.items()}}
