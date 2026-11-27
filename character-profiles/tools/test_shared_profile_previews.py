import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import profile_preview_pack as pack
import shared_profile_previews as previews


class SharedPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        out = io.BytesIO(); Image.new('RGB', (1280, 720), 'pink').save(out, format='PNG')
        content, meta = pack.pack(6, 6, {4: ('assets/first.png', 'first', out.getvalue()),
            5: ('assets/second.png', 'second', out.getvalue())}, width=640)
        import hashlib
        digest = hashlib.sha256(content).hexdigest()
        self.content = content
        self.row = {'asset': digest + '.dat', 'sha256': digest, 'bytes': len(content), 'metadata': meta,
            'bundles': {'bundle_scenarioscript_char_6': 'old'},
            'url': 'https://gitee.com/weeww123321/escah/releases/download/profile-previews-v1/' + digest + '.dat'}
        self.index = {'schemaVersion': 1, 'knownCharacters': [6, 7], 'characters': {'6': self.row}}
        self.index_bytes = pack.encode(json.dumps(self.index).encode())
        (self.root / 'shared-data.config.json').write_text(json.dumps({'profilePreviews': True,
            'repository': 'https://gitee.com/weeww123321/escah', 'branch': 'master', 'directory': 'character-profiles/public-data'}))

    def test_manual_refresh_downloads_only_dat_and_reuses_good_files(self):
        calls = []
        def opener(request, **kw):
            self.assertFalse(request.has_header('Authorization'))
            calls.append(request.full_url)
            return io.BytesIO(self.index_bytes if request.full_url.endswith('/preview-index.dat') else self.content)
        result = previews.refresh(self.root, opener=opener)
        self.assertEqual(result['completed'], 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(url.endswith('.dat') for url in calls))
        self.assertFalse((self.root / 'characters').exists())
        self.assertFalse((self.root / 'data/story_installed.json').exists())
        row = previews.metadata(self.root)['6']
        self.assertEqual(set(row['chapterPreviews']), {'4', '5'})
        with Image.open(self.root / row['chapterPreviews']['4']) as image:
            self.assertEqual(image.size, (640, 360))
            self.assertEqual(image.format, 'PNG')
        previews.refresh(self.root, opener=opener)
        self.assertEqual(len(calls), 3, 'Cached preview must not be fetched again')

    def test_corrupt_download_keeps_previous_cache(self):
        previews.install(self.root, self.content, self.row)
        before = (self.root / previews.RECEIPT).read_bytes()
        bad = dict(self.row); bad['sha256'] = 'a' * 64; bad['asset'] = bad['sha256'] + '.dat'; bad['url'] = self.row['url'].rsplit('/', 1)[0] + '/' + bad['asset']
        index = pack.encode(json.dumps({**self.index, 'characters': {'6': bad}}).encode())
        result = previews.refresh(self.root, opener=lambda req, **kw: io.BytesIO(index if req.full_url.endswith('/preview-index.dat') else self.content))
        self.assertEqual(result['failed'], 1)
        self.assertEqual((self.root / previews.RECEIPT).read_bytes(), before)

    def test_index_cannot_send_downloads_to_another_host(self):
        bad = json.loads(json.dumps(self.index)); bad['characters']['6']['url'] = 'https://example.com/' + self.row['asset']
        with self.assertRaises(ValueError):
            previews.read_index(pack.encode(json.dumps(bad).encode()))

    def test_cloud_incremental_skips_old_roles_without_preview(self):
        d = self.root / 'public-data'; d.mkdir(); (d / previews.INDEX).write_bytes(self.index_bytes)
        records = {'bundle_scenarioscript_char_6': {'hash': 'old'}, 'bundle_scenarioscript_char_7': {'hash': 'old'}}
        with patch('fill_stories.BundleStore') as store:
            result = previews.cloud(self.root, records, 'private')
        self.assertEqual(result['newCharacters'], 0)
        store.assert_not_called()

    def test_cloud_new_role_selection_and_missing_baseline_guard(self):
        records = {'bundle_scenarioscript_char_6': {'hash': 'old'}, 'bundle_scenarioscript_char_7': {'hash': 'old'},
            'bundle_scenarioscript_char_8': {'hash': 'new'}}
        with self.assertRaises(RuntimeError), patch('fill_stories.BundleStore') as store:
            previews.cloud(self.root, records, 'private')
        store.assert_not_called()
        d = self.root / 'public-data'; d.mkdir(); (d / previews.INDEX).write_bytes(self.index_bytes)
        def build(*args, **kw):
            self.assertEqual(kw['ids'], {8})
            with self.assertRaises(ValueError):kw['fetch_missing']('bundle_voice_adv_char_8_1', {})
            return {'pending': [], 'completed': [8]}
        with patch('fill_stories.BundleStore'), patch('profile_preview_pack.build', side_effect=build), patch('profile_preview_gitee.publish', return_value={'uploaded': 1}):
            result = previews.cloud(self.root, records, 'private')
        self.assertEqual(result['newCharacters'], 1)


if __name__ == '__main__':
    unittest.main()
