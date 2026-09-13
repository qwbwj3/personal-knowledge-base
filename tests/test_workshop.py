"""Synthetic regressions only. No user files, OCR models, network, or real claims.

Queue storage and hashing are real. CLI boundary and OCR launches use explicit
mocks. The filename/format tests describe current limits, not full Office fidelity.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/personal-knowledge-base/scripts'))
import visual_review as vr
import visual_review_queue as queue
import personal_kb as kb
import pdf_pipeline
import ocr_runtime
import ocr_worker


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pkb-workshop-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'; self.source.mkdir()
        self.path = self.source / 'synthetic.pdf'; self.path.write_bytes(b'synthetic-source-not-a-pdf')
        self.sha = vr.file_hash(self.path)
        self.store = vr.Store(self.root / 'reviews', self.source)
        self.version = pdf_pipeline.EXTRACTION_VERSION
        self.catalog = {'release_id': 'synthetic-r1', 'current': [], 'leads': [], 'maintenance': []}
        self.counter = 0

    def request(self, page=1, *, sha=None, version=None, variant=''):
        row = {'schema': vr.VERSION, 'source_relative': self.path.name,
               'source_sha256': sha or self.sha, 'extraction_version': version or self.version,
               'page': page, 'raw_id': variant, 'candidates': {'native': 'synthetic text'},
               'page_record': {}}
        identity = vr.digest(row)
        dest = self.store.root / 'requests' / (identity + '.json')
        vr._write(dest, dict(row, request_id=identity))
        self.counter += 1
        os.utime(dest, ns=(1_000_000_000 + self.counter, 1_000_000_000 + self.counter))
        return identity

    def accepted(self, identity, *, revoked=False):
        row = {'request_id': identity, 'source_sha256': self.sha, 'status': 'accepted',
               'text_sha256': hashlib.sha256(b'synthetic text').hexdigest()}
        key = vr.digest(row)
        ledger = self.store.ledger(); ledger['decisions'][key] = row
        if revoked: ledger['revoked'].append(key)
        vr._write(self.store.root / 'ledger.json', ledger)
        return {'decision_id': key, **{k: row[k] for k in ('request_id','text_sha256')}}

    def published(self, *, certs=None, lead=False):
        row = {'source_relative': self.path.name, 'sha256': self.sha,
               'extraction': {'extraction_version': self.version, 'complete_text_coverage': True,
                              'visual_review_decisions': certs or []}}
        self.catalog['leads' if lead else 'current'].append(row)

    def active(self, *ids):
        self.catalog['maintenance'] = [{'file': self.path.name, 'extraction': {
            'extraction_version': self.version, 'complete_text_coverage': False,
            'visual_review_requests': [{'request_id': v} for v in ids]}}]

    def collect(self):
        return queue.collect(self.store, self.catalog)

    def invoke(self, **changes):
        args = dict(name=None, kb_id='synthetic', prepare=None, decision_file=None,
                    confirm_proposal=None, revoke_decision=None, limit=20, offset=0, queue_id=None,
                    vision_capability='unknown', model_image_egress_approved=None, confirmation=None)
        args.update(changes)
        api = types.SimpleNamespace(state_home=lambda a: self.root,
            select_kb=lambda *a: ({}, self.root / 'kbs' / 'id', {}),
            operation_lock=lambda *a: contextlib.nullcontext(),
            effective_control=lambda *a: {'authorization': {'read': True,'model_context': True}},
            verify_source_identity=lambda *a: (self.source, {}),
            current_commit=lambda *a: ({}, self.root / 'release', {}),
            read_json=lambda *a: self.catalog, privacy_flags=lambda x: [], KBError=RuntimeError)
        class Finished(Exception): pass
        def emit(value, code=0):
            api.result, api.code = value, code
            raise Finished
        api.emit = emit
        with patch.object(vr, 'Store', return_value=self.store):
            try: vr.command(api, argparse.Namespace(**args))
            except Finished: pass
        return api

    def test_01_published_success_removes_33_history_tasks_readonly(self):
        for page in range(1,34): self.request(page)
        self.published()
        before = {p.name: p.read_bytes() for p in (self.store.root/'requests').iterdir()}
        value = self.collect()
        self.assertEqual(value['pending_count'], 0)
        self.assertEqual(value['items'], [])
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.store.root/'requests').iterdir()})

    def test_02_readable_lead_is_not_a_visual_task(self):
        self.request(); self.published(lead=True)
        self.assertEqual(self.collect()['pending_count'],0)

    def test_03_historical_snapshot_alone_does_not_suppress(self):
        self.request(); self.published()
        self.catalog['snapshots'] = self.catalog.pop('current')
        self.assertEqual(self.collect()['pending_count'],1)

    def test_04_failed_current_request_wins_over_prior_same_hash(self):
        self.request(variant='old'); latest=self.request(variant='new')
        self.published(); self.active(latest)
        self.assertEqual([i['request_id'] for i in self.collect()['items']],[latest])

    def test_05_accepted_record_needs_update_not_another_look(self):
        identity=self.request(); self.active(identity); self.accepted(identity)
        value=self.collect()
        self.assertEqual(value['pending_count'],0)
        self.assertEqual(value['awaiting_update_count'],1)
        self.assertEqual(value['items'][0]['next_action'],'update')

    def test_06_acceptance_for_different_request_does_not_approve_new_one(self):
        old=self.request(variant='old'); self.accepted(old)
        new=self.request(variant='new'); self.active(new)
        self.assertEqual(self.collect()['pending_count'],1)

    def test_07_changed_source_requires_new_review(self):
        old=self.request(); self.accepted(old); self.published()
        self.path.write_bytes(b'new source revision')
        self.sha=vr.file_hash(self.path)
        new=self.request(variant='new'); self.active(new)
        result=self.collect()
        self.assertEqual([r['request_id'] for r in result['items']],[new])
        self.assertEqual(result['pending_count'],1)

    def test_08_old_extractor_cannot_approve_current_request(self):
        old=self.request(version='old-extractor'); self.accepted(old)
        new=self.request(variant='new'); self.active(new)
        self.assertEqual([r['request_id'] for r in self.collect()['items']],[new])
        self.assertEqual(self.collect()['awaiting_update_count'],0)

    def test_09_revoked_published_certificate_is_not_resolved(self):
        identity=self.request(); cert=self.accepted(identity,revoked=True)
        self.published(certs=[cert])
        self.assertEqual(self.collect()['pending_count'],1)

    def test_10_valid_published_certificate_supersedes_old_requests(self):
        self.request(variant='old'); identity=self.request(variant='current')
        self.published(certs=[self.accepted(identity)])
        self.assertEqual(self.collect()['items'],[])

    def test_11_invalid_ledger_hash_is_not_acceptance(self):
        identity=self.request(); cert=self.accepted(identity)
        ledger=self.store.ledger();ledger['decisions'][cert['decision_id']]['text_sha256']='wrong'
        vr._write(self.store.root/'ledger.json',ledger)
        self.assertEqual(self.collect()['pending_count'],1)

    def test_12_source_unavailable_is_explicit_and_not_a_new_view_task(self):
        self.request(); self.path.rename(self.root/'moved-source')
        result=self.collect()
        self.assertEqual(result['source_revision_unavailable_count'],1)
        self.assertEqual(result['pending_count'],0)

    def test_13_source_hash_is_checked_once_for_many_pages(self):
        for page in range(1,34): self.request(page)
        with patch.object(self.store,'source',wraps=self.store.source) as read:
            result=self.collect()
        self.assertEqual(result['pending_count'],33)
        self.assertEqual(read.call_count,1)

    def test_14_pagination_33_returns_20_then_13_without_loss(self):
        for page in range(1,34):self.request(page)
        result=self.collect(); a=queue.paginate(result)
        b=queue.paginate(result,offset=a['next_offset'],expected_queue_id=a['queue_id'])
        self.assertEqual((a['items_returned'],b['items_returned']),(20,13))
        self.assertTrue(a['has_more']);self.assertFalse(b['has_more'])
        self.assertEqual(a['items']+b['items'],result['items'])

    def test_15_queue_identity_change_rejects_paged_continuation(self):
        self.request();first=self.collect();self.catalog['release_id']='synthetic-r2'
        with self.assertRaisesRegex(ValueError,'changed'):
            queue.paginate(self.collect(),expected_queue_id=first['queue_id'])

    def test_16_pagination_invalid_limits_and_offsets_rejected(self):
        for limit,offset in [(0,0),(101,0),(20,-1),(True,0),(20,True)]:
            with self.subTest(limit=limit,offset=offset),self.assertRaises(ValueError):
                queue.paginate(self.collect(),limit=limit,offset=offset)

    def test_17_no_vision_and_no_current_tasks_does_not_block(self):
        self.request();self.published()
        api=self.invoke(vision_capability='unavailable')
        self.assertEqual(api.code,0)
        self.assertEqual(api.result['status'],'no_visual_review_needed')
        self.assertEqual(api.result['pending_count'],0)

    def test_18_no_vision_pending_has_complete_paging_metadata(self):
        for page in range(1,34):self.request(page)
        api=self.invoke(vision_capability='unavailable')
        self.assertEqual(api.code,3)
        self.assertEqual(api.result['pending_count'],33)
        self.assertEqual(api.result['items_total'],33)
        self.assertEqual(api.result['next_offset'],20)

    def test_19_no_vision_accepted_pending_publish_requests_update(self):
        identity=self.request();self.active(identity);self.accepted(identity)
        api=self.invoke(vision_capability='unavailable')
        self.assertEqual(api.code,0)
        self.assertEqual(api.result['status'],'visual_review_update_required')

    def test_20_prepare_missing_auth_names_both_parameters(self):
        identity=self.request()
        with self.assertRaisesRegex(RuntimeError,'--model-image-egress-approved.*--confirmation'):
            self.invoke(prepare=identity,vision_capability='available')

    def test_21_stale_prepare_rejected_even_when_vision_unavailable(self):
        identity=self.request();self.published()
        with self.assertRaisesRegex(RuntimeError,'resolved'):
            self.invoke(prepare=identity,vision_capability='unavailable')

    def test_22_prepare_active_uses_authorized_current_request(self):
        identity=self.request()
        with patch.object(self.store,'prepare',return_value={'status':'prepared_test_double'}) as prepare:
            api=self.invoke(prepare=identity,model_image_egress_approved='yes',confirmation='Synthetic explicit page authorization')
        prepare.assert_called_once_with(identity,'Synthetic explicit page authorization')
        self.assertEqual(api.result['status'],'prepared_test_double')


    def test_33_real_publication_lookup_resolves_history_without_rebuilding(self):
        # Only PDF text extraction is replaced. Establish, source snapshots,
        # release hashes, authorization checks and queue CLI are actual code.
        state = self.root / 'state'
        data = pdf_pipeline._finish([{'page': 1, 'status': 'usable',
            'text': 'Synthetic workshop note with enough content for a complete safe test.',
            'method': 'synthetic-native', 'quality': {'readable': True, 'reasons': []}}], 'synthetic-native')
        def invoke(argv):
            output = io.StringIO()
            with patch.object(sys,'argv',['personal_kb.py','--state-home',str(state),*argv]), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as result:
                    kb.main()
            return result.exception.code, json.loads(output.getvalue())
        with patch.object(kb,'page_extract_pdf',return_value=data):
            code, established = invoke(['establish','--name','Synthetic workshop test',
                '--source-root',str(self.source),'--model-context-egress-approved','yes',
                '--confirmation','Synthetic-only source authorization for automated regression','--apply'])
        self.assertEqual(code,0,established)
        info=json.loads((state/'registry.json').read_text(encoding='utf-8'))['knowledge_bases'][0]
        self.store=vr.Store(vr.store_path(Path(info['root'])),self.source)
        self.request()
        before = {str(p.relative_to(state)): vr.file_hash(p) for p in state.rglob('*') if p.is_file()}
        code, result=invoke(['visual-review','--kb-id',info['id'],'--vision-capability','unavailable'])
        self.assertEqual(code,0,result)
        self.assertEqual(result['pending_count'],0)
        self.assertEqual(result['status'],'no_visual_review_needed')
        after = {str(p.relative_to(state)): vr.file_hash(p) for p in state.rglob('*') if p.is_file()}
        self.assertEqual(before,after)


class InputAndFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='pkb-input-contract-')
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()

    def test_23_canonical_utf8_decision_input(self):
        p=self.root/'decision.json';p.write_text('{"reason":"合成复核"}',encoding='utf-8')
        self.assertEqual(vr.read_decision_input(p),{'reason':'合成复核'})

    def test_24_symlinked_child_remains_rejected(self):
        p=self.root/'actual.json';p.write_text('{}',encoding='utf-8')
        alias=self.root/'linked.json'
        # Inject only the symlink detection; exercises policy on non-privileged Windows.
        original=Path.is_symlink
        with patch.object(Path,'is_symlink',lambda x:x==alias or original(x)),self.assertRaisesRegex(ValueError,'decision-file'):
            vr.read_decision_input(alias)

    def test_25_known_macos_tmp_alias_is_canonicalized_before_validation(self):
        original_resolve=Path.resolve;original_link=Path.is_symlink
        with patch.object(vr.sys,'platform','darwin'), \
                patch.object(Path,'is_symlink',lambda p:True if p==Path('/tmp') else original_link(p)), \
                patch.object(Path,'resolve',lambda p,**kw:Path('/private/tmp') if p==Path('/tmp') else original_resolve(p,**kw)), \
                patch.object(Path,'is_file',return_value=True), \
                patch.object(vr,'_read',return_value={'ok':True}) as read:
            self.assertEqual(vr.read_decision_input('/tmp/sample.json'),{'ok':True})
        read.assert_called_once_with(Path('/private/tmp/sample.json'))

    def test_26_parent_traversal_remains_rejected(self):
        with self.assertRaisesRegex(ValueError,'parent traversal'):
            vr.read_decision_input(self.root/'x'/'..'/'decision.json')

    def test_27_image_limit_structured_error_not_model_repair(self):
        cp=subprocess.CompletedProcess([],2,json.dumps({'code':'image_input_limit_exceeded','repair_required':False}),'')
        with patch.object(ocr_runtime,'_runtime',side_effect=lambda p:p),patch.object(ocr_runtime,'run_owned',return_value=cp):
            with self.assertRaises(ocr_runtime.OCRInputLimitError) as error:
                ocr_runtime._run_worker(self.root,['image','synthetic.png'])
        self.assertFalse(error.exception.repair_required)
        self.assertNotIn('Setup/repair:',str(error.exception))

    def test_28_input_limit_not_wrapped_with_setup_command(self):
        p=self.root/'input.png';p.write_bytes(b'synthetic')
        with patch.object(ocr_runtime,'probe',return_value={'ready':True}), \
                patch.object(ocr_runtime,'_run_worker',side_effect=ocr_runtime.OCRInputLimitError('limits')):
            with self.assertRaises(ocr_runtime.OCRInputLimitError):ocr_runtime.recognize_image(p)

    def test_29_worker_limit_response_fixed_no_setup(self):
        out,err=io.StringIO(),io.StringIO()
        with patch.object(sys,'argv',['ocr_worker.py','--home',str(self.root),'image','fake.png']), \
                patch.object(ocr_worker,'run',side_effect=ocr_worker.ImageInputLimitError()), \
                contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            self.assertEqual(ocr_worker.main(),2)
        self.assertEqual(json.loads(out.getvalue())['code'],'image_input_limit_exceeded')
        self.assertEqual(err.getvalue(),'')

    def test_30_current_format_allowlist_and_legacy_office_limits(self):
        self.assertTrue({'.pdf','.docx','.xlsx','.csv','.txt','.md','.html','.png','.jpeg','.jpg','.webp'}<=kb.SUPPORTED)
        self.assertFalse({'.doc','.xls','.ppt','.pptx','.tiff','.gif'}&kb.SUPPORTED)

    def test_31_docx_body_and_cell_text_are_flattened_not_table_layout(self):
        p=self.root/'synthetic.docx'
        xml=f'<w:document xmlns:w="{kb.W[1:-1]}"><w:body><w:p><w:r><w:t>正文</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>单元格</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
        with zipfile.ZipFile(p,'w') as z:z.writestr('word/document.xml',xml)
        self.assertEqual(kb.extract_docx(p),'正文\n\n单元格')

    def test_32_xlsx_raw_number_and_absent_formula_cache_not_display_values(self):
        cell=ET.fromstring(f'<c xmlns="{kb.S[1:-1]}" r="C2" s="1"><v>0.8</v></c>')
        self.assertEqual(kb.xlsx_value(cell,[]),'0.8')
        formula=ET.fromstring(f'<c xmlns="{kb.S[1:-1]}" r="C3"><f>1+1</f></c>')
        self.assertEqual(kb.xlsx_value(formula,[]),'')


if __name__=='__main__':unittest.main(verbosity=2)
