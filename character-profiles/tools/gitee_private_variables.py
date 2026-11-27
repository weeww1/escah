"""Gitee Go PASSWORD parameters via saved owner PAT; no browser required.

Secrets stay in process memory and HTTPS bodies. Logs contain names/status only.
"""
import json
import urllib.error
import urllib.request

REPOSITORY = 'https://gitee.com/weeww123321/escah'
PARAMETERS_URL = 'https://go-api.gitee.com/weeww123321/escah/gitee-go/ipipe/rest/v5/params'
NAMES = {'DMM_COOKIES_JSON', 'DMM_HTTP_PROXY', 'META_GIT_USER', 'META_GIT_TOKEN', 'META_REPOSITORY', 'META_BRANCH'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def saved_access():
    from background_official_client import load_login
    from shared_profile_data import ROOT
    access = load_login(ROOT / 'data/private/gitee-access.dpapi')
    if access.get('repository') != REPOSITORY + '.git':
        raise ValueError('本机仓库授权与资料仓库不符')
    return access


class GiteeVariables:
    def __init__(self, token, opener=None):
        if not isinstance(token, str) or not token or any(ord(c) < 32 for c in token):
            raise ValueError('Gitee 仓库令牌格式不正确')
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def request(self, rows=None):
        body = None if rows is None else json.dumps(rows, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(PARAMETERS_URL, data=body,
                    headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'},
                    method='GET' if rows is None else 'PUT')
        try:
            with self.opener.open(request, timeout=30) as response:
                content = response.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise ValueError()
            document = json.loads(content)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308, 401, 403):
                raise RuntimeError('Gitee 仓库令牌或写入权限已失效，请重新授权') from None
            raise RuntimeError('Gitee 私密参数接口暂时不可用，未确认更新成功') from None
        except (ValueError, OSError):
            raise RuntimeError('未收到有效的 Gitee 配置响应，请检查网络后重试') from None
        if document.get('code') != 0:
            raise RuntimeError('Gitee 未接受私密参数配置，请检查仓库权限和内容长度')
        return document.get('data')

    def list(self):
        data = self.request()
        rows = (data or {}).get('parameters', [])
        if not isinstance(rows, list):
            raise RuntimeError('Gitee 私密参数列表格式不正确')
        return rows

    def upsert_secrets(self, values):
        if not values or not set(values) <= NAMES:
            raise ValueError('不允许修改此参数')
        for value in values.values():
            if not isinstance(value, str) or not value or len(value) > 256 * 1024:
                raise ValueError('私密参数内容为空或过大')
        # The official API replaces the list. Preserve unrelated parameters.
        rows = self.list()
        updated = [row for row in rows if row.get('key') not in values]
        updated.extend({'key': name, 'type': 'PASSWORD', 'defaultValue': value,
                        'description': 'Public metadata updater credential'} for name, value in values.items())
        self.request(updated)
        confirmed = {row.get('key'): row for row in self.list()}
        for name in values:
            if confirmed.get(name, {}).get('type') != 'PASSWORD':
                raise RuntimeError('Gitee 未确认参数的保密类型，更新未完成')
        return [{'name': name, 'private': True} for name in values]


def update_dmm_cookie(state):
    access = saved_access()
    value = json.dumps({'expectedViewer': state['expectedViewer'], 'cookies': state['cookies']}, separators=(',', ':'))
    return GiteeVariables(access['token']).upsert_secrets({'DMM_COOKIES_JSON': value})


def provision():
    from background_official_client import load_login
    access = saved_access()
    state = load_login()
    values = {'DMM_COOKIES_JSON': json.dumps({'expectedViewer': state['expectedViewer'], 'cookies': state['cookies']}, separators=(',', ':')),
              'META_GIT_USER': access['username'], 'META_GIT_TOKEN': access['token'],
              'META_REPOSITORY': REPOSITORY + '.git', 'META_BRANCH': 'master'}
    return {'ok': True, 'variables': GiteeVariables(access['token']).upsert_secrets(values)}


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    try:
        print(json.dumps(provision(), ensure_ascii=False))
    except Exception as error:
        message = str(error) if isinstance(error, (RuntimeError, ValueError)) else '私密参数配置未完成，凭据未输出'
        print(json.dumps({'ok': False, 'error': message}, ensure_ascii=False))
        sys.exit(1)
