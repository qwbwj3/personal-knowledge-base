"""Exercise public classification CLI roundtrips for newly ingested materials.

Image observations are explicit synthetic host doubles, not model-accuracy tests.
The command parser, ingestion, classification checks and retrieval run normally.
"""
import json
import unittest

import test_multiformat as fixtures
import classification_flow
from PIL import Image


class MaterialClassificationTests(unittest.TestCase):
    setUp = fixtures.FlowTests.setUp
    invoke = fixtures.FlowTests.invoke
    establish = fixtures.FlowTests.establish

    def classify_through_public_commands(self, relative, expected_version):
        code, content = self.invoke(['classification', '--source', relative])
        self.assertEqual(code, 0, content)
        self.assertEqual(content['extraction_version'], expected_version)
        self.assertTrue(content['complete'])
        review = {key: content[key] for key in (
            'source_relative', 'source_sha256', 'text_sha256', 'extraction_version', 'scope_id')}
        review.update(kind='个人业务资料', subtype='个人经验', metadata={},
                      reviewer_id='synthetic-classification-reviewer', content_read=True,
                      reason='Synthetic fixture contains readable personal business material.',
                      evidence=[{'start': 0, 'end': 20, 'quote': content['text'][:20]}])
        request = self.root / 'classification.json'
        payload = {'schema': classification_flow.VERSION, 'reviews': [review]}
        fixtures.write(request, payload)
        code, updated = self.invoke(['update', '--classification-file', str(request)])
        self.assertEqual(code, 0, updated)
        self.assertEqual(updated['effective_count'], 1)
        self.assertEqual(updated['classification']['pending_count'], 0)
        # A normal subsequent update must reuse the same verified classification.
        code, unchanged = self.invoke(['update'])
        self.assertEqual(code, 0, unchanged)
        self.assertEqual(unchanged['effective_count'], 1)
        self.assertEqual(unchanged['classification']['pending_count'], 0)
        query = 'premiums' if relative.endswith('.png') else '年度'
        code, queried = self.invoke(['call', '--task', 'query', '--query', query])
        self.assertEqual(code, 0, queried)
        candidates = [item for item in queried['candidates'] if item['file'] == relative]
        self.assertEqual(len(candidates), 1, queried)
        code, readout = self.invoke(['read-source', '--request-json', json.dumps(candidates[0]['read_request'])])
        self.assertEqual(code, 0, readout)
        self.assertEqual(readout['text'], content['text'])
        # The fix changes the exported identity, never relaxes identity validation.
        review['extraction_version'] = 'text-v1'
        fixtures.write(request, payload)
        code, rejected = self.invoke(['update', '--classification-file', str(request)])
        self.assertEqual(code, 2, rejected)
        self.assertIn('已变化', rejected['message'])

    def test_workbook_classification_roundtrip(self):
        fixtures.xlsx(self.source / 'business.xlsx')
        self.establish()
        self.classify_through_public_commands('business.xlsx', fixtures.XLSX_VERSION)

    def test_image_classification_roundtrip(self):
        path = self.source / 'business.png'
        with Image.new('RGB', (100, 100), 'white') as image:
            image.save(path)
        self.establish()
        code, task = self.invoke(['material', 'image-prepare', '--file', path.name,
            '--vision-capability', 'available', '--image-egress-approved', 'yes',
            '--confirmation', 'Synthetic image observation test specifically authorized'])
        self.assertEqual(code, 0, task)
        request = self.root / 'region.json'
        fixtures.write(request, fixtures.region(task))
        code, saved = self.invoke(['material', 'image-region', '--input', str(request)])
        self.assertEqual(code, 0, saved)
        fixtures.write(request, fixtures.finish(task))
        code, finished = self.invoke(['material', 'image-complete', '--input', str(request)])
        self.assertEqual(code, 0, finished)
        code, updated = self.invoke(['update'])
        self.assertEqual(code, 0, updated)
        self.classify_through_public_commands(path.name, fixtures.IMAGE_VERSION)


if __name__ == '__main__':
    unittest.main()
