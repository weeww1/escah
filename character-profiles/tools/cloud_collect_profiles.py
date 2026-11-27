"""CI entry: secret DMM cookies in memory; publish only validated public JSON."""
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

import background_official_client as official
import shared_profile_data as public
from player_paths import data_root

ROOT = data_root()
PUBLIC_FILES = public.PUBLIC_FILES


def state_from_secret(value, protocol):
    if not isinstance(value, str) or not 0 < len(value) <= 256 * 1024:
        raise RuntimeError('未配置 DMM_COOKIES_JSON 私密环境变量，或内容过大')
    try:
        packet = json.loads(value)
        viewer = packet['expectedViewer']
        if not isinstance(viewer, str) or not viewer.isdecimal() or len(viewer) > 39:
            raise ValueError()
        rows = packet['cookies']
        if not isinstance(rows, list) or not 0 < len(rows) < 100:
            raise ValueError()
        cookies = []
        for row in rows:
            if row['domain'].lstrip('.') not in ('dmm.com', 'dmm.co.jp', 'games.dmm.co.jp', 'play.games.dmm.co.jp') or row['name'] not in official.COOKIE_NAMES:
                raise ValueError()
            if not isinstance(row['value'], str) or len(row['value']) > 20000 or any(c in row['value'] for c in '\r\n\0'):
                raise ValueError()
            expires = row.get('expires', -1)
            if not isinstance(expires, (int, float)):
                raise ValueError()
            if expires > 0 and expires <= time.time():
                continue
            cookies.append({key: row[key] for key in ('domain', 'name', 'value', 'path', 'secure', 'expires', 'session') if key in row})
        if not any(row['domain'].endswith('dmm.co.jp') and row['name'] in ('login_session_id', 'login_secure_id') for row in cookies):
            raise ValueError()
        if protocol.get('source') != 'local-original-client':
            raise ValueError()
        for key, length in [('iv', 16), ('salt', 32), ('defaultKey', 32)]:
            if len(base64.b64decode(protocol['crypt'][key], validate=True)) != length:
                raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('DMM 私密 Cookie 或官方协议配置格式不正确，请用扩展刷新') from None
    return {'schemaVersion': 1, 'expectedViewer': viewer, 'cookies': cookies,
            'crypt': protocol['crypt'], 'headers': protocol.get('headers', {}),
            'authTemplate': {'httpMethod': 'GET', 'headers': '', 'postData': '', 'authz': 'signed',
                             'contentType': 'JSON', 'numEntries': '3', 'getSummaries': 'false',
                             'signOwner': 'true', 'signViewer': 'true', 'container': 'dmm',
                             'bypassSpecCache': '', 'getFullHeaders': 'false', 'oauthState': '',
                             'OAUTH_SIGNATURE_PUBLICKEY': 'key_2032'}}


def fetch_roster(proxy=''):
    from update_manifest import CDN, MANIFESTS, decode_manifest
    # The public CDN is reachable directly from Actions. Reserve the private
    # account's proxy route for DMM and game requests, without proxying assets.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    records = {}
    try:
        for name in MANIFESTS:
            request = urllib.request.Request(CDN + name, headers={'User-Agent': 'public-metadata-ci/1.0'})
            with opener.open(request, timeout=45) as response:
                data = response.read(64 * 1024 * 1024 + 1)
            if len(data) > 64 * 1024 * 1024:
                raise ValueError()
            records.update(decode_manifest(data))
    except Exception:
        raise RuntimeError('云端未取得完整官方资源目录，请检查运行地区或 DMM_HTTP_PROXY；旧资料未覆盖') from None
    return {name: value for name, value in records.items() if re.fullmatch(r'(?:bundle_scenarioscript_char_|bundle_chara_c-panel_)\d+', name) or name.startswith('bundle_eventimage_')}


def seed_public_cache(project, scratch):
    directory = project / 'public-data'
    if not directory.exists():
        return
    public.validate_bundle(directory)
    shutil.copytree(directory, scratch / 'public-data', dirs_exist_ok=True)
    character_document, story_document = public.load_public_documents(directory)
    characters = character_document['characters']
    cache = {}
    for key, row in characters.items():
        fields = {field: row[field] for field in ('cv', 'artist', 'bio', 'serif') if isinstance(row.get(field), str)}
        cache[key] = {'id': int(key), 'verified': all(row.get('sources', {}).get(field) == 'official' for field in ('cv', 'artist', 'bio')),
                      'fields': fields}
    public.write(scratch / 'data/official-character-profiles.json', {'characters': cache})
    # Preserve already published titles; this pipeline refreshes character data.
    for kind, rows in story_document['stories'].items():
        folder = {'event': 'events', 'main': 'main', 'other': 'other'}[kind]
        if kind == 'event':
            public.write(scratch / 'stories/events/title-catalog.json', {'events': rows})
        else:
            for key, row in rows.items():
                public.write(scratch / 'stories' / folder / key / 'info.json', row)


def run(project=ROOT, environ=None, client_factory=official.Client, roster_fetch=fetch_roster):
    project = Path(project)
    env = os.environ if environ is None else environ
    state = state_from_secret(env.pop('DMM_COOKIES_JSON', ''), public.read(project / 'data/official-client-protocol.json'))
    proxy = env.get('DMM_HTTP_PROXY', '')
    print(json.dumps({'phase': 'manifest'}), flush=True)
    records = roster_fetch(proxy)
    if not records:
        raise RuntimeError('官方角色资源目录为空，旧资料未覆盖')
    with tempfile.TemporaryDirectory(prefix='public-metadata-ci-') as temporary:
        scratch = Path(temporary)
        seed_public_cache(project, scratch)
        public.write(scratch / 'manifest_data/resource_manifest.json', {'records': records})
        previous_root = official.ROOT
        try:
            official.ROOT = scratch
            client = client_factory(state, proxy=proxy)
            print(json.dumps({'phase': 'login', 'transport': 'http'}), flush=True)
            client.login()
            report = official.collect(client, refresh=env.get('DMM_REFRESH_ALL') == '1', emit=lambda message: print(message, flush=True))
            if report['failed']:
                raise RuntimeError('部分角色资料读取失败，本次未发布；旧资料保留')
            if env.get('PREVIEW_GITEE_TOKEN'):
                from shared_profile_previews import cloud as cloud_previews
                preview_report = cloud_previews(project, records, env['PREVIEW_GITEE_TOKEN'])
                print(json.dumps({'phase': 'profile-previews', **preview_report}), flush=True)
            summary = public.export(scratch)
            public.validate_bundle(scratch / 'public-data')
            # Never copy a temporary cache, cookie file or login state to the repository.
            output = project / 'public-data'
            output.mkdir(parents=True, exist_ok=True)
            for name in PUBLIC_FILES:
                destination = output / name
                temporary_file = destination.with_name(destination.name + '.tmp')
                temporary_file.write_bytes((scratch / 'public-data' / name).read_bytes())
                temporary_file.replace(destination)
            print(json.dumps({'phase': 'ready-to-publish', 'characters': summary['characters']}), flush=True)
            return report
        finally:
            official.ROOT = previous_root


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    try:
        run()
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else '云端采集失败，旧资料未发布；请检查私密凭据和网络'
        # File/function/line only: never print locals, response bodies or URLs.
        location = [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                    for frame in traceback.extract_tb(error.__traceback__)[-3:]]
        print(json.dumps({'error': message, 'errorType': type(error).__name__, 'location': location}, ensure_ascii=False), flush=True)
        sys.exit(1)
