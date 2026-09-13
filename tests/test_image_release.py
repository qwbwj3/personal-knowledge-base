"""Release regressions using synthetic images and explicit host test doubles.

These checks exercise pixels, persistence and query ingestion; they do not claim
that a real WorkBuddy/model has visually read the synthetic observation text.
"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_multiformat as fixtures
from PIL import Image

im=fixtures.im
mf=fixtures.mf
file_hash=fixtures.file_hash
write=fixtures.write
region=fixtures.region
finish=fixtures.finish


class ImageReleaseTests(unittest.TestCase):
    def setUp(self):
        fixtures.ImageTests.setUp(self)

    def test_corrected_result_replaces_cache_and_remains_current_after_reload(self):
        self.store.submit_region(region(self.task))
        first=self.store.finish_image(finish(self.task))
        _,_,old_meta=self.store.extraction(self.path,file_hash(self.path))
        corrected=region(self.task)
        corrected['blocks'][0]['text']='Corrected premium is 3200; annual coverage is 800000.'
        self.store.submit_region(corrected)
        second=self.store.finish_image(finish(self.task))
        self.assertNotEqual(first['result_id'],second['result_id'])
        self.assertEqual(second['superseded_results'],1)
        self.store=mf.Store(self.store.root,self.source)
        text,_,meta=self.store.extraction(self.path,file_hash(self.path))
        self.assertIn('Corrected premium',text)
        self.assertFalse(self.store.valid_meta(old_meta,file_hash(self.path)))
        self.assertTrue(self.store.valid_meta(meta,file_hash(self.path)))
        self.assertIn(first['result_id'],self.store.ledger()['images'])
        with patch.object(mf,'worker',side_effect=AssertionError('current packet must resume')):
            resumed=self.store.prepare_image(self.path.name,'same explicit test scope')
        self.assertEqual(resumed['task_id'],self.task['task_id'])
        self.assertEqual(resumed['progress']['complete'],1)
        # Retrying a completed request is idempotent, not a new publication.
        self.assertEqual(self.store.finish_image(finish(self.task))['result_id'],second['result_id'])
        self.store.revoke(second['result_id'])
        self.assertIsNone(self.store.extraction(self.path,file_hash(self.path)))
        self.assertFalse(self.store.valid_meta(meta,file_hash(self.path)))

    def test_failed_completion_keeps_previous_complete_result(self):
        self.store.submit_region(region(self.task))
        self.store.finish_image(finish(self.task))
        _,_,meta=self.store.extraction(self.path,file_hash(self.path))
        unresolved=region(self.task)
        unresolved.update(status='unresolved',unresolved=['Amount needs rechecking'])
        unresolved['checks']['no_unresolved_content']=False
        self.store.submit_region(unresolved)
        with self.assertRaises(ValueError):self.store.finish_image(finish(self.task))
        self.assertTrue(self.store.valid_meta(meta,file_hash(self.path)))
        self.assertEqual(self.store.progress(self.task)['complete'],0)

    def test_lost_packet_can_be_prepared_again_without_touching_original(self):
        self.store.submit_region(region(self.task))
        missing=self.store.root/self.task['packet_relative']/self.task['tiles'][0]['file']
        missing.unlink()
        before=file_hash(self.path)
        task=self.store.prepare_image(self.path.name,'synthetic recovery image authorization')
        self.assertNotEqual(task['task_id'],self.task['task_id'])
        self.assertEqual(task['progress']['reviewed'],0)
        self.assertTrue(Path(task['tiles'][0]['path']).is_file())
        self.assertEqual(file_hash(self.path),before)
        self.assertTrue((self.store.root/'regions'/self.task['task_id']/'r0c0.json').exists())

    def test_partial_multiregion_progress_resumes_and_requires_last_region(self):
        path=self.source/'long.png'
        with Image.new('RGB',(100,im.CORE+200),'white') as image:image.save(path)
        task=self.store.prepare_image(path.name,'synthetic long image scope')
        self.assertEqual(len(task['tiles']),2)
        first=region(task)
        self.store.submit_region(first)
        with self.assertRaises(ValueError):self.store.finish_image(finish(task))
        resumed=mf.Store(self.store.root,self.source).prepare_image(path.name,'same scope')
        self.assertEqual(resumed['task_id'],task['task_id'])
        self.assertEqual(resumed['progress']['remaining'],['r1c0'])
        second=copy.deepcopy(first)
        second.update(tile_id='r1c0',image_sha256=task['tiles'][1]['sha256'])
        second['blocks']=[{'type':'text','bbox':[1,im.CORE+10,90,im.CORE+40],
                           'text':'Last original region: verified synthetic tail marker.'}]
        self.store.submit_region(second)
        self.store.finish_image(finish(task))
        text,_,meta=self.store.extraction(path,file_hash(path))
        self.assertIn('tail marker',text)
        self.assertEqual(meta['source_regions'],2)
        self.assertEqual({b['tile_id'] for b in meta['blocks']},{'r0c0','r1c0'})

    def test_transparent_png_keeps_black_ink_on_white_background(self):
        for mode in ('RGBA','P'):
            with self.subTest(mode=mode):
                path=self.source/(mode+'.png')
                if mode=='RGBA':
                    source=Image.new('RGBA',(50,50),(0,0,0,0))
                    source.putpixel((10,10),(0,0,0,255))
                else:
                    source=Image.new('P',(50,50),0)
                    source.putpalette([0,0,0,0,0,0]+[0,0,0]*254)
                    source.info['transparency']=0
                    source.putpixel((10,10),1)
                source.save(path);source.close()
                folder=self.root/('transparent-'+mode)
                manifest=im.prepare(path,folder,file_hash(path))
                with Image.open(folder/manifest['tiles'][0]['file']) as got:
                    self.assertEqual(got.getpixel((0,0)),(255,255,255))
                    self.assertEqual(got.getpixel((10,10)),(0,0,0))


class ImagePublicationTests(unittest.TestCase):
    setUp=fixtures.FlowTests.setUp
    invoke=fixtures.FlowTests.invoke
    establish=fixtures.FlowTests.establish

    def test_corrected_image_is_ingested_and_discoverable_with_original_source(self):
        path=self.source/'premium.png'
        with Image.new('RGB',(100,100),'white') as image:image.save(path)
        self.source.joinpath('notes.md').write_text('Synthetic ordinary readable control document.',encoding='utf-8')
        info,_=self.establish()
        code,task=self.invoke(['material','image-prepare','--file',path.name,
            '--vision-capability','available','--image-egress-approved','yes',
            '--confirmation','Synthetic images specifically authorized'])
        self.assertEqual(code,0,task)
        observation=region(task)
        request=self.root/'region.json'
        completion=self.root/'finish.json'
        write(completion,finish(task))
        for value in ('oldpremium 3100','correctedpremium 3200'):
            observation['blocks'][0]['text']='Synthetic policy annual '+value+' with original region source.'
            write(request,observation)
            code,out=self.invoke(['material','image-region','--input',str(request)])
            self.assertEqual(code,0,out)
            code,out=self.invoke(['material','image-complete','--input',str(completion)])
            self.assertEqual(code,0,out)
            code,out=self.invoke(['update'])
            self.assertEqual(code,0,out)
            self.assertEqual(out['not_imported'],[])
        code,out=self.invoke(['call','--task','query','--query','correctedpremium'])
        self.assertEqual(code,0,out)
        self.assertIn('premium.png',json.dumps(out,ensure_ascii=False))
        candidates=[i for i in out['candidates'] if i['file']=='premium.png']
        self.assertEqual(len(candidates),1)
        code,readout=self.invoke(['read-source','--request-json',json.dumps(candidates[0]['read_request'])])
        self.assertEqual(code,0,readout)
        self.assertIn('correctedpremium',readout['text'])
        self.assertNotIn('oldpremium',readout['text'])
        self.assertIn('r0c0 bbox=',readout['text'])
        self.assertEqual(readout['sha256'],file_hash(path))
        self.assertEqual(file_hash(Path(readout['original_file'])),file_hash(path))


if __name__=='__main__':unittest.main(verbosity=2)
