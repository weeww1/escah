"""Build obfuscated first-CG previews from official local caches.

Shared previews use 640px WebP; the optional original mode preserves PNG pixels.
The shared format is obfuscation, not a secret: players have the decoding key.
"""
import argparse
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

from player_paths import data_root
import image_updater as images
import update_manifest as um

ROOT = data_root()
HEADER = b'ESCA-PREVIEW-1\n'
KEY = hashlib.sha256(b'escah-public-profile-previews-v1').digest()
MAX_BYTES = 32 * 1024 * 1024
BLACK = '83438378_8d9589e696ca'


def encode(payload):
    from Crypto.Cipher import AES
    if len(payload) > MAX_BYTES:
        raise ValueError('预览文件超过大小限制')
    nonce = hashlib.sha256(b'preview-nonce\0' + payload).digest()[:12]
    cipher = AES.new(KEY, AES.MODE_GCM, nonce=nonce)
    cipher.update(HEADER)
    content, tag = cipher.encrypt_and_digest(payload)
    return HEADER + nonce + tag + content


def decode(data):
    from Crypto.Cipher import AES
    n = len(HEADER)
    if not data.startswith(HEADER) or not n + 28 <= len(data) <= MAX_BYTES + n + 28:
        raise ValueError('预览混淆文件不完整或过大')
    cipher = AES.new(KEY, AES.MODE_GCM, nonce=data[n:n + 12])
    cipher.update(HEADER)
    try:
        return cipher.decrypt_and_verify(data[n + 28:], data[n + 12:n + 28])
    except ValueError:
        raise ValueError('预览混淆文件校验失败') from None


def official_path(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise ValueError('预览原图路径无效')
    p = PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or not value.startswith('assets/') or p.suffix.lower() != '.png':
        raise ValueError('预览原图路径无效')
    return value


def pack(cid, chapters, pictures, width=None, quality=82):
    from PIL import Image
    metadata = {'schemaVersion': 1, 'id': cid, 'chapters': chapters, 'previews': {}}
    metadata['rendering'] = {'width': width, 'quality': quality if width else None}
    files = {}
    for chapter, (source, ref, data) in pictures.items():
        source = official_path(source)
        stored = source
        if width:
            if width not in (400, 640):
                raise ValueError('预览宽度无效')
            with Image.open(io.BytesIO(data)) as original:
                image = original.convert('RGB'); image.thumbnail((width, width), Image.Resampling.LANCZOS)
                out = io.BytesIO(); image.save(out, format='WEBP', quality=quality, method=6)
                data = out.getvalue()
            stored += '.webp'
        if stored in files and files[stored] != data:
            raise ValueError('相同官方路径对应不同图片')
        files[stored] = data
        metadata['previews'][str(chapter)] = {'source': source, 'ref': ref,
            'file': stored, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, data in [('preview.json', json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode()), *sorted(files.items())]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, data, compress_type=zipfile.ZIP_STORED)
    result = encode(memory.getvalue())
    unpack(result)
    return result, metadata


def unpack(data):
    from PIL import Image
    payload = decode(data)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        if not 2 <= len(entries) <= 3 or len({p.filename for p in entries}) != len(entries):
            raise ValueError('预览文件数量无效')
        if any(p.compress_type != zipfile.ZIP_STORED or p.file_size > 16 * 1024 * 1024 for p in entries):
            raise ValueError('预览文件大小或编码无效')
        meta = json.loads(archive.read('preview.json'))
        if meta.get('schemaVersion') != 1 or type(meta.get('id')) is not int or not 0 < meta['id'] < 1000000:
            raise ValueError('预览角色编号无效')
        count, previews = meta.get('chapters'), meta.get('previews')
        if type(count) is not int or not 2 <= count <= 100 or not isinstance(previews, dict) or set(previews) != {str(count - 2), str(count - 1)}:
            raise ValueError('预览章节无效')
        files = {}
        for row in previews.values():
            source = official_path(row['source'])
            stored = row.get('file', source)
            if stored not in (source, source + '.webp'):
                raise ValueError('预览图片存放路径无效')
            content = archive.read(stored)
            if len(content) != row['bytes'] or hashlib.sha256(content).hexdigest() != row['sha256']:
                raise ValueError('预览原图校验失败')
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in ('PNG', 'WEBP') or image.width * image.height > 4096 * 4096:
                    raise ValueError('预览图片格式或尺寸无效')
                image.verify()
            files[stored] = content
        if set(archive.namelist()) != {'preview.json', *files}:
            raise ValueError('预览文件含有多余资源')
    return meta, files


def refs(scenario):
    values = scenario.get('EventImages') or [row.get('EventImage') for row in scenario.get('Methods', [])]
    return list(dict.fromkeys(v.lower() for v in values if isinstance(v, str) and v and v.lower() != BLACK))


def is_black(image):
    from PIL import ImageStat
    if image.mode == 'RGBA' and image.getchannel('A').getextrema()[1] == 0:
        return True
    probe = image.convert('L'); probe.thumbnail((64, 64))
    stats = ImageStat.Stat(probe)
    return stats.mean[0] < 8 and stats.stddev[0] < 3


def first_picture(scenario, records, cached):
    import UnityPy
    for ref in refs(scenario):
        suffix = '_'.join(ref.split('_')[:2])
        names = [n for n in records if n.startswith('bundle_eventimage_') and n.endswith(suffix)]
        if len(names) != 1:
            raise ValueError('首图对应的官方包不能唯一识别')
        name = names[0]
        bundle = cached(name)
        env = UnityPy.load(str(bundle))
        candidates = {source: ptr for source, ptr in env.container.items()
            if ptr.type.name == 'Texture2D' and PurePosixPath(source).stem.lower().endswith(ref)}
        if len(candidates) != 1:
            raise ValueError('官方首图不存在或不唯一')
        source, pointer = next(iter(candidates.items()))
        image = pointer.read().image
        if image is None:
            raise ValueError('官方首图解包失败')
        if is_black(image):
            continue
        target = io.BytesIO()
        # PNG is lossless; dimensions and decoded RGBA pixels are unchanged.
        image.save(target, format='PNG')
        return (source, ref, target.getvalue()), name
    raise ValueError('剧情没有有效的非黑屏首图')


def build(root=ROOT, ids=None, output=None, fetch_missing=None, notify=lambda value: None, width=640):
    """Default is cache-only. A cloud caller may provide its existing downloader."""
    from extract_scenario import read_scenarios
    from fill_stories import validate_bundle
    root = Path(root)
    output = Path(output) if output else root / 'public-previews'
    records = images.read_json(root / 'manifest_data/resource_manifest.json').get('records', {})
    state = images.read_json(output / 'build-state.json')
    rows = state.setdefault('characters', {})
    have, ready = {}, {}
    for folder in (root.parent / 'DL', root / 'DL'):
        if folder.exists():
            have.update({p.name: p for p in folder.rglob('bundle_*') if p.is_file()})

    def cached(name):
        if name in ready:
            return ready[name]
        path = have.get(name)
        if path:
            try:
                validate_bundle(path, records[name])
                ready[name] = path
                return path
            except Exception:
                pass
        if fetch_missing is None:
            raise FileNotFoundError(name)
        path = fetch_missing(name, records[name])
        validate_bundle(path, records[name])
        ready[name] = path
        return path

    targets = sorted((int(m[1]), n) for n in records
        if (m := re.fullmatch(r'bundle_scenarioscript_char_(\d+)', n)) and (ids is None or int(m[1]) in ids))
    completed, reused, pending = [], [], []
    output.mkdir(parents=True, exist_ok=True)
    for index, (cid, script) in enumerate(targets, 1):
        try:
            old = rows.get(str(cid), {})
            path = output / old.get('asset', 'missing')
            matching = old.get('metadata', {}).get('rendering', {}).get('width') == width
            local = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == old.get('sha256')
            if matching and old.get('bundles') and all(records.get(n, {}).get('hash') == h for n, h in old['bundles'].items()) and (local or fetch_missing is not None and old.get('url')):
                reused.append(cid)
                continue
            scenarios = read_scenarios(cached(script))
            if len(scenarios) < 2:
                raise ValueError('角色剧情不足两章')
            pictures, bundles = {}, {script: records[script]['hash']}
            for chapter in range(len(scenarios) - 2, len(scenarios)):
                picture, name = first_picture(scenarios[chapter], records, cached)
                pictures[chapter] = picture
                bundles[name] = records[name]['hash']
            content, metadata = pack(cid, len(scenarios), pictures, width=width)
            digest = hashlib.sha256(content).hexdigest()
            asset = digest + '.dat'
            tmp = output / (asset + '.tmp'); tmp.write_bytes(content); tmp.replace(output / asset)
            rows[str(cid)] = {'asset': asset, 'bytes': len(content), 'sha256': digest,
                'bundles': bundles, 'metadata': metadata}
            um.atomic_write_text(output / 'build-state.json', json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2))
            completed.append(cid)
        except Exception as exc:
            pending.append({'id': cid, 'reason': '缓存中缺少所需资源' if isinstance(exc, FileNotFoundError) else str(exc)})
        finally:
            notify({'done': index, 'total': len(targets), 'completed': len(completed), 'pending': len(pending)})
    # Public index is also obfuscated; the plaintext state stays owner-side.
    known = set(state.get('knownCharacters', []))
    known.update(int(m[1]) for n in records if (m := re.fullmatch(r'bundle_scenarioscript_char_(\d+)', n))) if fetch_missing is None and ids is None else known.update(completed)
    state['knownCharacters'] = sorted(known)
    um.atomic_write_text(output / 'build-state.json', json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2))
    public = {'schemaVersion': 1, 'knownCharacters': sorted(known), 'characters': rows}
    data = encode(json.dumps(public, sort_keys=True, separators=(',', ':')).encode())
    tmp = output / 'preview-index.dat.tmp'; tmp.write_bytes(data); tmp.replace(output / 'preview-index.dat')
    report = {'completed': completed, 'reused': reused, 'pending': pending, 'charactersAvailable': len(rows),
        'bytes': sum(row['bytes'] for row in rows.values()), 'downloaded': fetch_missing is not None}
    um.atomic_write_text(output / 'build-report.json', json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ids', help='Comma separated character IDs; default: all cached characters')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--width', type=int, choices=(0, 400, 640), default=640)
    args = parser.parse_args()
    sys_ids = {int(v) for v in args.ids.split(',')} if args.ids else None
    result = build(ids=sys_ids, output=args.output, width=args.width or None)
    print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in result.items()}, ensure_ascii=False))
