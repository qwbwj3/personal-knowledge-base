"""Bounded, read-only XLSX structure. Reading a saved cache is NOT recalculation.

No Excel macros, evaluation, external resources, or network are executed here.
Unknown number formats and shared-formula expansion are explicitly represented.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import PurePosixPath
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from material_common import XLSX_VERSION, file_hash

MAX_MEMBERS = 5000
MAX_UNPACKED = 100 * 1024 * 1024
MAX_MEMBER = 32 * 1024 * 1024
MAX_CELLS = 100000
MAX_TEXT = 8 * 1024 * 1024
ADDRESS = re.compile(r'^\$?([A-Z]{1,3})\$?([1-9][0-9]{0,6})$')
BUILTIN = {0:'General',1:'0',2:'0.00',3:'#,##0',4:'#,##0.00',9:'0%',10:'0.00%',
           14:'mm-dd-yy',15:'d-mmm-yy',16:'d-mmm',17:'mmm-yy',18:'h:mm AM/PM',19:'h:mm:ss AM/PM',
           20:'h:mm',21:'h:mm:ss',22:'m/d/yy h:mm',49:'@'}


def point(address):
    match = ADDRESS.fullmatch(str(address).upper())
    if not match: raise ValueError('invalid_cell_address')
    col = 0
    for c in match[1]: col = col * 26 + ord(c) - 64
    row = int(match[2])
    if col > 16384 or row > 1048576: raise ValueError('cell_outside_xlsx_grid')
    return row, col


def address(row, col):
    result = ''
    while col:
        col, rem = divmod(col - 1, 26); result = chr(65 + rem) + result
    return result + str(row)


def rectangle(value):
    parts = str(value).split(':')
    if len(parts) > 2: raise ValueError('invalid_range')
    a, b = point(parts[0]), point(parts[-1])
    if a[0] > b[0] or a[1] > b[1]: raise ValueError('reversed_range')
    return *a, *b


def _xml(archive, name):
    raw = archive.read(name)
    upper = raw.upper().replace(b'\x00', b'')
    if b'<!DOCTYPE' in upper or b'<!ENTITY' in upper: raise ValueError('xml_entities_not_supported')
    return ET.fromstring(raw)


def _target(base, target):
    if not target or '\\' in target or ':' in target: raise ValueError('unsafe_workbook_relationship')
    value = posixpath.normpath(posixpath.join(posixpath.dirname(base), target)) if not target.startswith('/') else target[1:]
    if value.startswith('../') or value == '..' or not value.startswith('xl/'):
        raise ValueError('workbook_relationship_outside_package')
    return value


def _text(element):
    # Rich text runs concatenate; phonetic annotations are not cell text.
    if element is None: return ''
    return ''.join(node.text or '' for node in element.findall('{*}t') + element.findall('{*}r/{*}t'))


def _display(raw, fmt, date1904=False):
    if raw is None: return None, 'blank'
    if isinstance(raw, bool): return 'TRUE' if raw else 'FALSE', 'exact'
    if not isinstance(raw, (int, float, Decimal)):
        return str(raw), 'exact_text'
    num = Decimal(str(raw))
    if fmt in ('General', '@'): return str(raw), 'raw_general'
    # Explicitly small renderer: preserve format code for all other cases.
    match = re.fullmatch(r'(\$|¥|￥)?(#,##0|0)(?:\.(0{1,12}))?(%)?', fmt)
    if match:
        symbol, grouping, decimals, percent = match.groups()
        places = len(decimals or '')
        value = num * (100 if percent else 1)
        try: rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        except InvalidOperation: return str(raw), 'unsupported_precision_preserved'
        return (symbol or '') + format(rounded, (',' if grouping.startswith('#') else '') + f'.{places}f') + ('%' if percent else ''), 'supported_format'
    dates = {'yyyy-mm-dd':'%Y-%m-%d','yyyy/mm/dd':'%Y/%m/%d','mm-dd-yy':'%m-%d-%y',
             'm/d/yy':'%m/%d/%y','m/d/yyyy':'%m/%d/%Y','yyyy-mm-dd hh:mm:ss':'%Y-%m-%d %H:%M:%S',
             'm/d/yy h:mm':'%m/%d/%y %H:%M','h:mm':'%H:%M','h:mm:ss':'%H:%M:%S'}
    if fmt.lower() in dates:
        if not date1904 and 60 <= num < 61: return '1900-02-29 (Excel compatibility date)', 'excel_1900_leap_day'
        days = float(num) - (1 if not date1904 and num >= 61 else 0)
        try: return ((datetime(1904,1,1) if date1904 else datetime(1899,12,31)) + timedelta(days=days)).strftime(dates[fmt.lower()]), 'supported_format'
        except (OverflowError, ValueError): pass
    return str(raw), 'unsupported_format_preserved'


def read_workbook(path):
    before = file_hash(path)
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist(); names = [i.filename for i in infos]
        if len(infos) > MAX_MEMBERS or len(names) != len(set(names)): raise ValueError('xlsx_member_count_or_duplicates')
        if sum(i.file_size for i in infos) > MAX_UNPACKED or any(i.file_size > MAX_MEMBER for i in infos):
            raise ValueError('xlsx_expansion_limit')
        for info in infos:
            p = PurePosixPath(info.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in info.filename or info.flag_bits & 1:
                raise ValueError('unsafe_xlsx_member')
        book = _xml(archive,'xl/workbook.xml')
        props = book.find('{*}workbookPr'); date1904 = props is not None and props.get('date1904') in ('1','true')
        rels = _xml(archive,'xl/_rels/workbook.xml.rels')
        targets = {n.get('Id'):_target('xl/workbook.xml',n.get('Target')) for n in rels
                   if n.get('TargetMode') != 'External'}
        external = any(n.get('TargetMode') == 'External' for n in rels)
        # Scan relationship records too; embedded links must never be refreshed.
        for name in names:
            if name.endswith('.rels'):
                external |= any(n.get('TargetMode') == 'External' for n in _xml(archive,name))
        formats, styles, shared = {}, [0], []
        if 'xl/styles.xml' in names:
            style = _xml(archive,'xl/styles.xml')
            formats = {int(n.get('numFmtId')): n.get('formatCode','General') for n in style.findall('{*}numFmts/{*}numFmt')}
            styles = [int(n.get('numFmtId','0')) for n in style.findall('{*}cellXfs/{*}xf')] or [0]
        if 'xl/sharedStrings.xml' in names:
            shared = [_text(n) for n in _xml(archive,'xl/sharedStrings.xml').findall('{*}si')]
        output = {'schema':XLSX_VERSION,'source_sha256':before,'date_system':'1904' if date1904 else '1900',
            'sheets':[], 'defined_names':[dict(n.attrib, text=n.text or '') for n in book.findall('{*}definedNames/{*}definedName')],
            'calculation_properties':dict(book.find('{*}calcPr').attrib) if book.find('{*}calcPr') is not None else {},
            'features':{'macros':any('vbaProject' in n or 'macrosheet' in n.lower() for n in names),
                        'external_links': external or any(n.startswith('xl/externalLinks/') for n in names),
                        'connections':any(n in ('xl/connections.xml',) or n.startswith('xl/queryTables/') for n in names),
                        'drawings':any(n.startswith('xl/drawings/') for n in names),
                        'images':any(n.startswith('xl/media/') for n in names),
                        'charts':any(n.startswith('xl/charts/') for n in names)},
            'tables': [], 'coverage':'cell_records_not_all_visual_or_semantic_content', 'warnings':[]}
        count = 0
        for sheet in book.findall('{*}sheets/{*}sheet'):
            rid = next((v for k,v in sheet.attrib.items() if k.endswith('}id')),None)
            name = targets.get(rid)
            if not name or name not in names: raise ValueError('missing_worksheet_relationship')
            node = _xml(archive,name)
            if node.tag.rsplit('}',1)[-1] != 'worksheet': raise ValueError('non_worksheet_sheet_not_supported')
            dimension = node.find('{*}dimension')
            record = {'name':sheet.get('name'),'state':sheet.get('state','visible'),
                'declared_range': dimension.get('ref') if dimension is not None else None,
                'cells':{},'merged_ranges':[n.get('ref') for n in node.findall('{*}mergeCells/{*}mergeCell')],
                'hidden_rows':[], 'column_properties':[dict(n.attrib) for n in node.findall('{*}cols/{*}col')],
                'formula_masters':{}}
            for ref in record['merged_ranges']: rectangle(ref)
            for row in node.findall('{*}sheetData/{*}row'):
                if row.get('hidden') in ('1','true'): record['hidden_rows'].append(int(row.get('r')))
                for cell in row.findall('{*}c'):
                    count += 1
                    if count > MAX_CELLS: raise ValueError('xlsx_cell_limit')
                    coord = cell.get('r'); r,c = point(coord)
                    coord = address(r,c)
                    if coord in record['cells']: raise ValueError('duplicate_cell_address')
                    kind = cell.get('t','n'); val = cell.find('{*}v'); raw = val.text if val is not None else None
                    value = raw
                    if kind == 's':
                        if raw is None or not 0 <= int(raw) < len(shared): raise ValueError('invalid_shared_string')
                        value = shared[int(raw)]
                    elif kind == 'inlineStr': value = _text(cell.find('{*}is'))
                    elif kind == 'b':
                        if raw not in (None,'0','1'):raise ValueError('invalid_boolean_cell')
                        value = None if raw is None else raw == '1'
                    elif kind == 'n' and raw is not None:
                        number = Decimal(raw)
                        if not number.is_finite() or len(raw)>100 or abs(number.adjusted())>308: raise ValueError('nonfinite_or_large_number')
                        value = int(number) if number == number.to_integral() else float(number)
                    form = cell.find('{*}f'); formula = None
                    if form is not None:
                        if len(form.text or '') > 8192: raise ValueError('formula_length_limit')
                        formula = {'text':form.text or '', 'attributes':dict(form.attrib)}
                        if form.get('t')=='shared' and form.text:
                            record['formula_masters'][form.get('si')] = {'address':coord,**formula}
                    style_id = int(cell.get('s','0'))
                    if not 0 <= style_id < len(styles): raise ValueError('invalid_style_index')
                    code = formats.get(styles[style_id], BUILTIN.get(styles[style_id], 'unsupported_builtin_'+str(styles[style_id])))
                    display,status = _display(value,code,date1904)
                    record['cells'][coord] = {'address':coord,'row':r,'column':c,'type':kind,'raw_value':raw,
                        'value':value,'formula':formula,'cached_value':value if formula else None,
                        'cache_status':('missing' if val is None else 'error' if kind=='e' else 'saved_cache_unverified') if formula else 'not_formula',
                        'number_format':code,'display_text':display,'display_status':status}
            for cell in record['cells'].values():
                f = cell['formula']
                if f and f['attributes'].get('t')=='shared' and not f['text']:
                    f['master'] = record['formula_masters'].get(f['attributes'].get('si'))
                    f['expansion_status'] = 'engine_required_not_an_empty_cell'
            coords = [(x['row'],x['column']) for x in record['cells'].values()]
            record['used_range'] = (address(min(x[0] for x in coords),min(x[1] for x in coords))+':'+address(max(x[0] for x in coords),max(x[1] for x in coords))) if coords else None
            output['sheets'].append(record)
        for name in names:
            if name.startswith('xl/tables/') and name.endswith('.xml'):
                table = _xml(archive,name)
                output['tables'].append({'name':table.get('name'),'range':table.get('ref'),
                    'columns':[dict(n.attrib) for n in table.findall('{*}tableColumns/{*}tableColumn')]})
        if len({s['name'] for s in output['sheets']})!=len(output['sheets']): raise ValueError('duplicate_sheet_name')
        output['cell_count']=count
        for key in ('images','charts','drawings'):
            if output['features'][key]: output['warnings'].append(key+'_present_not_interpreted')
    if file_hash(path)!=before: raise ValueError('source_changed_during_workbook_read')
    return output


def range_read(book, sheet_name, ref):
    sheets = [s for s in book['sheets'] if s['name']==sheet_name]
    if len(sheets)!=1: raise ValueError('unknown_worksheet')
    sheet=sheets[0]; r0,c0,r1,c1 = rectangle(ref)
    if (r1-r0+1)*(c1-c0+1)>2000: raise ValueError('range_limit_2000_cells')
    rows=[]
    for r in range(r0,r1+1):
        row=[]
        for c in range(c0,c1+1):
            at=address(r,c); value=dict(sheet['cells'].get(at,{'address':at,'row':r,'column':c,'value':None,'formula':None,'cache_status':'not_formula','display_text':None,'type':'blank'}))
            for merged in sheet['merged_ranges']:
                a,b,x,y=rectangle(merged)
                if a<=r<=x and b<=c<=y: value.update(merged_range=merged,merged_anchor=address(a,b));break
            value['hidden_row']=r in sheet['hidden_rows']
            value['hidden_column']=any(int(x['min'])<=c<=int(x['max']) and x.get('hidden') in ('1','true') for x in sheet['column_properties'])
            row.append(value)
        rows.append(row)
    return {'source_sha256':book['source_sha256'],'sheet':sheet_name,'range':ref,'rows':rows,
            'formula_values_are_saved_cache':True,'warnings':book['warnings']}


def extract(path):
    book=read_workbook(path);lines=['# XLSX 结构化提取','公式缓存仅为文件上次保存值，未经重新计算。']
    for sheet in book['sheets']:
        lines.append('## 工作表 '+sheet['name']+' ('+sheet['state']+')')
        for cell in sorted(sheet['cells'].values(),key=lambda x:(x['row'],x['column'])):
            line=f"[{sheet['name']}!{cell['address']}] 显示={cell['display_text']!r}; 原值={cell['value']!r}; 格式={cell['number_format']}"
            if cell['formula']: line+='; 公式='+str(cell['formula'])+'; 缓存状态='+cell['cache_status']
            lines.append(line)
        if sheet['merged_ranges']:lines.append('合并范围='+','.join(sheet['merged_ranges']))
    text='\n'.join(lines)
    if len(text.encode('utf-8'))>MAX_TEXT:raise ValueError('xlsx_text_limit_no_silent_truncation')
    return text,'XLSX结构化单元格/公式读取',{'material_version':XLSX_VERSION,'complete_text_coverage':True,
        'coverage':book['coverage'],'workbook':book,'warnings':book['warnings']}


def validate_calculation(book, request):
    if not isinstance(request,dict) or set(request)!= {'inputs','outputs'}: raise ValueError('calculation_inputs_outputs_required')
    if any(book['features'][k] for k in ('macros','external_links','connections')):raise ValueError('active_or_external_workbook_blocked')
    risky=re.compile(r'\b(?:WEBSERVICE|RTD|HYPERLINK|IMAGE|STOCKHISTORY|FILTERXML|CALL|REGISTER|EXEC|EVALUATE|RUN|PY|SQL.REQUEST)\s*\(|\[|\||(?:https?|file)://',re.I)
    formulas=[c['formula']['text'] for s in book['sheets'] for c in s['cells'].values() if c['formula']]
    formulas += [n['text'] for n in book['defined_names']]
    if any(risky.search(f) for f in formulas):raise ValueError('external_or_network_formula_blocked')
    if not isinstance(request['inputs'],list) or not isinstance(request['outputs'],list) or len(request['inputs'])>1000 or not 1<=len(request['outputs'])<=200:raise ValueError('calculation_size_limit')
    sheets={s['name']:s for s in book['sheets']}; seen=set()
    for item in request['inputs']:
        if not isinstance(item,dict) or set(item)!= {'sheet','cell','value'}:raise ValueError('input_literal_required')
        rc=point(item['cell'])
        if item['cell']!=address(*rc):raise ValueError('canonical_cell_address_required')
        key=(item['sheet'],item['cell'])
        if item['sheet'] not in sheets or key in seen:raise ValueError('duplicate_or_unknown_input')
        seen.add(key);val=item['value']
        if not (val is None or type(val) in (str,int,float,bool)) or (isinstance(val,str) and (val.startswith('=') or len(val)>32767)):
            raise ValueError('literal_input_only_no_formula_injection')
        if sheets[item['sheet']]['cells'].get(item['cell'],{}).get('formula'):raise ValueError('scenario_input_must_not_replace_formula')
        if isinstance(val,float) and (val!=val or abs(val)==float('inf')):raise ValueError('nonfinite_input')
    seen=set()
    for item in request['outputs']:
        if not isinstance(item,dict) or set(item)!= {'sheet','cell'} or item['sheet'] not in sheets:raise ValueError('invalid_output_cell')
        rc=point(item['cell'])
        if item['cell']!=address(*rc):raise ValueError('canonical_cell_address_required')
        key=(item['sheet'],item['cell'])
        if key in seen:raise ValueError('duplicate_output')
        seen.add(key)
    return request
