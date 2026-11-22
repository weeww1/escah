"""Owner-only HTTP login and public profile collection; no browser runtime."""
import argparse
import base64
import ctypes
import hashlib
import hmac
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from shared_profile_data import ROOT, read, write

PRIVATE = ROOT / 'data/private/official-login.dpapi'
PLAY = 'https://play.games.dmm.co.jp/game/escalationheroines/'
ORIGIN = 'https://prod.e-heroines.net'
AUTH_URL = ORIGIN + '/server/product/api/Login/Auth'
PROXY_URL = 'https://osapi.dmm.com/gadgets/makeRequest'
LAUNCH_URL = 'https://artemis.games.dmm.co.jp/member/pc/init-game-frame/escalationheroines'
PUBLIC_ACTIONS = {'Character/GetMaster', 'Unit/GetGroupMaster', 'Library/GetCharacterProfile'}
COOKIE_NAMES = {'login_secure_id', 'login_session_id', 'althash', 'has_althash', 'uid', 'secid',
                'ckcy', 'ckcy_remedied_check', 'check_done_login', 'deviceIdentify', 'INT_SESID',
                'INT_SESID_SECURE', 'age_check_done', 'choukou_session'}


class LoginRequired(RuntimeError):
    pass


def protect(data, decrypt=False):
    """Windows CurrentUser DPAPI. Plaintext exists only in process memory."""
    if os.name != 'nt':
        raise RuntimeError('后台登录凭据只支持当前 Windows 用户')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = function(ctypes.byref(source), 'Official metadata collector', None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise RuntimeError('无法读取本机加密登录状态，请用当前 Windows 账号重新绑定')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def save_login(value, path=PRIVATE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = protect(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(encrypted)
    temporary.replace(path)


def load_login(path=PRIVATE):
    if not Path(path).exists():
        raise LoginRequired('尚未保存登录凭据。请在小号原游戏页面点击扩展的“更新登录凭据”。')
    try:
        value = json.loads(protect(Path(path).read_bytes(), decrypt=True))
        if value.get('schemaVersion') != 1:
            raise ValueError()
        return value
    except (ValueError, OSError):
        raise LoginRequired('本机登录状态不可用，请重新绑定') from None


def allowed_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        return False
    if parsed.hostname == 'play.games.dmm.co.jp':
        return parsed.path.rstrip('/') == '/game/escalationheroines'
    if parsed.hostname == 'osapi.dmm.com':
        return parsed.path in ('/gadgets/ifr', '/gadgets/makeRequest')
    if parsed.hostname == 'artemis.games.dmm.co.jp':
        return parsed.path == '/member/pc/init-game-frame/escalationheroines'
    return parsed.hostname == 'prod.e-heroines.net' and parsed.path in {
        '/server/product/api/' + action for action in PUBLIC_ACTIONS | {'Login/Auth'}}


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if not allowed_url(new_url):
            raise LoginRequired('平台登录需要重新验证，请重新绑定；后台不会打开浏览器')
        return super().redirect_request(request, fp, code, message, headers, new_url)


def parse_launch(page, expected_viewer):
    page = html.unescape(page).replace('\\/', '/').replace('\\u0026', '&').replace('\\u003d', '=')
    candidates = re.findall(r'https://osapi\.dmm\.com/gadgets/ifr\?[^\s<>"\'\\]+', page)
    for candidate in candidates:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(candidate).query)
        if query.get('st') and query.get('url') and query.get('viewer'):
            if query['viewer'][0] != expected_viewer:
                raise LoginRequired('平台账号与绑定的小号不一致，已停止采集，请重新绑定')
            gadget = urllib.parse.urlparse(query['url'][0])
            if gadget.scheme != 'https' or gadget.hostname != 'prod.e-heroines.net':
                continue
            return candidate, query['st'][0], query['url'][0]
    raise LoginRequired('未取得小号启动凭据，平台登录可能已过期，请重新绑定')


def decrypt_envelope(document, key, iv):
    if isinstance(document, dict) and isinstance(document.get('encrypt'), str):
        cipher = AES.new(key, AES.MODE_CBC, iv)
        value = unpad(cipher.decrypt(base64.b64decode(document['encrypt'])), AES.block_size)
        document = json.loads(value)
    for _ in range(4):
        if isinstance(document, str):
            document = json.loads(document)
        else:
            break
    return document


class Client:
    def __init__(self, state, proxy='', opener=None):
        self.state = state
        self.jar = http.cookiejar.CookieJar()
        for row in state.get('cookies', []):
            domain = row['domain']
            if domain.lstrip('.') not in ('dmm.com', 'dmm.co.jp', 'games.dmm.co.jp',
                                           'play.games.dmm.co.jp') or row['name'] not in COOKIE_NAMES:
                continue
            expires = None if row.get('session') or row.get('expires', -1) < 0 else int(row['expires'])
            self.jar.set_cookie(http.cookiejar.Cookie(0, row['name'], row['value'], None, False,
                domain, domain.startswith('.'), domain.startswith('.'), row.get('path') or '/', True,
                row.get('secure', True), expires, expires is None, None, None, {}, False))
        proxies = {'https': proxy} if proxy else {}
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler(proxies),
            urllib.request.HTTPCookieProcessor(self.jar), SafeRedirect())
        self.iv = base64.b64decode(state['crypt']['iv'])
        self.salt = base64.b64decode(state['crypt']['salt'])
        self.bootstrap_key = hmac.new(self.salt, base64.b64decode(state['crypt']['defaultKey']), hashlib.sha256).digest()
        self.session_key = None
        self.headers = {key: value for key, value in state.get('headers', {}).items()
                        if key.lower() in ('user-agent', 'x-app-ver', 'x-device', 'x-crypt-on')}
        self.headers.setdefault('User-Agent', 'Mozilla/5.0')

    def request(self, url, *, data=None, headers=None):
        if not allowed_url(url):
            raise RuntimeError('后台客户端拒绝访问非登录或资料接口')
        request = urllib.request.Request(url, data=data, headers={'User-Agent': self.headers['User-Agent'], **(headers or {})})
        try:
            with self.opener.open(request, timeout=20) as response:
                result = response.read(8 * 1024 * 1024 + 1)
                status = response.status
        except urllib.error.HTTPError as error:
            status, result = error.code, error.read(65536)
        except LoginRequired:
            raise
        except (OSError, urllib.error.URLError):
            raise RuntimeError('后台网络连接失败，请确认本地代理正在运行；浏览器不需要运行') from None
        if len(result) > 8 * 1024 * 1024:
            raise RuntimeError('服务器返回超出资料大小限制')
        return status, result.decode('utf-8', errors='replace')

    def login(self):
        status, page = self.request(LAUNCH_URL, headers={'X-Access-From': 'play',
            'X-Redirect-Control': 'ec-mrnhbtk-user', 'Origin': 'https://play.games.dmm.co.jp', 'Referer': PLAY,
            'Accept': 'application/json'})
        if status != 200:
            raise LoginRequired('平台启动入口返回 HTTP %s，请检查代理或重新绑定登录' % status)
        _, signing_token, gadget = parse_launch(page, self.state['expectedViewer'])
        params = {**self.state['authTemplate'], 'url': AUTH_URL, 'st': signing_token, 'gadget': gadget,
                  'httpMethod': 'GET', 'postData': '', 'authz': 'signed', 'contentType': 'JSON'}
        status, response = self.request(PROXY_URL, data=urllib.parse.urlencode(params).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            outer = json.loads(response[response.index('{'):])
            item = outer[AUTH_URL]
            if status != 200 or item.get('rc') != 200:
                raise ValueError()
            auth = decrypt_envelope(json.loads(item['body']), self.bootstrap_key, self.iv)['data']
            if not isinstance(auth['token'], str) or not re.fullmatch('[0-9a-fA-F]{64}', auth['key']):
                raise ValueError()
            self.session_key = hmac.new(self.salt, bytes.fromhex(auth['key']), hashlib.sha256).digest()
            self.headers.update({'X-Token': auth['token'], 'X-Os-Id': str(auth['os_id'])})
        except (ValueError, KeyError, TypeError):
            raise LoginRequired('小号后台认证未通过，已保留资料，请重新绑定登录') from None
        # Keep refreshed platform cookies only; game access tokens need not persist.
        self.state['cookies'] = [{'domain': c.domain, 'name': c.name, 'value': c.value,
            'path': c.path, 'secure': c.secure, 'expires': c.expires or -1, 'session': c.discard}
            for c in self.jar if c.name in COOKIE_NAMES and c.domain.lstrip('.') in
            ('dmm.com', 'dmm.co.jp', 'games.dmm.co.jp', 'play.games.dmm.co.jp')]
        self.state['lastLoginAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    def api(self, action, payload=None, renew=True):
        if action not in PUBLIC_ACTIONS:
            raise RuntimeError('只允许读取公开角色资料')
        if self.session_key is None:
            self.login()
        body = None
        headers = dict(self.headers)
        if payload is not None:
            cipher = AES.new(self.session_key, AES.MODE_CBC, self.iv)
            encrypted = base64.b64encode(cipher.encrypt(pad(json.dumps(payload, separators=(',', ':')).encode(), 16))).decode()
            body = urllib.parse.urlencode({'encrypt': encrypted}).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        status, response = self.request(ORIGIN + '/server/product/api/' + action, data=body, headers=headers)
        try:
            document = decrypt_envelope(json.loads(response), self.session_key, self.iv)
        except (ValueError, TypeError):
            raise RuntimeError('官方资料返回格式异常，HTTP %s；原有资料已保留' % status) from None
        if isinstance(document.get('data'), dict) and document['data'].get('code') == 930001:
            raise RuntimeError('采集账号正在其他终端登录游戏，已停止请求。请退出该账号的游戏后重试。')
        if status in (401, 403) or (isinstance(document.get('data'), dict) and document['data'].get('code') == 930000):
            if renew:
                self.login()
                return self.api(action, payload, renew=False)
            raise LoginRequired('游戏会话已失效，请重新绑定小号登录')
        return status, document


def collect(client, ids=None, refresh=False, emit=print):
    status, document = client.api('Character/GetMaster')
    group_status, group_document = client.api('Unit/GetGroupMaster')
    if status != 200 or group_status != 200 or not isinstance(document.get('data'), list):
        raise RuntimeError('未能读取官方角色基础表，原有资料已保留')
    raw_groups = group_document['data']
    groups = {str(key): value for key, value in (enumerate(raw_groups) if isinstance(raw_groups, list) else raw_groups.items())}
    characters = {}
    for row in document['data']:
        key = str(row['character_id'])
        characters[key] = {'id': int(key), 'name': row['name'], 'sub': row['name'],
            'realName': row['character_name'], 'groupId': row['group'], 'faction': groups.get(str(row['group']), '')}
    if len(characters) < 100 or any(not row['faction'] for row in characters.values()):
        raise RuntimeError('官方角色或阵营表不完整，原有资料已保留')
    write(ROOT / 'data/official-character-basics.json', {'verified': True, 'source': 'official-master-api',
        'capturedAt': client.state['lastLoginAt'], 'characters': characters, 'groups': groups})
    records = read(ROOT / 'manifest_data/resource_manifest.json').get('records', {})
    resource_ids = {int(match[1]) for name in records if (match := re.fullmatch(r'(?:bundle_scenarioscript_char_|bundle_chara_c-panel_)(\d+)', name))}
    eligible = sorted(int(key) for key in characters if int(key) in resource_ids) if resource_ids else sorted(map(int, characters))
    wanted = ids or eligible
    cache = read(ROOT / 'data/official-character-profiles.json')
    cache.setdefault('characters', {})
    todo = [key for key in wanted if refresh or not cache['characters'].get(str(key), {}).get('verified') or
            not all(isinstance(cache['characters'][str(key)].get('fields', {}).get(field), str) for field in ('cv', 'artist', 'bio', 'serif'))]
    failed, completed = [], 0
    for index, key in enumerate(todo):
        emit(json.dumps({'phase': 'profiles', 'completed': index, 'total': len(todo), 'characterId': key}))
        status, document = client.api('Library/GetCharacterProfile', {'character_id': key})
        row = document.get('data', {})
        if status == 200 and all(isinstance(row.get(field), str) for field in ('voice', 'artist', 'desc')):
            fields = {'cv': row['voice'], 'artist': row['artist'], 'bio': row['desc']}
            if isinstance(row.get('serif'), str):
                fields['serif'] = row['serif']
            cache['characters'][str(key)] = {'id': key, 'fields': fields, 'verified': True,
                'responsePath': '$.data', 'source': ORIGIN + '/server/product/api/Library/GetCharacterProfile',
                'capturedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
            write(ROOT / 'data/official-character-profiles.json', cache)
            completed += 1
        else:
            failed.append({'id': key, 'httpStatus': status})
        time.sleep(0.35)
    report = {'completed': completed, 'skipped': len(wanted) - len(todo), 'failed': failed,
              'total': len(wanted), 'excludedBaseOnly': sorted(set(map(int, characters)) - set(eligible)), 'transport': 'http'}
    write(ROOT / 'data/shared-profile-collection-report.json', report)
    write(ROOT / 'data/shared-profile-collection-status.json', {'phase': 'done', **report})
    emit(json.dumps(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--ids')
    parser.add_argument('--refresh-all', action='store_true')
    args = parser.parse_args()
    config = read(ROOT / 'shared-data.config.json')
    if args.bind:
        packet = sys.stdin.read(1024 * 1024 + 1)
        if len(packet) > 1024 * 1024:
            raise RuntimeError('绑定数据过大')
        state = json.loads(packet)
    else:
        state = load_login()
    client = Client(state, proxy=config.get('httpProxy', ''))
    print(json.dumps({'phase': 'login', 'transport': 'http'}), flush=True)
    client.login()
    status, sample = client.api('Library/GetCharacterProfile', {'character_id': 82})
    if status != 200 or not all(isinstance(sample.get('data', {}).get(field), str) for field in ('voice', 'artist', 'desc')):
        raise RuntimeError('后台登录后的资料读取验证失败')
    save_login(client.state)
    if args.bind or args.check:
        print(json.dumps({'authenticated': True, 'browserRequired': False, 'profileCheck': 82}), flush=True)
    else:
        ids = list(dict.fromkeys(map(int, args.ids.split(',')))) if args.ids else None
        if ids and any(not 0 < key < 1000000 for key in ids):
            raise RuntimeError('角色编号无效')
        collect(client, ids=ids, refresh=args.refresh_all, emit=lambda message: print(message, flush=True))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    try:
        main()
    except Exception as error:
        # Never print HTTP bodies, signed URLs, cookies or credential packets.
        message = str(error) if isinstance(error, RuntimeError) else '后台资料采集未完成，原有资料已保留'
        print(json.dumps({'error': message, 'loginRequired': isinstance(error, LoginRequired)}, ensure_ascii=False), flush=True)
        sys.exit(1)
