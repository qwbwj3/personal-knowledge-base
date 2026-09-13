"""Release regressions using synthetic OOXML, never learner documents.

Worker doubles below validate error handling only, not actual recalculation.
Real host-engine acceptance is executed separately where that engine exists.
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_multiformat import NS, xlsx, wm, material_worker, file_hash, mf


class WorkbookReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pkb-xlsx-release-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / '工作簿.xlsx'

    def book(self, cells):
        return wm.read_workbook(xlsx(self.path, sheetxml=f'<worksheet xmlns="{NS}"><sheetData>{cells}</sheetData></worksheet>'))

    def test_empty_numeric_cache_is_missing_but_empty_text_cache_is_valid(self):
        book = self.book('<row r="1"><c r="A1"><f>1+1</f><v/></c><c r="B1" t="str"><f>IF(1,"",2)</f><v/></c><c r="C1" t="str"><f>IF(1,"",2)</f></c></row>')
        cells = book['sheets'][0]['cells']
        self.assertEqual(cells['A1']['cache_status'], 'missing')
        self.assertIsNone(cells['A1']['cached_value'])
        self.assertEqual(cells['B1']['cache_status'], 'saved_cache_unverified')
        self.assertEqual(cells['B1']['cached_value'], '')
        self.assertEqual(cells['C1']['cache_status'], 'missing')

    def test_array_children_keep_formula_relationship_even_without_cell_record(self):
        book = self.book('<row r="1"><c r="A1"><f t="array" ref="A1:A3">ROW(A1:A3)</f><v>1</v></c></row><row r="2"><c r="A2"><v>2</v></c></row>')
        second = book['sheets'][0]['cells']['A2']
        self.assertEqual(second['formula']['master']['address'], 'A1')
        self.assertEqual(second['cache_status'], 'saved_cache_unverified')
        third = wm.range_read(book, '计划', 'A3')['rows'][0][0]
        self.assertEqual(third['formula']['range'], 'A1:A3')
        self.assertEqual(third['cache_status'], 'missing')
        for cell in ('A1','A2','A3'):
            with self.subTest(cell=cell), self.assertRaisesRegex(ValueError, 'must_not_replace_formula'):
                wm.validate_calculation(book, {'inputs':[{'sheet':'计划','cell':cell,'value':10}], 'outputs':[{'sheet':'计划','cell':'A1'}]})

    def test_shared_range_missing_follower_formula_does_not_lose_master(self):
        book = self.book('<row r="1"><c r="A1"><f t="shared" si="0" ref="A1:A2">B1+1</f><v>1</v></c></row><row r="2"><c r="A2"><v>2</v></c></row>')
        self.assertEqual(book['sheets'][0]['cells']['A2']['formula']['master']['address'], 'A1')

    def test_formula_master_outside_its_range_rejected(self):
        with self.assertRaisesRegex(ValueError, 'formula_master_outside_range'):
            self.book('<row r="1"><c r="A1"><f t="array" ref="B1:B2">1</f><v>1</v></c></row>')

    def test_merge_anchor_data_available_when_requested_range_starts_inside_merge(self):
        book = wm.read_workbook(xlsx(self.path))
        cell = wm.range_read(book, '计划', 'B1')['rows'][0][0]
        self.assertEqual(cell['merged_anchor'], 'A1')
        self.assertEqual(cell['merged_anchor_value'], '年度')
        self.assertIsNone(cell['value'])
        with self.assertRaisesRegex(ValueError, 'must_use_merged_anchor'):
            wm.validate_calculation(book, {'inputs':[{'sheet':'计划','cell':'B1','value':'新标题'}], 'outputs':[{'sheet':'计划','cell':'F2'}]})

    def test_date_time_display_matches_excel_padding_without_platform_flags(self):
        for raw,fmt,expected in [(61,'m/d/yy','3/1/00'),(61,'m/d/yyyy','3/1/1900'),
                                 (61.25,'m/d/yy h:mm','3/1/00 6:00'),(60.25,'h:mm','6:00'),
                                 (61.25,'yyyy-mm-dd hh:mm:ss','1900-03-01 06:00:00')]:
            with self.subTest(fmt=fmt):
                self.assertEqual(wm._display(raw,fmt), (expected,'supported_format'))

    def test_quoted_currency_and_negative_sign(self):
        self.assertEqual(wm._display(-1234.5,'$#,##0.00'), ('-$1,234.50','supported_format'))
        self.assertEqual(wm._display(1234.5,'"￥"#,##0.00'), ('￥1,234.50','supported_format'))
        self.assertEqual(wm._display(.125,'0.00%'), ('12.50%','supported_format'))

    def test_host_receipt_rejects_missing_array_child_without_cell_record(self):
        self.book('<row r="1"><c r="A1"><f t="array" ref="A1:A3">ROW(A1:A3)</f><v>1</v></c></row>')
        store = mf.Store(self.path.parent/'state', self.path.parent)
        task = store.calculation_task(self.path.name, {'inputs':[], 'outputs':[{'sheet':'计划','cell':'A3'}]})
        receipt = {'status':'calculated','executed':True,'engine':'explicit-test-double',
                   'execution_evidence':'validation double, not real calculation','executed_at':'2026-09-13T00:00:00Z',
                   'outputs':[{'sheet':'计划','cell':'A3','value':None}]}
        with self.assertRaisesRegex(ValueError, 'missing_formula_result'):
            store.save_calculation(task, receipt, 'host_report_not_independently_verified')

    def test_host_receipt_allows_hash_literal_but_rejects_excel_errors(self):
        xlsx(self.path)
        store = mf.Store(self.path.parent/'state', self.path.parent)
        task = store.calculation_task(self.path.name, {'inputs':[], 'outputs':[{'sheet':'计划','cell':'F2'}]})
        receipt = {'status':'calculated','executed':True,'engine':'explicit-test-double',
                   'execution_evidence':'validation double, not real calculation','executed_at':'2026-09-13T00:00:00Z',
                   'outputs':[{'sheet':'计划','cell':'F2','value':'#客户编号'}]}
        self.assertEqual(store.save_calculation(task, receipt, 'host_report_not_independently_verified')['outputs'][0]['value'],'#客户编号')
        for error in ('#DIV/0!','#N/A','#NAME?','#SPILL!','#GETTING_DATA'):
            receipt['outputs'][0]['value'] = error
            with self.subTest(error=error), self.assertRaisesRegex(ValueError,'formula_error'):
                store.save_calculation(task, receipt, 'host_report_not_independently_verified')

    def run_worker_double(self, result):
        xlsx(self.path)
        # Deliberate double, not evidence that a spreadsheet engine ran.
        class Sheet:
            def get_range(self, _): return types.SimpleNamespace(values=result)
        book = types.SimpleNamespace(worksheets=types.SimpleNamespace(get_item=lambda _:Sheet()), recalculate=lambda:None)
        fake = types.SimpleNamespace(Blob=types.SimpleNamespace(load=lambda _:None),
                                     SpreadsheetFile=types.SimpleNamespace(import_xlsx=lambda _:book))
        job = {'path':str(self.path), 'source_sha256':file_hash(self.path),
               'request':{'inputs':[], 'outputs':[{'sheet':'计划','cell':'F2'}]}}
        with patch.dict(sys.modules, {'artifact_tool':fake}), patch.object(material_worker.importlib.util, 'find_spec', return_value=object()):
            return material_worker.recalculate(job)

    def test_worker_rejects_nonfinite_or_structured_engine_outputs(self):
        for value in (float('nan'),float('inf'), {'value':600}, [600]):
            with self.subTest(value=value):
                result=self.run_worker_double([[value]])
                self.assertEqual(result['status'],'calculation_incomplete')
                self.assertEqual(result['error_code'],'invalid_engine_value')

    def test_worker_error_value_fails_but_hash_prefixed_literal_does_not(self):
        self.assertEqual(self.run_worker_double([['#DIV/0!']])['status'], 'calculation_incomplete')
        result = self.run_worker_double([['#客户编号']])
        self.assertEqual(result['outputs'][0]['value'], '#客户编号')


if __name__ == '__main__':
    unittest.main()
