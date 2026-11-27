import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import profile_preview_gitee as publisher
from profile_preview_pack import encode, decode


class PublisherTests(unittest.TestCase):
    def test_uploads_only_missing_assets_and_publishes_after_all_succeed(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); rows = {}
            for cid in (1, 2, 3):
                content = bytes([cid]) * 100
                digest = hashlib.sha256(content).hexdigest(); name = digest + '.dat'
                (root / name).write_bytes(content)
                rows[str(cid)] = {'asset': name, 'sha256': digest}
            rows['1']['url'] = 'https://gitee.com/weeww123321/escah/releases/download/profile-previews-v1/' + rows['1']['asset']
            (root / 'preview-index.dat').write_bytes(encode(json.dumps({'characters': rows}).encode()))
            calls = []
            def upload(_self, rid, path):
                calls.append(path.name)
                return {'name': path.name, 'browser_download_url': 'https://gitee.com/weeww123321/escah/releases/download/profile-previews-v1/' + path.name}
            with patch.object(publisher.Releases, 'release', return_value={'id': 1}), patch.object(publisher.Releases, 'assets', return_value=[]), patch.object(publisher.Releases, 'upload', upload):
                result = publisher.publish(root, 'test-private', root / 'result.dat')
            self.assertEqual(result['uploaded'], 2)
            self.assertEqual(set(calls), {rows['2']['asset'], rows['3']['asset']})
            doc = json.loads(decode((root / 'result.dat').read_bytes()))
            self.assertTrue(all(row.get('url') for row in doc['characters'].values()))

    def test_failed_upload_does_not_replace_public_index(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); content = b'test'; digest = hashlib.sha256(content).hexdigest(); name = digest + '.dat'
            (root / name).write_bytes(content)
            (root / 'preview-index.dat').write_bytes(encode(json.dumps({'characters': {'1': {'asset': name, 'sha256': digest}}}).encode()))
            target = root / 'result.dat'; target.write_bytes(b'previous-valid-index')
            with patch.object(publisher.Releases, 'release', return_value={'id': 1}), patch.object(publisher.Releases, 'assets', return_value=[]), patch.object(publisher.Releases, 'upload', side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):publisher.publish(root, 'test-private', target)
            self.assertEqual(target.read_bytes(), b'previous-valid-index')


if __name__ == '__main__':
    unittest.main()
