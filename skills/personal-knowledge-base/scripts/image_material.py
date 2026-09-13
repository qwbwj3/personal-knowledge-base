"""Original-resolution image reading packets and host-observed structured content.

No OCR/model API call. The host must actually inspect images; these validators
prove identity/shape/coverage only, not visual or financial correctness.
"""
from __future__ import annotations
from pathlib import Path
import math
import re
import warnings
from material_common import IMAGE_VERSION, file_hash, safe

CORE = 1792
MARGIN = 96
MAX_PIXELS = 80_000_000
MAX_SIDE = 40000
MAX_TILES = 128
ID = re.compile(r'^[A-Za-z0-9_-]{1,60}$')
CHECKS = {'image_actually_viewed','all_core_content_checked','numbers_units_checked','no_unresolved_content'}


def grid(width, height):
    if type(width) is not int or type(height) is not int or min(width,height)<1 or max(width,height)>MAX_SIDE or width*height>MAX_PIXELS:
        raise ValueError('image_size_limit')
    nx,ny=math.ceil(width/CORE),math.ceil(height/CORE)
    if nx*ny>MAX_TILES:raise ValueError('image_region_limit')
    return [{'tile_id':f'r{r}c{c}', 'core_box':[c*CORE,r*CORE,min((c+1)*CORE,width),min((r+1)*CORE,height)],
             'view_box':[max(0,c*CORE-MARGIN),max(0,r*CORE-MARGIN),min((c+1)*CORE+MARGIN,width),min((r+1)*CORE+MARGIN,height)]}
            for r in range(ny) for c in range(nx)]


def prepare(path, output, expected_sha):
    from PIL import Image, ImageOps
    path=safe(path);output=safe(output)
    if file_hash(path)!=expected_sha or output.exists():raise ValueError('changed_source_or_existing_image_output')
    output.mkdir(parents=True)
    with warnings.catch_warnings():
        warnings.simplefilter('error',Image.DecompressionBombWarning)
        with Image.open(path) as raw:
            if raw.format not in ('PNG','JPEG','WEBP') or getattr(raw,'n_frames',1)!=1:raise ValueError('unsupported_image_or_animation')
            grid(raw.width,raw.height)
            orientation=raw.getexif().get(274,1)
            image=ImageOps.exif_transpose(raw).convert('RGB')
        try:
            tiles=grid(image.width,image.height)
            overview=image.copy();overview.thumbnail((1280,1280));overview.save(output/'overview.png',compress_level=3);overview.close()
            for tile in tiles:
                crop=image.crop(tuple(tile['view_box']))
                name=tile['tile_id']+'.png';crop.save(output/name,compress_level=3)
                tile.update(file=name,sha256=file_hash(output/name),width=crop.width,height=crop.height)
                crop.close()
            result={'schema':IMAGE_VERSION,'source_sha256':expected_sha,'width':image.width,'height':image.height,
                    'source_orientation':orientation,'coordinates':'exif_oriented_original_pixels',
                    'overview':{'file':'overview.png','sha256':file_hash(output/'overview.png'),'purpose':'navigation_not_small_text_evidence'},
                    'tiles':tiles,'ocr_executed':False,'model_called':False,'downsampled_tiles':False}
        finally:image.close()
    if file_hash(path)!=expected_sha:raise ValueError('image_changed_during_preparation')
    return result


def _text(value, limit=32768):
    if not isinstance(value,str) or len(value)>limit or '\x00' in value:raise ValueError('invalid_observed_text')
    return value


def validate_region(task, region):
    expected={'task_id','tile_id','image_sha256','overview_sha256','reviewer','status','checks','blocks','unresolved'}
    if not isinstance(region,dict) or set(region)!=expected:raise ValueError('region_fields_mismatch')
    if region['task_id']!=task['task_id']:raise ValueError('wrong_image_task')
    tiles={t['tile_id']:t for t in task['tiles']}
    if region['tile_id'] not in tiles:raise ValueError('unknown_image_tile')
    tile=tiles[region['tile_id']]
    if region['image_sha256']!=tile['sha256'] or region['overview_sha256']!=task['overview']['sha256']:raise ValueError('observed_image_hash_mismatch')
    if not _text(region['reviewer'],200).strip():raise ValueError('actual_reviewer_required')
    if region['status'] not in ('complete','unresolved'):raise ValueError('invalid_region_status')
    if not isinstance(region['blocks'],list) or len(region['blocks'])>4000 or not isinstance(region['unresolved'],list):raise ValueError('region_size_or_unresolved_shape')
    for note in region['unresolved']:_text(note,2000)
    checks=region['checks']
    if not isinstance(checks,dict) or set(checks)!=CHECKS or any(type(v) is not bool for v in checks.values()):raise ValueError('region_checks_required')
    if region['status']=='complete' and (not all(checks.values()) or region['unresolved']):raise ValueError('incomplete_region_must_remain_unresolved')
    for block in region['blocks']:
        if not isinstance(block,dict) or block.get('type') not in ('text','table_cell'):raise ValueError('unsupported_observation_block')
        allowed={'type','bbox','text'}|({'table_id','row','column'} if block['type']=='table_cell' else set())
        if set(block)!=allowed:raise ValueError('block_fields_mismatch')
        _text(block['text'],4000 if block['type']=='table_cell' else 32768)
        box=block['bbox']
        if not isinstance(box,list) or len(box)!=4 or any(type(n) is not int for n in box):raise ValueError('integer_original_bbox_required')
        x0,y0,x1,y1=box; vx0,vy0,vx1,vy1=tile['view_box'];cx0,cy0,cx1,cy1=tile['core_box']
        if not (vx0<=x0<x1<=vx1 and vy0<=y0<y1<=vy1):raise ValueError('block_outside_view_region')
        if not (cx0<=(x0+x1)/2<cx1 and cy0<=(y0+y1)/2<cy1):raise ValueError('block_owned_by_another_core_no_duplicate_context')
        if block['type']=='table_cell':
            if not isinstance(block['table_id'],str) or not ID.fullmatch(block['table_id']):raise ValueError('invalid_table_id')
            if any(type(block[k]) is not int or block[k]<0 for k in ('row','column')):raise ValueError('invalid_table_cell_index')
    return region


def finish(task, regions, request):
    if set(request)!= {'task_id','tables','reviewer','layout_checked','unresolved'} or request['task_id']!=task['task_id']:
        raise ValueError('image_completion_fields_mismatch')
    if request['layout_checked'] is not True or request['unresolved']!=[] or not _text(request['reviewer'],200).strip():
        raise ValueError('image_layout_unresolved')
    expected={t['tile_id'] for t in task['tiles']}
    if set(regions)!=expected:raise ValueError('all_original_regions_must_be_reviewed')
    tables={}; lines=[]; cells={}; records=[]
    if not isinstance(request['tables'],list) or len(request['tables'])>100:raise ValueError('table_definition_limit')
    for table in request['tables']:
        if set(table)!= {'table_id','title','columns','rows'} or not ID.fullmatch(table['table_id']) or table['table_id'] in tables:raise ValueError('invalid_or_duplicate_table_definition')
        if not isinstance(table['columns'],list) or not 1<=len(table['columns'])<=200 or type(table['rows']) is not int or not 0<=table['rows']<=10000 or table['rows']*len(table['columns'])>100000:raise ValueError('table_shape_limit')
        _text(table['title'],1000)
        for name in table['columns']:_text(name,500)
        tables[table['table_id']]=table;cells[table['table_id']]={}
    for tile in task['tiles']:
        observation=validate_region(task,regions[tile['tile_id']])
        if observation['status']!='complete':raise ValueError('region_still_unresolved')
        for block in observation['blocks']:
            record={**block,'tile_id':tile['tile_id']};records.append(record)
            if block['type']=='text':
                lines.append(f"[原图区域 {tile['tile_id']} bbox={block['bbox']}] {block['text']}")
            else:
                key=block['table_id'];position=(block['row'],block['column'])
                if key not in tables or position in cells[key]:raise ValueError('undefined_table_or_duplicate_cell')
                table=tables[key]
                if block['row']>=table['rows'] or block['column']>=len(table['columns']):raise ValueError('cell_outside_declared_table')
                cells[key][position]=record
    for key,table in tables.items():
        if len(cells[key])!=table['rows']*len(table['columns']):raise ValueError('missing_table_cells_blank_cells_must_be_explicit')
        lines.append('# 表格 '+table['title']+' ('+key+')')
        for row in range(table['rows']):
            for col,name in enumerate(table['columns']):
                block=cells[key][row,col]
                lines.append(f"[表 {key} 数据行{row+1} 列{col+1} {name} 区域{block['tile_id']} bbox={block['bbox']}] {block['text']}")
    text='\n'.join(lines)
    if not text.strip() or len(text.encode('utf-8'))>8*1024*1024:raise ValueError('empty_or_oversize_transcription')
    return {'text':text,'tables':request['tables'],'blocks':records,'region_count':len(regions),
            'source_sha256':task['source_sha256'],'task_id':task['task_id'],'reviewer':request['reviewer'],
            'coverage':'all_core_regions_host_claimed_reviewed','semantic_accuracy_verified':False,
            'actual_visual_inspection_verifiable_by_software':False}
