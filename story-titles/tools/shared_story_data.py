"""Independent public story-title bundle; same transport as character metadata."""
import base64
import datetime
import hashlib
import json
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path

# Also usable from story-titles/tools in the public metadata repository.
if not (Path(__file__).parent / 'shared_profile_data.py').exists():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'character-profiles/tools'))
from shared_profile_data import (ROOT, PACK_HEADER, PACK_NAME, MAX_FILE_BYTES,
                                MAX_PACK_JSON_BYTES, read, write, source_url,
                                validate_stories)

CONFIG_NAME = 'story-titles.config.json'
DOCUMENT_NAME = 'stories.json'


def pack(document):
    validate_stories(document)
    content = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    envelope = {'schemaVersion': 2, 'files': {DOCUMENT_NAME: content.decode('utf-8')},
                'sha256': {DOCUMENT_NAME: hashlib.sha256(content).hexdigest()}}
    raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(content) > MAX_FILE_BYTES or len(raw) > MAX_PACK_JSON_BYTES:
        raise ValueError('剧情标题资料包超出大小限制')
    result = PACK_HEADER + base64.b64encode(zlib.compress(raw, 9)) + b'\n'
    if len(result) > MAX_FILE_BYTES:
        raise ValueError('剧情标题资料包超出大小限制')
    return result


def unpack(data):
    if len(data) > MAX_FILE_BYTES or not data.startswith(PACK_HEADER):
        raise ValueError('剧情标题包版本或大小不正确')
    try:
        compressed = base64.b64decode(data[len(PACK_HEADER):].removesuffix(b'\n'), validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_PACK_JSON_BYTES + 1)
        if len(raw) > MAX_PACK_JSON_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('剧情标题包不完整或超出大小限制')
        envelope = json.loads(raw)
        if (not isinstance(envelope, dict) or set(envelope) != {'schemaVersion', 'files', 'sha256'}
                or envelope['schemaVersion'] != 2 or not isinstance(envelope['files'], dict)
                or set(envelope['files']) != {DOCUMENT_NAME}
                or not isinstance(envelope['files'][DOCUMENT_NAME], str)):
            raise ValueError('剧情标题包格式不正确')
        content = envelope['files'][DOCUMENT_NAME].encode('utf-8')
        if len(content) > MAX_FILE_BYTES or envelope['sha256'] != {DOCUMENT_NAME: hashlib.sha256(content).hexdigest()}:
            raise ValueError('剧情标题包校验失败')
        document = validate_stories(json.loads(content))
        if set(document['stories']) != {'event', 'main', 'other'}:
            raise ValueError('剧情标题包缺少故事分类')
        return document
    except (ValueError, TypeError, KeyError, zlib.error, UnicodeError) as error:
        raise ValueError('剧情标题包还原或校验失败，原有缓存已保留') from error


def export(document, directory):
    directory = Path(directory)
    data = pack(document)
    unpack(data)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / PACK_NAME
    temporary = destination.with_name(destination.name + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(destination)
    return destination


def refresh(root=ROOT, opener=None):
    root = Path(root)
    config = read(root / CONFIG_NAME)
    if not config.get('repository'):
        return {'configured': False, 'updated': False}
    fallbacks = config.get('fallbacks', [])
    if not isinstance(fallbacks, list) or len(fallbacks) > 8 or any(not isinstance(row, dict) for row in fallbacks):
        raise ValueError('剧情标题备用源配置不正确')
    sources = [config, *fallbacks]
    for source in sources:
        source_url(source, PACK_NAME)
        if source.get('format', 'packed') != 'packed':
            raise ValueError('剧情标题下载格式不支持')
        if source.get('proxy'):
            from urllib.parse import urlsplit
            proxy = urlsplit(source['proxy'])
            if proxy.scheme not in ('http', 'https') or not proxy.hostname:
                raise ValueError('剧情标题代理配置不正确')
    failures = []
    for index, source in enumerate(sources):
        transports = [opener or urllib.request.urlopen]
        if source.get('proxy') and opener is None:
            transports.insert(0, urllib.request.build_opener(urllib.request.ProxyHandler({'https': source['proxy']})).open)
        last_error = None
        for transport in transports:
            try:
                request = urllib.request.Request(source_url(source, PACK_NAME), headers={
                    'User-Agent': 'public-story-titles-updater/1.0', 'Accept': 'application/octet-stream'})
                with transport(request, timeout=20) as response:
                    data = response.read(MAX_FILE_BYTES + 1)
                document = unpack(data)
            except (OSError, ValueError, TypeError, KeyError) as error:
                last_error = error
                continue
            write(root / 'data/shared-story-titles.json', document)
            return {'configured': True, 'updated': True, 'usedFallback': index > 0,
                    'stories': {kind: len(rows) for kind, rows in document['stories'].items()}}
        failures.append('HTTP ' + str(last_error.code) if isinstance(last_error, urllib.error.HTTPError) else type(last_error).__name__)
    raise RuntimeError('剧情标题资料源更新失败，原有缓存已保留（' + '；'.join(failures) + '）') from None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--refresh', action='store_true')
    mode.add_argument('--pack', nargs=2, metavar=('JSON', 'DIRECTORY'))
    mode.add_argument('--unpack', nargs=2, metavar=('PACK', 'JSON'))
    args = parser.parse_args()
    if args.pack:
        result = {'file': str(export(read(args.pack[0]), args.pack[1]))}
    elif args.unpack:
        document = unpack(Path(args.unpack[0]).read_bytes())
        write(args.unpack[1], document)
        result = {'file': args.unpack[1]}
    else:
        result = refresh()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
