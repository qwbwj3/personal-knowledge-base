"""Synthetic material regression: real ZIP/image/KB IO, explicit host assertions.

An 'image_actually_viewed' field in these fixtures is a test double, NOT evidence
of a model reading the fixture. Optional actual spreadsheet engine tests are run
separately and are never replaced by arithmetic mocks called a real engine.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'skills/personal-knowledge-base/scripts'))
from material_common import digest,file_hash,write,IMAGE_VERSION,XLSX_VERSION
import image_material as im
import workbook_material as wm
import material_flow as mf
import material_worker
import personal_kb as kb

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'

def xlsx(path,*,sheetxml=None,external=False,macro=False,date1904=False):
    cells='''<row r="1"><c r="A1" t="inlineStr"><is><t>年度</t></is></c><c r="C1" t="inlineStr"><is><t>比率</t></is></c></row>
<row r="2"><c r="A2"><v>100</v></c><c r="C2" s="1"><v>0.8</v></c><c r="D2" s="2"><v>1234.5</v></c><c r="E2" s="3"><v>61</v></c><c r="F2"><f>A2*Data!A1</f><v>1</v></c><c r="G2"><f>SUM(A2,Data!A1)</f></c></row>
<row r="3" hidden="1"><c r="A3"><f t="shared" si="0" ref="A3:A4">A2+1</f><v>101</v></c><c r="C3" t="e"><f>1/0</f><v>#DIV/0!</v></c></row>
<row r="4"><c r="A4"><f t="shared" si="0"/><v>102</v></c></row>'''
    data={'[Content_Types].xml':f'''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>''',
    '_rels/.rels':f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    'xl/workbook.xml':f'<workbook xmlns="{NS}" xmlns:r="{REL}"><workbookPr date1904="{int(date1904)}"/><sheets><sheet name="计划" sheetId="1" r:id="rId1"/><sheet name="Data" sheetId="2" state="hidden" r:id="rId2"/></sheets><calcPr calcId="1" fullCalcOnLoad="1"/></workbook>',
    'xl/_rels/workbook.xml.rels':f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{REL}/worksheet" Target="worksheets/sheet2.xml"/>'+ (f'<Relationship Id="rId3" Type="{REL}/externalLink" Target="https://example.invalid/data" TargetMode="External"/>' if external else '')+f'<Relationship Id="rId4" Type="{REL}/styles" Target="styles.xml"/></Relationships>',
    'xl/styles.xml':f'''<styleSheet xmlns="{NS}"><numFmts count="2"><numFmt numFmtId="164" formatCode="$#,##0.00"/><numFmt numFmtId="165" formatCode="yyyy-mm-dd"/></numFmts><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0"/></cellStyleXfs><cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="9"/><xf numFmtId="164"/><xf numFmtId="165"/></cellXfs></styleSheet>''',
    'xl/worksheets/sheet1.xml':sheetxml or f'<worksheet xmlns="{NS}"><dimension ref="A1:G4"/><cols><col min="2" max="2" hidden="1"/></cols><sheetData>{cells}</sheetData><mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells></worksheet>',
    'xl/worksheets/sheet2.xml':f'<worksheet xmlns="{NS}"><sheetData><row r="1"><c r="A1"><v>3</v></c></row></sheetData></worksheet>'}
    if macro:data['xl/vbaProject.bin']='not executable synthetic macro flag'
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        for name,value in data.items():z.writestr(name,value)
    return path

def region(task,*,blocks=None):
    tile=task['tiles'][0]
    return {'task_id':task['task_id'],'tile_id':tile['tile_id'],'image_sha256':tile['sha256'],
        'overview_sha256':task['overview']['sha256'],'reviewer':'synthetic-host-judgment',
        'status':'complete','checks':dict.fromkeys(im.CHECKS,True),
        'blocks':blocks if blocks is not None else [{'type':'text','bbox':[1,1,90,20],'text':'Synthetic workshop material: premiums and coverage.'}], 'unresolved':[]}

def finish(task,tables=None):
    return {'task_id':task['task_id'],'tables':tables or [],'reviewer':'synthetic-host-judgment','layout_checked':True,'unresolved':[]}

class WorkbookTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='pkb-book-');self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve();self.path=xlsx(self.root/'example.xlsx')
        self.book=wm.read_workbook(self.path)
    def test_sparse_range_preserves_blank_and_merge_anchor(self):
        result=wm.range_read(self.book,'计划','A1:C2')
        self.assertEqual(result['rows'][1][1]['address'],'B2')
        self.assertIsNone(result['rows'][1][1]['value'])
        self.assertEqual(result['rows'][0][1]['merged_anchor'],'A1')
        self.assertTrue(result['rows'][1][1]['hidden_column'])
    def test_formula_and_stale_cache_are_separate(self):
        c=self.book['sheets'][0]['cells']['F2']
        self.assertEqual(c['formula']['text'],'A2*Data!A1');self.assertEqual(c['cached_value'],1)
        self.assertEqual(c['cache_status'],'saved_cache_unverified')
        self.assertEqual(self.book['sheets'][0]['cells']['G2']['cache_status'],'missing')
    def test_percentage_currency_date_formats(self):
        cells=self.book['sheets'][0]['cells']
        self.assertEqual(cells['C2']['display_text'],'80%')
        self.assertEqual(cells['D2']['display_text'],'$1,234.50')
        self.assertEqual(cells['E2']['display_text'],'1900-03-01')
    def test_dates_1904_and_compatibility_serial60(self):
        self.assertEqual(wm._display(0,'yyyy-mm-dd',True)[0],'1904-01-01')
        self.assertEqual(wm._display(60,'yyyy-mm-dd')[1],'excel_1900_leap_day')
    def test_shared_formula_has_master_not_blank_value(self):
        c=self.book['sheets'][0]['cells']['A4']
        self.assertEqual(c['formula']['master']['address'],'A3')
        self.assertEqual(c['formula']['expansion_status'],'engine_required_not_an_empty_cell')
    def test_hidden_rows_and_sheet_retained(self):
        self.assertEqual(self.book['sheets'][1]['state'],'hidden')
        self.assertTrue(wm.range_read(self.book,'计划','A3:A3')['rows'][0][0]['hidden_row'])
    def test_unknown_format_keeps_raw_and_code(self):
        self.assertEqual(wm._display(0.8,'[Red]0.00;[Blue]-0.00'),('0.8','unsupported_format_preserved'))
    def test_extract_includes_coordinate_and_formula(self):
        text,method,meta=wm.extract(self.path)
        self.assertIn('[计划!F2]',text);self.assertIn('A2*Data!A1',text)
        self.assertEqual(meta['material_version'],XLSX_VERSION)
        self.assertEqual(meta['workbook']['source_sha256'],file_hash(self.path))
    def test_bounded_range_and_unknown_sheet(self):
        for name,ref in [('计划','A1:Z999'),('not-a-sheet','A1'),('计划','C2:A1')]:
            with self.subTest(name=name,ref=ref),self.assertRaises(ValueError):wm.range_read(self.book,name,ref)
    def test_xml_entities_rejected(self):
        p=xlsx(self.root/'entity.xlsx',sheetxml='<!DOCTYPE x [<!ENTITY a "boom">]><x/>')
        with self.assertRaises(ValueError):wm.read_workbook(p)
    def test_archive_traversal_and_duplicates_rejected(self):
        p=self.root/'bad.xlsx'
        with zipfile.ZipFile(p,'w') as z:z.writestr('../outside','x')
        with self.assertRaises(ValueError):wm.read_workbook(p)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(p,'w') as z:z.writestr('x','a');z.writestr('x','b')
        with self.assertRaises(ValueError):wm.read_workbook(p)
    def test_archive_and_cell_limits(self):
        with patch.object(wm,'MAX_UNPACKED',10),self.assertRaises(ValueError):wm.read_workbook(self.path)
        with patch.object(wm,'MAX_CELLS',2),self.assertRaises(ValueError):wm.read_workbook(self.path)
    def test_formula_errors_not_numeric_zero(self):
        self.assertEqual(self.book['sheets'][0]['cells']['C3']['cache_status'],'error')
        self.assertEqual(self.book['sheets'][0]['cells']['C3']['value'],'#DIV/0!')
    def test_duplicate_alias_coordinates_rejected(self):
        p=xlsx(self.root/'dupe.xlsx',sheetxml=f'<worksheet xmlns="{NS}"><sheetData><row r="1"><c r="A1"><v>1</v></c><c r="a1"><v>2</v></c></row></sheetData></worksheet>')
        with self.assertRaises(ValueError):wm.read_workbook(p)
    def test_calc_literals_outputs_and_formula_preservation(self):
        request={'inputs':[{'sheet':'计划','cell':'A2','value':200}],'outputs':[{'sheet':'计划','cell':'F2'}]}
        self.assertEqual(wm.validate_calculation(self.book,request),request)
        for cell in ('F2','f2','$F$2'):
            request['inputs'][0]['cell']=cell
            with self.subTest(cell=cell),self.assertRaises(ValueError):wm.validate_calculation(self.book,request)
    def test_calc_rejects_external_macro_network_and_formula_input(self):
        request={'inputs':[],'outputs':[{'sheet':'计划','cell':'F2'}]}
        for key in ('external_links','macros','connections'):
            bad=copy.deepcopy(self.book);bad['features'][key]=True
            with self.subTest(key=key),self.assertRaises(ValueError):wm.validate_calculation(bad,request)
        bad=copy.deepcopy(self.book);bad['sheets'][0]['cells']['F2']['formula']['text']='WEBSERVICE("https://example.invalid")'
        with self.assertRaises(ValueError):wm.validate_calculation(bad,request)
        request['inputs']=[{'sheet':'计划','cell':'A2','value':'=1+1'}]
        with self.assertRaises(ValueError):wm.validate_calculation(self.book,request)
    def test_parser_reports_external_and_macro_not_execute(self):
        p=xlsx(self.root/'active.xlsx',external=True,macro=True)
        value=wm.read_workbook(p)
        self.assertTrue(value['features']['external_links']);self.assertTrue(value['features']['macros'])
    def test_missing_calculation_engine_is_not_cache_success(self):
        job={'path':str(self.path),'source_sha256':file_hash(self.path),'request':{'inputs':[],'outputs':[{'sheet':'计划','cell':'F2'}]}}
        with patch.object(material_worker.importlib.util,'find_spec',return_value=None):result=material_worker.recalculate(job)
        self.assertFalse(result['executed']);self.assertEqual(result['status'],'engine_required');self.assertNotIn('outputs',result)
    def test_rich_text_excludes_phonetic_annotations(self):
        import xml.etree.ElementTree as ET
        xml=ET.fromstring(f'<si xmlns="{NS}"><r><t>正常</t></r><rPh sb="0" eb="2"><t>拼音</t></rPh><r><t>文字</t></r></si>')
        self.assertEqual(wm._text(xml),'正常文字')

class ImageTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        tmp=tempfile.TemporaryDirectory(prefix='pkb-image-');self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve();self.source=self.root/'materials';self.source.mkdir()
        self.path=self.source/'tiny.png';Image.new('RGB',(100,100),'white').save(self.path)
        self.store=mf.Store(self.root/'state',self.source)
        manifest=im.prepare(self.path,self.store.root/'images'/'packet',file_hash(self.path))
        self.task={**manifest,'source_relative':self.path.name,'packet_relative':'images/packet','image_authorization':'synthetic test'}
        self.task['task_id']=digest(self.task);write(self.store.root/'tasks'/(self.task['task_id']+'.json'),self.task)
    def test_grid_covers_3240_6800_once_with_original_resolution(self):
        g=im.grid(3240,6800);self.assertEqual(len(g),8)
        self.assertEqual(sum((t['core_box'][2]-t['core_box'][0])*(t['core_box'][3]-t['core_box'][1]) for t in g),3240*6800)
        self.assertEqual(g[-1]['core_box'][2:],[3240,6800])
    def test_large_source_pixels_match_each_generated_tile(self):
        from PIL import Image
        path=self.source/'long.png'
        image=Image.new('RGB',(3240,6800),'white');image.putpixel((2000,2000),(12,34,56));image.save(path);image.close()
        before=file_hash(path);packet=im.prepare(path,self.root/'big',before)
        self.assertEqual(len(packet['tiles']),8);self.assertFalse(packet['downsampled_tiles']);self.assertEqual(before,file_hash(path))
        with Image.open(path) as source:
            for t in packet['tiles']:
                with Image.open(self.root/'big'/t['file']) as got:
                    self.assertEqual(got.size,(t['view_box'][2]-t['view_box'][0],t['view_box'][3]-t['view_box'][1]))
                    self.assertEqual(got.tobytes(),source.crop(t['view_box']).tobytes())
    def test_exif_orientation_is_recorded(self):
        from PIL import Image
        path=self.source/'rotated.jpg';x=Image.new('RGB',(100,50),'white');exif=Image.Exif();exif[274]=6;x.save(path,exif=exif)
        packet=im.prepare(path,self.root/'rotated',file_hash(path))
        self.assertEqual((packet['width'],packet['height']),(50,100));self.assertEqual(packet['source_orientation'],6)
    def test_region_roundtrip_finish_one_original(self):
        self.store.submit_region(region(self.task))
        output=self.store.finish_image(finish(self.task));self.assertEqual(output['source_documents'],1);self.assertFalse(output['published'])
        text,method,meta=self.store.extraction(self.path,file_hash(self.path))
        self.assertIn('Synthetic workshop material',text);self.assertTrue(self.store.valid_meta(meta,file_hash(self.path)))
    def test_no_complete_without_all_regions(self):
        with self.assertRaises(ValueError):self.store.finish_image(finish(self.task))
    def test_unresolved_region_retains_progress_not_admission(self):
        observation=region(self.task);observation['status']='unresolved';observation['unresolved']=['text unreadable'];observation['checks']['no_unresolved_content']=False
        value=self.store.submit_region(observation);self.assertEqual(value['progress']['complete'],0)
        with self.assertRaises(ValueError):self.store.finish_image(finish(self.task))
    def test_wrong_hash_and_no_actual_look_rejected(self):
        observation=region(self.task);observation['image_sha256']='0'*64
        with self.assertRaises(ValueError):self.store.submit_region(observation)
        observation=region(self.task);observation['checks']['image_actually_viewed']=False
        with self.assertRaises(ValueError):self.store.submit_region(observation)
    def test_task_or_source_changes_invalidate(self):
        self.path.write_bytes(b'new revision')
        with self.assertRaises(ValueError):self.store.submit_region(region(self.task))
    def test_prepared_image_change_rejected(self):
        (self.store.root/self.task['packet_relative']/self.task['tiles'][0]['file']).write_bytes(b'changed')
        with self.assertRaises(ValueError):self.store.task(self.task['task_id'])
    def test_table_blank_cells_explicit_and_complete(self):
        cells=[{'type':'table_cell','table_id':'plan','row':0,'column':c,'text':val,'bbox':[c*30+1,25,c*30+29,45]} for c,val in enumerate(['100','','80%'])]
        self.store.submit_region(region(self.task,blocks=cells))
        output=self.store.finish_image(finish(self.task,[{'table_id':'plan','title':'表格','columns':['金额','空列','比例'],'rows':1}]))
        text,_,meta=self.store.extraction(self.path,file_hash(self.path));self.assertIn('列2 空列',text);self.assertIn('80%',text)
        self.assertEqual(len(meta['blocks']),3)
    def test_missing_or_duplicate_table_cells_not_whole_image_success(self):
        table=[{'table_id':'t','title':'t','columns':['a','b'],'rows':1}]
        cell={'type':'table_cell','table_id':'t','row':0,'column':0,'text':'1','bbox':[1,1,20,20]}
        for blocks in ([cell],[cell,cell]):
            with self.subTest(count=len(blocks)),self.assertRaises(ValueError):
                im.finish(self.task,{self.task['tiles'][0]['tile_id']:region(self.task,blocks=blocks)},finish(self.task,table))
    def test_context_cannot_duplicate_neighbor_core(self):
        task={'task_id':'a'*64,'overview':{'sha256':'b'*64},'tiles':[{'tile_id':'r0c1','core_box':[1792,0,3240,1792],'view_box':[1696,0,3240,1888],'sha256':'c'*64}]}
        observation=region(task,blocks=[{'type':'text','text':'neighbor','bbox':[1700,1,1750,20]}])
        with self.assertRaises(ValueError):im.validate_region(task,observation)
    def test_revoked_content_is_not_reusable(self):
        self.store.submit_region(region(self.task));r=self.store.finish_image(finish(self.task));_,_,meta=self.store.extraction(self.path,file_hash(self.path))
        self.store.revoke(r['result_id']);self.assertFalse(self.store.valid_meta(meta,file_hash(self.path)));self.assertIsNone(self.store.extraction(self.path,file_hash(self.path)))
    def test_size_budget_no_unbounded_decompression(self):
        for w,h in [(0,1),(40001,1),(10000,10000)]:
            with self.subTest(w=w,h=h),self.assertRaises(ValueError):im.grid(w,h)
    def test_resumed_preparation_does_not_repeat_rendering(self):
        with patch.object(mf,'worker',side_effect=AssertionError('should reuse exact task')):
            self.assertEqual(self.store.prepare_image(self.path.name,'same scope')['task_id'],self.task['task_id'])
    def test_reuse_never_crosses_original_path(self):
        with self.assertRaises(ValueError):self.store.source('../outside.png')

class FlowTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='pkb-flow-');self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve();self.source=self.root/'materials';self.source.mkdir();self.state=self.root/'state'
    def invoke(self,args):
        out=io.StringIO()
        with patch.object(sys,'argv',['personal_kb.py','--state-home',str(self.state),*args]),contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as exited:kb.main()
        return exited.exception.code,json.loads(out.getvalue())
    def establish(self):
        code,out=self.invoke(['establish','--name','Synthetic materials','--source-root',str(self.source),'--model-context-egress-approved','yes','--confirmation','Synthetic document authorization','--apply'])
        self.assertEqual(code,0,out)
        entry=json.loads((self.state/'registry.json').read_text(encoding='utf-8'))['knowledge_bases'][0]
        return entry,out
    def test_image_and_workbook_normal_ingestion_revoke_and_stop(self):
        from PIL import Image
        path=self.source/'notes.png';Image.new('RGB',(100,100),'white').save(path)
        self.source.joinpath('notes.md').write_text('Synthetic content for an ordinary readable control document.',encoding='utf-8')
        xlsx(self.source/'model.xlsx');original=file_hash(path)
        info,est=self.establish();self.assertEqual(len(est['not_imported']),1)
        self.assertTrue(est['not_imported'][0]['extraction']['requires_host_vision'])
        code,out=self.invoke(['material','image-prepare','--file',path.name,'--vision-capability','unavailable']);self.assertEqual(code,3);self.assertEqual(out['status'],'host_vision_unavailable')
        code,task=self.invoke(['material','image-prepare','--file',path.name,'--vision-capability','available','--image-egress-approved','yes','--confirmation','Synthetic images specifically authorized'])
        self.assertEqual(code,0,task)
        f=self.root/'region.json';write(f,region(task));code,out=self.invoke(['material','image-region','--input',str(f)]);self.assertEqual(code,0,out)
        f=self.root/'finish.json';write(f,finish(task));code,result=self.invoke(['material','image-complete','--input',str(f)]);self.assertEqual(code,0,result)
        code,up=self.invoke(['update']);self.assertEqual(code,0,up);self.assertEqual(up['not_imported'],[])
        self.assertEqual(file_hash(path),original)
        code,book=self.invoke(['material','workbook','--file','model.xlsx','--sheet','计划','--range','A2:G2']);self.assertEqual(code,0,book);self.assertEqual(book['rows'][0][1]['address'],'B2')
        code,out=self.invoke(['material','revoke','--result-id',result['result_id']]);self.assertEqual(code,0,out)
        config=json.loads((Path(info['root'])/'config.json').read_text(encoding='utf8'))
        view=kb.resolve_effective_view(Path(info['root']),config)
        self.assertNotIn('notes.png',[i['source_relative'] for i in view['effective_current']])
        stopped=self.root/'stop.json';write(stopped,{'model.xlsx':{'状态':'停用'}})
        code,out=self.invoke(['update','--decisions',str(stopped)]);self.assertEqual(code,0,out)
        with patch.object(wm,'read_workbook',side_effect=AssertionError('stopped source must not be parsed')):
            code,out=self.invoke(['material','workbook','--file','model.xlsx'])
        self.assertEqual(code,2,out)
    def test_calculation_task_and_host_receipt_no_cache_substitution(self):
        xlsx(self.source/'model.xlsx');info,_=self.establish()
        request={'inputs':[{'sheet':'计划','cell':'A2','value':200}],'outputs':[{'sheet':'计划','cell':'F2'}]}
        f=self.root/'request.json';write(f,request)
        code,task=self.invoke(['material','calculate','--file','model.xlsx','--input',str(f),'--engine','host'])
        self.assertEqual(code,0,task);self.assertFalse(task['executed']);self.assertEqual(task['status'],'engine_required')
        payload={'task_id':task['task_id'],'source_sha256':task['source_sha256'],'inputs':request['inputs'],
                 'status':'calculated','executed':True,'engine':'synthetic-host-test-double',
                 'execution_evidence':'Synthetic receipt protocol test only, not actual engine calculation','executed_at':'2026-01-01T00:00:00Z',
                 'outputs':[{'sheet':'计划','cell':'F2','value':600}]}
        result=self.root/'result.json';write(result,payload)
        code,out=self.invoke(['material','calculation-result','--input',str(result)])
        self.assertEqual(code,0,out);self.assertEqual(out['evidence_scope'],'host_report_not_independently_verified');self.assertFalse(out['published_as_product_evidence'])
        payload['source_sha256']='0'*64;write(result,payload)
        code,out=self.invoke(['material','calculation-result','--input',str(result)]);self.assertEqual(code,2)
    def test_read_revocation_checked_before_source_content(self):
        xlsx(self.source/'model.xlsx');info,_=self.establish()
        # Effective-control guard boundary is an explicit mock; actual parsing must not start.
        with patch.object(kb,'effective_control',return_value={'authorization':{'read':False,'model_context':True}}),patch.object(wm,'read_workbook',side_effect=AssertionError('must not read')):
            code,out=self.invoke(['material','workbook','--file','model.xlsx'])
        self.assertEqual(code,2);self.assertIn('授权',out['message'])

if __name__=='__main__':unittest.main(verbosity=2)
