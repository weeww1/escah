"""Manual small-preview updates and incremental owner/cloud generation."""
import hashlib
import io
import json
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import image_updater as images
import shared_profile_data as shared
import profile_preview_pack as pack
import update_manifest as um

INDEX = 'preview-index.dat'
RECEIPT = 'data/profile-previews.json'


def read_index(data):
    doc = json.loads(pack.decode(data))
    if doc.get('schemaVersion') != 1 or not isinstance(doc.get('characters'), dict) or len(doc['characters']) > 10000:
        raise ValueError('角色预览清单格式错误')
    known = doc.get('knownCharacters', [])
    if not isinstance(known, list) or len(known) > 10000 or any(type(v) is not int or not 0 < v < 1000000 for v in known):
        raise ValueError('角色预览基线无效')
    for cid, row in doc['characters'].items():
        if not cid.isdecimal() or not 0 < int(cid) < 1000000 or not isinstance(row, dict):
            raise ValueError('角色预览编号无效')
        digest = row.get('sha256', '')
        if not re.fullmatch(r'[a-f0-9]{64}', digest) or row.get('asset') != digest + '.dat' or type(row.get('bytes')) is not int or not 0 < row['bytes'] < pack.MAX_BYTES:
            raise ValueError('角色预览文件记录无效')
        if row.get('metadata', {}).get('id') != int(cid):
            raise ValueError('角色预览元数据编号不匹配')
        if row.get('url'):
            url = urllib.parse.urlsplit(row['url'])
            if url.scheme != 'https' or url.hostname != 'gitee.com' or not url.path.startswith('/weeww123321/escah/') or not url.path.endswith('/' + row['asset']) or url.query or url.fragment or url.username or url.password:
                raise ValueError('角色预览下载地址无效')
    return doc


def valid(root, row):
    try:
        return len(row.get('chapterPreviews', {})) == 2 and bool(row.get('files')) and all(
            images.local_path(root, n).is_file() and images.local_path(root, n).stat().st_size == value['bytes']
            and hashlib.sha256(images.local_path(root, n).read_bytes()).hexdigest() == value['sha256']
            for n, value in row['files'].items())
    except (ValueError, TypeError, KeyError, OSError):
        return False


def metadata(root):
    return {cid: row for cid, row in images.read_json(root / RECEIPT).get('characters', {}).items() if valid(root, row)}


def install(root, data, expected=None):
    from PIL import Image
    meta, files = pack.unpack(data)
    if expected and (meta != expected['metadata'] or hashlib.sha256(data).hexdigest() != expected['sha256']):
        raise ValueError('角色预览与清单不匹配')
    staged, previews, saved = [], {}, {}
    try:
        for chapter, row in meta['previews'].items():
            content = files[row.get('file', row['source'])]
            with Image.open(io.BytesIO(content)) as image:
                png = io.BytesIO(); image.save(png, format='PNG'); value = png.getvalue()
            relative = 'images/previews/official/' + row['source']
            dest = images.local_path(root, relative)
            dest.parent.mkdir(parents=True, exist_ok=True)
            temporary = dest.with_name(dest.name + '.preview-part'); temporary.write_bytes(value)
            staged.append((temporary, dest))
            previews[chapter] = relative
            saved[relative] = {'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
        for temporary, dest in staged:
            temporary.replace(dest)
        receipt = {'chapters': meta['chapters'], 'chapterPreviews': previews, 'files': saved,
            'sha256': hashlib.sha256(data).hexdigest()}
        state = images.read_json(root / RECEIPT)
        state.setdefault('characters', {})[str(meta['id'])] = receipt
        um.atomic_write_text(root / RECEIPT, json.dumps(state, ensure_ascii=False, indent=2))
        return meta['id']
    finally:
        for temporary, dest in staged:
            temporary.unlink(missing_ok=True)


def refresh(root=shared.ROOT, notify=lambda value: None, opener=None):
    """Only called by the explicit character Update List button. No CG downloads."""
    config = images.read_json(root / 'shared-data.config.json')
    if not config.get('profilePreviews'):
        return {'configured': False, 'completed': 0}
    document = None
    for source in [config, *config.get('fallbacks', [])]:
        transports = [opener or urllib.request.urlopen]
        if source.get('proxy') and opener is None:
            transports.insert(0, urllib.request.build_opener(urllib.request.ProxyHandler({'https': source['proxy']})).open)
        for transport in transports:
            try:
                request = urllib.request.Request(shared.source_url(source, INDEX), headers={'User-Agent': 'escah-profile-previews/1.0'})
                with transport(request, timeout=20) as response:
                    value = response.read(2 * 1024 * 1024 + 1)
                if len(value) > 2 * 1024 * 1024:
                    raise ValueError('角色预览清单过大')
                document = read_index(value)
                break
            except (OSError, ValueError, TypeError, KeyError):
                pass
        if document is not None:
            break
    if document is None:
        raise RuntimeError('角色预览清单读取失败，保留现有预览')
    existing = images.read_json(root / RECEIPT).get('characters', {})
    pending = [(cid, row) for cid, row in document['characters'].items() if row.get('url') and
        (existing.get(cid, {}).get('sha256') != row['sha256'] or not valid(root, existing.get(cid, {})))]
    failures, done = [], []
    # Downloads are public and never carry the owner's Gitee authorization.
    download = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    for i, (cid, row) in enumerate(pending):
        notify({'phase': 'preparing', 'message': '正在更新角色预览', 'progressDone': i, 'progressTotal': len(pending)})
        try:
            request = urllib.request.Request(row['url'], headers={'User-Agent': 'escah-profile-previews/1.0'})
            with download(request, timeout=30) as response:
                data = response.read(row['bytes'] + 1)
            if len(data) != row['bytes']:
                raise ValueError('角色预览文件长度错误')
            done.append(install(root, data, row))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            failures.append({'id': int(cid), 'error': type(exc).__name__})
    notify({'phase': 'preparing', 'progressDone': len(pending), 'progressTotal': len(pending)})
    return {'configured': True, 'completed': len(done), 'changedIds': done, 'failed': len(failures), 'failures': failures}


def cloud(project, records, token, backfill=False, limit=0):
    """Only new/changed characters; baseline characters without CGs stay pending."""
    index = project / 'public-data' / INDEX
    if not index.is_file():
        raise RuntimeError('缺少预览基线，停止云端补图，避免误下载全部旧角色CG')
    previous = read_index(index.read_bytes())
    roster = {int(m[1]) for n in records if (m := re.fullmatch(r'bundle_scenarioscript_char_(\d+)', n))}
    wanted = roster - set(previous['knownCharacters'])
    if backfill:
        wanted |= roster - {int(cid) for cid in previous['characters']}
    for cid, row in previous['characters'].items():
        if any(records.get(n, {}).get('hash') != h for n, h in row.get('bundles', {}).items()):
            wanted.add(int(cid))
    if not wanted:
        return {'completed': 0, 'reused': len(previous['characters']), 'newCharacters': 0}
    if limit:
        wanted = set(sorted(wanted)[:limit])
    from profile_preview_gitee import publish
    import fill_stories as filler
    with tempfile.TemporaryDirectory(prefix='profile-preview-ci-') as temporary:
        scratch = Path(temporary)
        shared.write(scratch / 'manifest_data/resource_manifest.json', {'records': records})
        output = scratch / 'public-previews'; output.mkdir()
        shared.write(output / 'build-state.json', previous)
        old_root, old_receipts = filler.ROOT, filler.BUNDLE_RECEIPTS
        try:
            filler.ROOT, filler.BUNDLE_RECEIPTS = scratch, scratch / 'data/story_bundle_cache.json'
            store = filler.BundleStore(records)
            def fetch(name, record):
                if not re.fullmatch(r'bundle_(?:scenarioscript_char_\d+|eventimage_[a-z0-9_]+)', name):
                    raise ValueError('预览生成禁止下载其他类型资源')
                store.ensure([name])
                return store.have[name]
            report = pack.build(scratch, ids=wanted, output=output, fetch_missing=fetch, width=640)
            if report['pending'] and not backfill:
                raise RuntimeError('新增角色预览生成未完成，本次保留旧清单供补跑重试')
            published = publish(output, token, index)
            result = {'completed': len(report['completed']), 'newCharacters': len(wanted),
                'failed': len(report['pending']), 'pending': report['pending'], **published}
            shared.write(project / 'data/profile-preview-cloud-report.json', result)
            return result
        finally:
            filler.ROOT, filler.BUNDLE_RECEIPTS = old_root, old_receipts


if __name__ == '__main__':
    import argparse
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cloud-backfill', action='store_true')
    parser.add_argument('--verify-complete', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()
    if args.verify_complete:
        result = shared.read(shared.ROOT / 'data/profile-preview-cloud-report.json')
        if result.get('failed'):
            print(json.dumps({'incomplete': result['failed'], 'pending': result['pending']}, ensure_ascii=False))
            raise SystemExit(1)
        raise SystemExit(0)
    if not args.cloud_backfill or not 0 <= args.limit <= 1000:
        parser.error('必须显式选择云端补齐，并使用有效批量数')
    from cloud_collect_profiles import fetch_roster
    token = os.environ.get('PREVIEW_GITEE_TOKEN')
    if not token:
        raise SystemExit('未配置预览附件发布凭据')
    result = cloud(shared.ROOT, fetch_roster(), token, backfill=True, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False))
