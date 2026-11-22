"""Commit only public metadata; Git credentials come from the CI environment."""
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from player_paths import data_root
from shared_profile_data import validate_bundle

ROOT = data_root()
FILES = ['public-data/characters.json', 'public-data/stories.json', 'public-data/manifest.json']


def repository_url(value, provider='gitee'):
    host = {'gitee': 'gitee.com', 'github': 'github.com'}.get(provider)
    if not host:
        raise RuntimeError('不支持的资料发布平台')
    if value.startswith('https://' + host + '/'):
        parsed = urllib.parse.urlparse(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError('资料仓库地址不能包含凭据或查询参数')
        value = parsed.path.strip('/').removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*', value):
        raise RuntimeError('META_REPOSITORY 应填写对应平台的 owner/repo')
    return 'https://' + host + '/' + value + '.git'


def publish(project=ROOT, environ=None):
    project = Path(project)
    env = dict(os.environ if environ is None else environ)
    env.pop('DMM_COOKIES_JSON', None)
    env.pop('DMM_HTTP_PROXY', None)
    if not env.get('META_GIT_TOKEN') or not env.get('META_GIT_USER'):
        raise RuntimeError('尚未配置 META_GIT_USER / META_GIT_TOKEN 私密推送凭据')
    provider = env.get('META_PROVIDER', 'gitee')
    url = repository_url(env.get('META_REPOSITORY', ''), provider)
    branch = (env.get('GITEE_BRANCH') if provider == 'gitee' else '') or env.get('META_BRANCH') or ('main' if provider == 'github' else 'master')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*', branch) or '..' in branch or any(not p or p == '.' for p in branch.split('/')):
        raise RuntimeError('资料分支配置不正确')
    validate_bundle(project / 'public-data')
    env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_TRACE': '0', 'GIT_CURL_VERBOSE': '0', 'GIT_TRACE_CURL': '0'})
    with tempfile.TemporaryDirectory(prefix='metadata-push-') as temporary:
        askpass = Path(temporary) / 'askpass.py'
        askpass.write_text('#!/usr/bin/env python3\nimport os,sys\nprint(os.environ["META_GIT_USER"] if "username" in sys.argv[1].lower() else os.environ["META_GIT_TOKEN"])\n', encoding='utf-8')
        askpass.chmod(0o700)
        env['GIT_ASKPASS'] = str(askpass)

        def git(args, success=(0,)):
            result = subprocess.run(['git', '-c', 'credential.helper=', *args], cwd=project, env=env,
                                    capture_output=True, text=True)
            if result.returncode not in success:
                # Git stderr can contain credential-bearing remote URLs. Never echo it.
                raise RuntimeError('资料提交或推送失败，请检查分支和推送凭据；未强制覆盖仓库')
            return result

        repository_root = Path(git(['rev-parse', '--show-toplevel']).stdout.strip()).resolve()
        try:
            prefix = project.resolve().relative_to(repository_root).as_posix()
        except ValueError:
            raise RuntimeError('资料目录不在当前 Git 仓库内') from None
        allowed = {((prefix + '/') if prefix != '.' else '') + name for name in FILES}
        staged = git(['diff', '--cached', '--name-only']).stdout.splitlines()
        if set(staged) - allowed:
            raise RuntimeError('流水线暂存区存在非资料文件，已停止发布')
        git(['add', '--', *FILES])
        if git(['diff', '--cached', '--quiet', '--', *FILES], success=(0, 1)).returncode == 0:
            print('Public metadata unchanged; no commit needed.')
            return False
        git(['-c', 'user.name=Metadata updater', '-c', 'user.email=metadata-updater@users.noreply.' + ('github.com' if provider == 'github' else 'gitee.com'),
             'commit', '-m', 'Update public metadata [skip ci]', '--', *FILES])
        git(['push', url, 'HEAD:refs/heads/' + branch])
        print('Validated public metadata published to ' + provider + '.')
        return True


if __name__ == '__main__':
    try:
        publish()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
