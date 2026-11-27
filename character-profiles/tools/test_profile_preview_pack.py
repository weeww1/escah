import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image
import profile_preview_pack as preview


class PreviewPackTests(unittest.TestCase):
    def image(self, color):
        im = Image.new('RGBA', (1280, 720), color)
        out = io.BytesIO(); im.save(out, format='PNG')
        return out.getvalue()

    def pictures(self):
        return {4: ('assets/event_0358/original_first.png', 'first', self.image('pink')),
                5: ('assets/event_0358/original_second.png', 'second', self.image('blue'))}

    def test_round_trip_keeps_original_bytes_dimensions_and_names(self):
        pictures = self.pictures()
        data, meta = preview.pack(358, 6, pictures)
        self.assertNotIn(b'original_first.png', data)
        self.assertNotIn(b'\x89PNG\r\n', data)
        restored, files = preview.unpack(data)
        self.assertEqual(restored, meta)
        for source, ref, content in pictures.values():
            self.assertEqual(files[source], content)
            with Image.open(io.BytesIO(files[source])) as im:
                self.assertEqual(im.size, (1280, 720))
        self.assertLess(len(data) - sum(len(v[2]) for v in pictures.values()), 4096)
        self.assertEqual(preview.pack(358, 6, pictures)[0], data)

    def test_corruption_and_truncation_are_rejected(self):
        data, _ = preview.pack(358, 6, self.pictures())
        for changed in (data[:-1], data[:50] + bytes([data[50] ^ 1]) + data[51:], b'PNG' + data):
            with self.assertRaises(ValueError):
                preview.unpack(changed)

    def test_640_webp_preserves_official_source_name(self):
        pictures = self.pictures()
        content, meta = preview.pack(358, 6, pictures, width=640)
        restored, files = preview.unpack(content)
        self.assertEqual(meta, restored)
        self.assertEqual(meta['rendering']['width'], 640)
        for row in meta['previews'].values():
            self.assertTrue(row['file'].endswith('.png.webp'))
            with Image.open(io.BytesIO(files[row['file']])) as im:
                self.assertEqual(im.format, 'WEBP')
                self.assertEqual(im.size, (640, 360))

    def test_escaping_paths_and_wrong_chapters_are_rejected(self):
        bad = self.pictures(); bad[4] = ('assets/../../private.png', *bad[4][1:])
        with self.assertRaises(ValueError):
            preview.pack(358, 6, bad)
        with self.assertRaises(ValueError):
            preview.pack(358, 8, self.pictures())

    def test_black_frame_skipped_but_dark_detailed_image_retained(self):
        self.assertTrue(preview.is_black(Image.new('RGB', (64, 64), 'black')))
        im = Image.new('RGB', (64, 64), 'black')
        for x in range(8):
            for y in range(64):im.putpixel((x, y), (45, 45, 45))
        self.assertFalse(preview.is_black(im))
        self.assertEqual(preview.refs({'EventImages': [preview.BLACK, 'FIRST', 'FIRST', 'SECOND']}), ['first', 'second'])

    def test_local_build_never_downloads_or_creates_story_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = root / 'manifest_data/resource_manifest.json'; p.parent.mkdir()
            p.write_text(json.dumps({'records': {'bundle_scenarioscript_char_6': {'hash': 'missing'}}}))
            result = preview.build(root)
            self.assertFalse(result['downloaded'])
            self.assertEqual(len(result['pending']), 1)
            self.assertFalse((root / 'DL').exists())
            self.assertFalse((root / 'characters').exists())
            self.assertFalse((root / 'data/story_installed.json').exists())


if __name__ == '__main__':
    unittest.main()
