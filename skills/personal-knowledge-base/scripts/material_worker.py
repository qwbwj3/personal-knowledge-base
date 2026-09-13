"""Owned, bounded worker for direct image preparation and optional recalculation.

artifact_tool is a host capability, not a public dependency users must install.
Never substitute stored Excel caches when the engine is unavailable or fails.
"""
from __future__ import annotations
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from material_common import read, file_hash, safe

ERROR_VALUES = {'#REF!','#DIV/0!','#VALUE!','#NAME?','#N/A','#NUM!','#NULL!','#SPILL!','#CALC!','#CIRC!'}


def recalculate(job):
    from workbook_material import read_workbook, validate_calculation
    path=safe(job['path'])
    if file_hash(path)!=job['source_sha256']:raise ValueError('calculation_source_changed')
    book_info=read_workbook(path);request=validate_calculation(book_info,job['request'])
    if importlib.util.find_spec('artifact_tool') is None:
        return {'status':'engine_required','engine':'artifact_tool','executed':False,
                'message':'当前Python没有可用的表格计算引擎；公式和结构仍可读取。使用宿主已有表格工具，不安装未知内部包。'}
    # Bound cold-start inside the existing owned-worker budget; no global change.
    os.environ.setdefault('ARTIFACT_TOOL_RPC_DAEMON_STARTUP_TIMEOUT_S','25')
    from artifact_tool import Blob, SpreadsheetFile
    # Import into a new engine workbook. No export, no original writes.
    book=SpreadsheetFile.import_xlsx(Blob.load(str(path)))
    for item in request['inputs']:
        book.worksheets.get_item(item['sheet']).get_range(item['cell']).values=[[item['value']]]
    book.recalculate()
    values=[]
    for item in request['outputs']:
        matrix=book.worksheets.get_item(item['sheet']).get_range(item['cell']).values
        if not isinstance(matrix,list) or len(matrix)!=1 or len(matrix[0])!=1:raise ValueError('unexpected_engine_output_shape')
        value=matrix[0][0]
        original=next(s for s in book_info['sheets'] if s['name']==item['sheet'])['cells'].get(item['cell'],{})
        if value is None and original.get('formula'):
            return {'status':'calculation_incomplete','engine':'artifact_tool','executed':True,'error_code':'missing_formula_result','output':item}
        if isinstance(value,str) and value.startswith('#'):
            return {'status':'calculation_incomplete','engine':'artifact_tool','executed':True,'error_code':'formula_error','output':item,'value':value}
        values.append({**item,'value':value})
    if file_hash(path)!=job['source_sha256']:raise ValueError('source_changed_during_calculation')
    return {'status':'calculated','engine':'artifact_tool','executed':True,
            'execution_evidence':'import_xlsx -> set literal inputs -> recalculate -> read output cells',
            'executed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'outputs':values,
            'source_unchanged':True,'evidence_scope':'direct_local_engine_execution_not_semantic_validation'}


def main():
    try:
        job=read(sys.argv[1])
        with open(os.devnull,'w',encoding='utf-8') as sink, contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
            if job['action']=='image':
                from image_material import prepare
                result={'status':'prepared','manifest':prepare(job['path'],job['output'],job['source_sha256'])}
            elif job['action']=='calculate':result=recalculate(job)
            else:raise ValueError('unknown_material_action')
        print(json.dumps(result,ensure_ascii=True,allow_nan=False))
        return 0
    except ImportError:
        print(json.dumps({'status':'capability_missing','code':'material_dependency_unavailable','executed':False}));return 2
    except Exception:
        # Do not leak filenames, document text, formulas or local credentials in
        # process errors. The parent already knows the scoped task identity.
        print(json.dumps({'status':'blocked','code':'material_worker_failed','executed':False}));return 2


if __name__=='__main__':raise SystemExit(main())
