"""Mirror only the three validated public JSON files into the existing Gitee repo."""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from player_paths import data_root
from shared_profile_data import validate_bundle
from cloud_publish_gitee import FILES, publish, repository_url


def mirror(project=None, environ=None):
    project = Path(project) if project is not None else data_root()
    validate_bundle(project / 'public-data')
    env = dict(os.environ if environ is None else environ)
    for key in list(env):
        if key.startswith('GIT_CONFIG_') or key in ('DMM_COOKIES_JSON', 'DMM_HTTP_PROXY', 'DMM_MIHOMO_NODE_JSON', 'GH_TOKEN', 'GITHUB_TOKEN', 'GITEE_BRANCH'):
            env.pop(key, None)
    if not env.get('META_GIT_USER') or not env.get('META_GIT_TOKEN'):
        raise RuntimeError('尚未配置 Gitee 镜像推送凭据')
    url = repository_url(env.get('META_REPOSITORY', ''), 'gitee')
    branch = env.get('META_BRANCH', 'master')
    if branch != 'master' or url != 'https://gitee.com/weeww123321/escah.git':
        raise RuntimeError('Gitee 镜像目标与已授权仓库不符')
    authorization = base64.b64encode((env['META_GIT_USER'] + ':' + env['META_GIT_TOKEN']).encode()).decode()
    env.update({'META_PROVIDER': 'gitee', 'GIT_CONFIG_COUNT': '2',
                'GIT_CONFIG_KEY_0': 'http.https://gitee.com/.extraHeader',
                'GIT_CONFIG_VALUE_0': 'Authorization: Basic ' + authorization,
                'GIT_CONFIG_KEY_1': 'credential.helper', 'GIT_CONFIG_VALUE_1': '',
                'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'Never',
                'GIT_TRACE': '0', 'GIT_CURL_VERBOSE': '0', 'GIT_TRACE_CURL': '0'})
    with tempfile.TemporaryDirectory(prefix='gitee-public-mirror-') as temporary:
        checkout = Path(temporary) / 'repo'
        result = subprocess.run(['git', 'clone', '--single-branch', '--depth', '1', '--branch', branch, url, str(checkout)],
                                env=env, capture_output=True, timeout=90)
        if result.returncode:
            raise RuntimeError('无法读取 Gitee 镜像仓库，请检查网络和推送授权')
        output = checkout / 'character-profiles'
        (output / 'public-data').mkdir(parents=True, exist_ok=True)
        for relative in FILES:
            shutil.copyfile(project / relative, output / relative)
        validate_bundle(output / 'public-data')
        # Existing unrelated files and history stay in this separate checkout.
        return publish(output, env)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    try:
        changed = mirror()
        print('Gitee public metadata mirror verified' + (' and updated.' if changed else '; already current.'))
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else 'Gitee 公开资料同步失败，凭据未输出'
        print(message, file=sys.stderr)
        sys.exit(1)
