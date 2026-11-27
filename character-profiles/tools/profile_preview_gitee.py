"""Explicit owner-side Gitee preview trial; never invoked on player startup."""
import argparse
import hashlib
import json
import secrets
import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from gitee_private_variables import NoRedirect, saved_access
from profile_preview_pack import ROOT, unpack

API = 'https://gitee.com/api/v5/repos/weeww123321/escah'


class Releases:
    def __init__(self, token, opener=None):
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))

    def request(self, endpoint, data=None, content_type='application/json'):
        if not endpoint.startswith('/') or '..' in endpoint or not __import__('re').fullmatch(r'/[A-Za-z0-9_/?=&.-]+', endpoint):
            raise ValueError('Gitee 附件接口无效')
        request = urllib.request.Request(API + endpoint, data=data, headers={
            'Authorization': 'Bearer ' + self.token, 'User-Agent': 'escah-preview-publisher/1.0',
            'Content-Type': content_type, 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=60) as response:
                body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError('Gitee 响应过大')
            return json.loads(body)
        except urllib.error.HTTPError as error:
            # Never print response bodies or token-bearing requests.
            raise RuntimeError('Gitee 发行版接口 HTTP ' + str(error.code)) from None

    def release(self, tag):
        rows = self.request('/releases')
        old = next((row for row in rows if row['tag_name'] == tag), None)
        if old:
            return old
        body = json.dumps({'access_token': self.token, 'tag_name': tag, 'target_commitish': 'master',
            'name': '角色预览' if tag == 'profile-previews-v1' else '角色预览试传：单角色',
            'body': '资料页首图预览的混淆文件。',
            'prerelease': tag != 'profile-previews-v1'}, ensure_ascii=False).encode()
        return self.request('/releases', body)

    def upload(self, release_id, path):
        boundary = 'esca-preview-' + secrets.token_hex(16)
        content = path.read_bytes()
        if len(content) >= 100 * 1024 * 1024:
            raise ValueError('预览附件超过 Gitee 单文件限额')
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="access_token"\r\n\r\n'.encode()
            + self.token.encode() + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
            + content + f'\r\n--{boundary}--\r\n'.encode())
        return self.request(f'/releases/{int(release_id)}/attach_files', data,
            'multipart/form-data; boundary=' + boundary)

    def assets(self, release_id):
        rows = []
        for page in range(1, 101):
            batch = self.request(f'/releases/{int(release_id)}/attach_files?per_page=100&page={page}')
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise RuntimeError('Gitee 预览附件数量超出限制')


def public_url(asset, name):
    value = asset.get('browser_download_url') or asset.get('download_url')
    if not value and asset.get('id'):
        value = 'https://gitee.com/weeww123321/escah/attach_files/' + str(asset['id']) + '/download/' + urllib.parse.quote(name)
    p = urllib.parse.urlsplit(value or '')
    if p.scheme != 'https' or p.hostname != 'gitee.com' or not p.path.startswith('/weeww123321/escah/') or p.query or p.fragment or p.username or p.password:
        raise ValueError('Gitee 预览地址无效')
    return value


def publish(directory, token, destination):
    from profile_preview_pack import encode, decode
    document = json.loads(decode((directory / 'preview-index.dat').read_bytes()))
    client = Releases(token)
    release = client.release('profile-previews-v1')
    assets = {v.get('name'): v for v in client.assets(release['id'])}
    pending = []
    for row in document['characters'].values():
        if row.get('url'):
            public_url({'browser_download_url': row['url']}, row['asset'])
            continue
        name = row['asset']
        path = directory / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('待发布的预览文件不存在或校验失败')
        if name not in assets:
            pending.append((name, path))
    # Cross-border round trips dominate these small files. Bound concurrency,
    # retain content-addressed reuse, and keep the index private until complete.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(Releases(token).upload, release['id'], path): name for name, path in pending}
        for future in concurrent.futures.as_completed(futures):
            assets[futures[future]] = future.result()
    for row in document['characters'].values():
        if row.get('url'):
            continue
        name = row['asset']
        row['url'] = public_url(assets[name], name)
    content = encode(json.dumps(document, sort_keys=True, separators=(',', ':')).encode())
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + '.tmp'); tmp.write_bytes(content); tmp.replace(destination)
    # Preserve published receipts for subsequent owner-side incremental exports.
    (directory / 'build-state.json').write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'uploaded': len(pending), 'characters': len(document['characters']), 'releaseId': release['id']}


def recover(directory, token, destination, records):
    """Recover published small files after an interrupted owner/cloud batch."""
    from profile_preview_pack import encode, decode
    client = Releases(token); release = client.release('profile-previews-v1')
    document = json.loads(decode(destination.read_bytes())) if destination.is_file() else {'schemaVersion': 1, 'knownCharacters': [], 'characters': {}}
    known = {row['asset'] for row in document['characters'].values()}
    pending = [asset for asset in client.assets(release['id']) if asset.get('name') not in known]
    def read_asset(asset):
        name = asset.get('name', '')
        if not __import__('re').fullmatch(r'[a-f0-9]{64}\.dat', name):
            raise ValueError('Published preview asset has an invalid name')
        url = public_url(asset, name)
        public = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with public.open(url, timeout=40) as response:
            data = response.read(32 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() + '.dat' != name:
            raise ValueError('Published preview asset hash mismatch')
        metadata, files = unpack(data)
        cid = metadata['id']; script = 'bundle_scenarioscript_char_' + str(cid)
        if metadata.get('rendering', {}).get('width') != 640 or script not in records:
            raise ValueError('Published preview asset is not the expected 640px character format')
        bundles = {script: records[script]['hash']}
        for picture in metadata['previews'].values():
            suffix = '_'.join(picture['ref'].lower().split('_')[:2])
            names = [n for n in records if n.startswith('bundle_eventimage_') and n.endswith(suffix)]
            if len(names) != 1:
                raise ValueError('Published preview resource group is ambiguous')
            bundles[names[0]] = records[names[0]]['hash']
        return str(cid), {'asset': name, 'bytes': len(data), 'sha256': name[:-4],
            'metadata': metadata, 'bundles': bundles, 'url': url}, data
    directory.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for cid, row, data in pool.map(read_asset, pending):
            (directory / row['asset']).write_bytes(data)
            document['characters'][cid] = row
    destination.write_bytes(encode(json.dumps(document, sort_keys=True, separators=(',', ':')).encode()))
    (directory / 'build-state.json').write_text(json.dumps(document, indent=2), encoding='utf-8')
    (directory / 'preview-index.dat').write_bytes(destination.read_bytes())
    return {'recovered': len(pending), 'characters': len(document['characters'])}


def trial(directory, tag='profile-preview-trial-20261127'):
    state = json.loads((directory / 'build-state.json').read_text(encoding='utf-8'))
    if len(state['characters']) != 1:
        raise ValueError('试传只允许一个角色，不能批量发布')
    row = next(iter(state['characters'].values()))
    path = directory / row['asset']
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != row['sha256']:
        raise ValueError('本地试传文件校验失败')
    metadata, files = unpack(content)
    access = saved_access()
    client = Releases(access['token'])
    release = client.release(tag)
    # Query before upload so retrying a download test does not duplicate assets.
    assets = client.request(f"/releases/{int(release['id'])}/attach_files")
    asset = next((v for v in assets if v.get('name') == path.name), None)
    if asset is None:
        asset = client.upload(release['id'], path)
    public_url = asset.get('browser_download_url') or asset.get('download_url')
    if not public_url and asset.get('id'):
        public_url = 'https://gitee.com/weeww123321/escah/attach_files/' + str(asset['id']) + '/download/' + urllib.parse.quote(path.name)
    parsed = urllib.parse.urlsplit(public_url or '')
    if parsed.scheme != 'https' or parsed.hostname != 'gitee.com' or not parsed.path.startswith('/weeww123321/escah/'):
        raise RuntimeError('Gitee 未返回可验证的公开下载地址，附件字段：' + ','.join(sorted(asset)))
    # A completely separate public client: no token, account cookie or local proxy.
    public = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(public_url, headers={'User-Agent': 'escah-preview-reader/1.0'})
    try:
        with public.open(request, timeout=60) as response:
            received = response.read(len(content) + 1)
    except urllib.error.HTTPError as error:
        raise RuntimeError('Gitee 公开附件下载 HTTP ' + str(error.code)) from None
    if received != content:
        raise RuntimeError('Gitee 公开下载未取得完整混淆文件')
    restored, original_files = unpack(received)
    if restored != metadata or original_files != files:
        raise RuntimeError('下载后的原图还原校验失败')
    report = {'passed': True, 'character': metadata['id'], 'releaseId': release['id'],
        'tag': tag, 'publicUrl': public_url, 'bytes': len(received), 'images': len(original_files),
        'unauthenticatedDownload': True, 'losslessRoundTrip': True, 'fullCGDownloads': 0}
    (directory / 'gitee-trial-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'outputs/profile-preview-trial')
    args = parser.parse_args()
    try:
        print(json.dumps(trial(args.directory), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'passed': False, 'error': str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__}, ensure_ascii=False))
        raise SystemExit(1)
