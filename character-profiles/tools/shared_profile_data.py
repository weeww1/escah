"""Build and download public metadata bundles. Never export login/account data."""
import base64
import datetime
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from player_paths import data_root

ROOT = data_root()
CONFIG = ROOT / 'shared-data.config.json'
PACK_NAME = 'metadata.dat'
PACK_HEADER = b'ESCA-META-1\n'
PUBLIC_FILES = ('characters.json', 'stories.json', 'manifest.json')
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PACK_JSON_BYTES = 32 * 1024 * 1024
FIELDS = ('name', 'sub', 'realName', 'faction', 'cv', 'artist', 'bio', 'serif')
SECRET = re.compile(r'(?i)(?:PHPSESSID\s*=|Authorization\s*:|Bearer\s+[A-Za-z0-9_-]{20}|-----BEGIN[^\n]*PRIVATE|(?:access_token|session_id)\s*[:=])')


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {} if default is None else default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    # Hashes must survive Git checkout on both Windows and Linux.
    with temporary.open('w', encoding='utf-8', newline='\n') as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def text(value, limit=15000):
    if not isinstance(value, str):
        return ''
    value = value.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\\r', '\n').strip()
    if len(value) > limit or SECRET.search(value):
        raise ValueError('资料中存在超长内容或疑似登录凭据，已停止导出')
    return value


def validate_characters(document):
    if set(document) - {'schemaVersion', 'generatedAt', 'characters'}:
        raise ValueError('资料包包含非公开顶层字段')
    if document.get('schemaVersion') != 1 or not isinstance(document.get('characters'), dict):
        raise ValueError('资料包格式不正确')
    if not 0 < len(document['characters']) < 10000:
        raise ValueError('资料包角色数量不正确')
    for key, row in document['characters'].items():
        if not key.isdecimal() or not 0 < int(key) < 1000000 or not isinstance(row, dict):
            raise ValueError('资料包角色编号不正确')
        if set(row) - {'id', 'sources', *FIELDS} or row.get('id') != int(key):
            raise ValueError('资料包含非公开字段或编号不匹配')
        if not isinstance(row.get('sources', {}), dict) or set(row.get('sources', {})) - set(FIELDS):
            raise ValueError('资料来源字段不正确')
        if any(value not in ('official', 'wiki', 'local') for value in row.get('sources', {}).values()):
            raise ValueError('资料来源不正确')
        for field in FIELDS:
            if field in row:
                if not isinstance(row[field], str):
                    raise ValueError('角色资料字段必须是文字')
                text(row[field], 15000 if field in ('bio', 'serif') else 500)
    return document


def merge_profile_info(info, shared):
    """Refresh automatically sourced fields while preserving manual changes."""
    result = dict(info)
    sources = info.get('profileSources', {})
    for field in FIELDS:
        value = shared.get(field)
        if value and (not info.get(field) or sources.get(field) in ('wiki', 'wiki-cache', 'official', 'shared')):
            result[field] = value
    return result


def build(root=ROOT):
    root = Path(root)
    basics = read(root / 'data/official-character-basics.json')
    basics = basics.get('characters', {}) if basics.get('verified') is True else {}
    profiles = read(root / 'data/official-character-profiles.json').get('characters', {})
    wiki = read(root / 'glossary/wiki-profiles.json')
    records = read(root / 'manifest_data/resource_manifest.json').get('records', {})
    resource_ids = {str(int(match[1])) for name in records if (match := re.fullmatch(r'(?:bundle_scenarioscript_char_|bundle_chara_c-panel_)(\d+)', name))}
    characters = {}
    for key in sorted(set(basics) | set(profiles), key=int):
        if resource_ids and key not in resource_ids:
            continue
        row = {'id': int(key), 'sources': {}}

        def take(values, source):
            for field in FIELDS:
                value = text(values.get(field), 15000 if field in ('bio', 'serif') else 500)
                if value:
                    row[field] = value
                    row['sources'][field] = source

        local = read(root / 'characters' / key / 'info.json')
        page = basics.get(key, {}).get('sub') or local.get('sub') or local.get('name')
        take(wiki.get(page, {}), 'wiki')
        take(local, 'local')
        take(basics.get(key, {}), 'official')
        if profiles.get(key, {}).get('verified') is True:
            take(profiles[key].get('fields', {}), 'official')
        if row.get('name') or row.get('sub'):
            characters[key] = row
    stories = {}
    for kind, folder in [('event', 'events'), ('main', 'main'), ('other', 'other')]:
        parent = root / 'stories' / folder
        groups = {path.parent.name: read(path) for path in parent.glob('*/info.json') if path.parent.name.isdecimal()}
        captured = read(parent / 'title-catalog.json')
        if kind == 'event':
            for key, value in captured.get('events', {}).items():
                groups[key] = {**groups.get(key, {}), **value}
        elif kind == 'main':
            groups.update(main_title_groups(captured))
        # Other/GetEtc chapter IDs are not scenario bundle IDs. Only publish
        # titles already associated with an actual local story folder.
        output = {}
        for key, value in groups.items():
            if not str(key).isdecimal() or not isinstance(value, dict):
                continue
            row = {'id': int(key), 'title': text(value.get('title'), 500)}
            if isinstance(value.get('chapterTitles'), list):
                row['chapterTitles'] = [text(title, 500) for title in value['chapterTitles']]
            if isinstance(value.get('episodeIds'), list):
                row['episodeIds'] = [int(i) for i in value['episodeIds'] if isinstance(i, int) and i > 0]
            output[str(key)] = row
        stories[kind] = output
    return characters, stories


def main_title_groups(catalog):
    """Use the same bundle slots as main-story-titles.js, not the part IDs."""
    result, prologue, slot = {}, [], 2
    for part in sorted(catalog.get('parts', {}).values(), key=lambda row: int(row['id'])):
        base = (int(part['id']) - 1) * 100
        chapters = {int(row['id']): row for row in part.get('chapters', [])}
        captured_ids = [key for key in chapters if base <= key < base + 100]
        ids = part.get('chapterIds', list(range(base, max(captured_ids) + 1)) if captured_ids else [])
        for chapter_id in sorted(set(map(int, ids))):
            if not base <= chapter_id < base + 100:
                continue
            chapter = chapters.get(chapter_id, {})
            count = int(part.get('chapterCounts', {}).get(str(chapter_id)) or chapter.get('totalCount') or len(chapter.get('episodes', [])))
            titles = [''] * count
            for episode in chapter.get('episodes', []):
                number = int(episode.get('no') or int(episode['id']) % 100)
                if 0 < number <= 100:
                    titles.extend([''] * max(0, number - len(titles)))
                    titles[number - 1] = text(episode.get('title'), 500)
            if chapter_id == base:
                prologue.extend(titles)
            else:
                result[str(slot)] = {'title': ' '.join(filter(None, [part.get('title'), chapter.get('title')])), 'chapterTitles': titles}
                slot += 1
    if prologue:
        result['1'] = {'title': 'プロローグ', 'chapterTitles': prologue}
    return result


def export(root=ROOT):
    root = Path(root)
    characters, stories = build(root)
    output = root / 'public-data'
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for name, key, value in [('characters.json', 'characters', characters), ('stories.json', 'stories', stories)]:
        previous = read(output / name)
        document = {'schemaVersion': 1, 'generatedAt': previous.get('generatedAt', now) if previous.get(key) == value else now, key: value}
        if key == 'characters':
            validate_characters(document)
        else:
            validate_stories(document)
        write(output / name, document)
    files = {}
    for name in ('characters.json', 'stories.json'):
        data = (output / name).read_bytes()
        files[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    previous = read(output / 'manifest.json')
    write(output / 'manifest.json', {'schemaVersion': 1, 'generatedAt': previous.get('generatedAt', now) if previous.get('files') == files else now, 'files': files})
    write_packed_bundle(output)
    (output / 'README.md').write_text('Public game metadata only. Contains character profiles and story titles.\nNo accounts, login tokens, cookies, keys, ownership or unlock states.\n', encoding='utf-8')
    return {'characters': len(characters), 'stories': {kind: len(rows) for kind, rows in stories.items()}, 'directory': str(output)}


def source_url(config, filename):
    repository = config.get('repository', '').strip()
    provider = config.get('provider', 'github')
    if repository.startswith('https://gitee.com/'):
        provider = 'gitee'
        repository = urllib.parse.urlparse(repository).path.strip('/').removesuffix('.git')
    elif repository.startswith('https://github.com/'):
        provider = 'github'
        repository = urllib.parse.urlparse(repository).path.strip('/').removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('资料仓库请填写owner/repo或支持的仓库主页链接')
    branch = config.get('branch', 'main')
    directory = config.get('directory', 'metadata').strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', branch) or '..' in branch.split('/') or not re.fullmatch(r'[A-Za-z0-9_/-]+', directory) or '..' in directory.split('/'):
        raise ValueError('资料分支或目录配置不正确')
    if provider == 'gitee':
        return 'https://gitee.com/' + repository + '/raw/' + urllib.parse.quote(branch, safe='/') + '/' + directory + '/' + filename
    if provider != 'github':
        raise ValueError('不支持的资料仓库平台')
    return 'https://raw.githubusercontent.com/' + repository + '/' + urllib.parse.quote(branch, safe='/') + '/' + directory + '/' + filename


def validate_stories(document):
    if set(document) - {'schemaVersion', 'generatedAt', 'stories'} or document.get('schemaVersion') != 1:
        raise ValueError('剧情标题资料包格式不正确')
    if not isinstance(document.get('stories'), dict) or set(document['stories']) - {'event', 'main', 'other'}:
        raise ValueError('剧情标题分类不正确')
    for rows in document['stories'].values():
        if not isinstance(rows, dict):
            raise ValueError('剧情标题列表不正确')
        for key, row in rows.items():
            if not key.isdecimal() or not isinstance(row, dict) or set(row) - {'id', 'title', 'chapterTitles', 'episodeIds'}:
                raise ValueError('剧情标题包含非公开字段')
            if row.get('id') != int(key) or not isinstance(row.get('title'), str):
                raise ValueError('剧情标题编号或文字格式不正确')
            text(row['title'], 500)
            if not isinstance(row.get('chapterTitles', []), list) or not isinstance(row.get('episodeIds', []), list):
                raise ValueError('剧情章节标题格式不正确')
            for title in row.get('chapterTitles', []):
                if not isinstance(title, str):
                    raise ValueError('章节标题必须为文字')
                text(title, 500)
            if any(type(value) is not int or value <= 0 for value in row.get('episodeIds', [])):
                raise ValueError('剧情章节编号不正确')
    return document


def validate_public_files(files):
    if set(files) != set(PUBLIC_FILES):
        raise ValueError('资料包文件列表不正确')
    for name, data in files.items():
        if len(data) > (65536 if name == 'manifest.json' else MAX_FILE_BYTES):
            raise ValueError('公开资料包超出大小限制')
    manifest = json.loads(files['manifest.json'])
    if not isinstance(manifest, dict) or manifest.get('schemaVersion') != 1 or set(manifest.get('files', {})) != {'characters.json', 'stories.json'}:
        raise ValueError('资料清单格式不正确')
    documents = []
    for name, validate in [('characters.json', validate_characters), ('stories.json', validate_stories)]:
        data = files[name]
        record = manifest['files'][name]
        if not isinstance(record, dict) or len(data) != record.get('bytes') or hashlib.sha256(data).hexdigest() != record.get('sha256'):
            raise ValueError('资料包校验失败：' + name)
        document = json.loads(data)
        if not isinstance(document, dict):
            raise ValueError('资料内容格式不正确')
        documents.append(validate(document))
    return tuple(documents)


def write_packed_bundle(directory):
    """Deterministic compression/encoding for transport; this is not encryption."""
    directory = Path(directory)
    files = {name: (directory / name).read_bytes() for name in PUBLIC_FILES}
    validate_public_files(files)
    packet = json.dumps({'schemaVersion': 1, 'files': {name: data.decode('utf-8') for name, data in files.items()}},
                        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(packet) > MAX_PACK_JSON_BYTES:
        raise ValueError('公开资料包超出大小限制')
    data = PACK_HEADER + base64.b64encode(zlib.compress(packet, 9)) + b'\n'
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('公开资料包超出大小限制')
    destination = directory / PACK_NAME
    temporary = destination.with_name(destination.name + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(destination)
    return destination


def unpack_public_files(data):
    if len(data) > MAX_FILE_BYTES or not data.startswith(PACK_HEADER):
        raise ValueError('资料混淆包版本或大小不正确')
    try:
        compressed = base64.b64decode(data[len(PACK_HEADER):].removesuffix(b'\n'), validate=True)
        decoder = zlib.decompressobj()
        packet = decoder.decompress(compressed, MAX_PACK_JSON_BYTES + 1)
        if len(packet) > MAX_PACK_JSON_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('资料混淆包不完整或超出大小限制')
        document = json.loads(packet)
        if not isinstance(document, dict) or set(document) != {'schemaVersion', 'files'} or document['schemaVersion'] != 1:
            raise ValueError('资料混淆包格式不正确')
        files = document['files']
        if not isinstance(files, dict) or set(files) != set(PUBLIC_FILES) or any(not isinstance(value, str) for value in files.values()):
            raise ValueError('资料混淆包文件列表不正确')
        files = {name: value.encode('utf-8') for name, value in files.items()}
        validate_public_files(files)
        return files
    except (ValueError, TypeError, zlib.error, UnicodeError) as error:
        raise ValueError('资料混淆包还原或校验失败，原有缓存已保留') from error


def validate_bundle(directory, require_packed=False):
    directory = Path(directory)
    files = {name: (directory / name).read_bytes() for name in PUBLIC_FILES}
    validate_public_files(files)
    packed = directory / PACK_NAME
    if require_packed or packed.exists():
        if unpack_public_files(packed.read_bytes()) != files:
            raise ValueError('资料混淆包与公开 JSON 不一致')
    return True


def refresh(root=ROOT, opener=None, on_source=None):
    root = Path(root)
    config = read(root / 'shared-data.config.json')
    if not config.get('repository'):
        return {'configured': False, 'updated': False}

    fallbacks = config.get('fallbacks', [])
    if not isinstance(fallbacks, list) or len(fallbacks) > 8 or any(not isinstance(row, dict) for row in fallbacks):
        raise ValueError('备用资料源配置不正确')
    sources = [config, *fallbacks]
    # Validate every destination before any network request.
    for source in sources:
        source_url(source, 'manifest.json')
        if source.get('format', 'json') not in ('json', 'packed-v1'):
            raise ValueError('资料下载格式不支持')
        proxy = source.get('proxy', '')
        if proxy:
            parsed = urllib.parse.urlsplit(proxy)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ValueError('资料下载代理配置不正确')

    def load_source(source, open_public):
        def download(name, limit):
            request = urllib.request.Request(source_url(source, name), headers={'User-Agent': 'public-game-metadata-updater/1.0', 'Accept': 'application/octet-stream' if name == PACK_NAME else 'application/json'})
            with open_public(request, timeout=20) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError('公开资料包超出大小限制')
            return data

        if source.get('format') == 'packed-v1':
            return validate_public_files(unpack_public_files(download(PACK_NAME, MAX_FILE_BYTES)))

        manifest = json.loads(download('manifest.json', 65536))
        if not isinstance(manifest, dict) or manifest.get('schemaVersion') != 1 or set(manifest.get('files', {})) != {'characters.json', 'stories.json'}:
            raise ValueError('公开资料清单版本不支持')
        documents = {}
        for name, validate in [('characters.json', validate_characters), ('stories.json', validate_stories)]:
            data = download(name, 8 * 1024 * 1024)
            expected = manifest['files'][name]
            if not isinstance(expected, dict) or len(data) != expected.get('bytes') or hashlib.sha256(data).hexdigest() != expected.get('sha256'):
                raise ValueError('公开资料校验失败，原有缓存已保留：' + name)
            documents[name] = validate(json.loads(data))
        return documents['characters.json'], documents['stories.json']

    failures, last_error = [], None
    for index, source in enumerate(sources):
        provider = 'gitee' if urllib.parse.urlsplit(source_url(source, 'manifest.json')).hostname == 'gitee.com' else 'github'
        if on_source:
            on_source(provider, index > 0)
        transports = [opener or urllib.request.urlopen]
        # Proxy settings are optional and local to a source. A machine without
        # that proxy can still try its ordinary network connection.
        if source.get('proxy') and opener is None:
            transports.insert(0, urllib.request.build_opener(urllib.request.ProxyHandler({'https': source['proxy']})).open)
        for open_public in transports:
            try:
                document, stories = load_source(source, open_public)
            except (OSError, ValueError, TypeError, KeyError) as error:
                last_error = error
                continue
            # Both complete documents must pass validation from the same source.
            write(root / 'data/shared-character-profiles.json', document)
            write(root / 'data/shared-story-titles.json', stories)
            return {'configured': True, 'updated': True, 'characters': len(document['characters']),
                    'provider': provider, 'usedFallback': index > 0}
        reason = 'HTTP ' + str(last_error.code) if isinstance(last_error, urllib.error.HTTPError) else type(last_error).__name__
        failures.append(('Gitee' if provider == 'gitee' else 'GitHub') + ': ' + reason)
    if len(sources) == 1:
        raise last_error
    raise RuntimeError('所有资料源更新失败，原有缓存已保留（' + '；'.join(failures) + '）') from None


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    if '--validate-only' in sys.argv:
        validate_bundle(ROOT / 'public-data')
        print(json.dumps({'valid': True}))
    elif '--refresh-roster' in sys.argv:
        from update_manifest import fetch_manifest_records
        try:
            records = fetch_manifest_records()
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        write(ROOT / 'manifest_data/resource_manifest.json', {'checkedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'records': records})
        print(json.dumps({'records': len(records)}))
    else:
        print(json.dumps(export(), ensure_ascii=False))
