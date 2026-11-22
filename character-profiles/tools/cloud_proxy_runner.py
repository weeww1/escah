"""Run one secret Mihomo node for this job, with a loopback-only HTTP proxy."""
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

VERSION = 'v1.19.34'
ASSET = 'mihomo-linux-amd64-compatible-' + VERSION + '.gz'
DOWNLOAD = 'https://github.com/MetaCubeX/mihomo/releases/download/' + VERSION + '/' + ASSET
ARCHIVE_SHA256 = '2763f5b414527f7baf6dc57e3f82f3ab561eedf34bfd7c2f2cde1f7e592efe06'
PORT = 18081


def proxy_config(node):
    if not isinstance(node, dict) or not isinstance(node.get('server'), str) or not isinstance(node.get('port'), int) or not 0 < node['port'] < 65536:
        raise RuntimeError('云端节点配置格式不正确')
    if node.get('type') not in {'ss', 'ssr', 'vmess', 'vless', 'trojan', 'hysteria', 'hysteria2', 'tuic', 'socks5', 'http', 'anytls'}:
        raise RuntimeError('当前节点类型暂不支持云端导入')
    if node.get('dialer-proxy'):
        raise RuntimeError('当前节点需要其他代理链，不能作为单个节点导入')
    for key in ('certificate', 'private-key', 'ca'):
        if node.get(key):
            raise RuntimeError('当前节点依赖本机证书文件，不能直接用于云端')
    node = {**node, 'name': 'collector-node'}
    return {'mixed-port': PORT, 'allow-lan': False, 'bind-address': '127.0.0.1',
            'mode': 'rule', 'log-level': 'silent', 'ipv6': False,
            'external-controller': '', 'profile': {'store-selected': False, 'store-fake-ip': False},
            'proxies': [node], 'proxy-groups': [{'name': 'COLLECTOR', 'type': 'select', 'proxies': ['collector-node']}],
            'rules': ['MATCH,COLLECTOR']}


def runtime_directory():
    directory = os.environ.get('RUNNER_TEMP')
    if not directory:
        raise RuntimeError('此入口只在云端任务的临时目录中运行')
    return Path(directory).resolve() / 'public-metadata-proxy'


def install_core(directory):
    request = urllib.request.Request(DOWNLOAD, headers={'User-Agent': 'public-metadata-proxy/1.0'})
    with urllib.request.urlopen(request, timeout=90) as response:
        archive = response.read(64 * 1024 * 1024 + 1)
    if len(archive) > 64 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError('Mihomo 官方内核校验失败，未启动代理')
    binary = gzip.decompress(archive)
    if len(binary) > 128 * 1024 * 1024 or not binary.startswith(b'\x7fELF'):
        raise RuntimeError('Mihomo 内核文件格式不正确')
    destination = directory / 'mihomo'
    destination.write_bytes(binary)
    destination.chmod(0o700)
    return destination


def start():
    value = os.environ.pop('DMM_MIHOMO_NODE_JSON', '')
    if not value:
        print('No cloud node configured; using the configured HTTP proxy or direct connection.')
        return
    if sys.platform != 'linux':
        raise RuntimeError('云端代理入口仅支持 Linux runner')
    try:
        config = proxy_config(json.loads(value))
    except (ValueError, TypeError):
        raise RuntimeError('云端节点 Secret 不是有效的 JSON') from None
    diagnostics = diagnose_node(config['proxies'][0])
    print(json.dumps({'phase': 'proxy-network-check', **diagnostics}), flush=True)
    if not diagnostics['nodeTcpReachable']:
        raise RuntimeError('当前节点入口无法从 GitHub runner 连接，未尝试 DMM 登录')
    directory = runtime_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_file = directory / 'config.json'
    # Open exclusively with owner-only permissions, even before writing content.
    descriptor = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(config, stream)
    binary = install_core(directory)
    tested = subprocess.run([str(binary), '-t', '-d', str(directory), '-f', str(config_file)],
                            capture_output=True, timeout=30)
    if tested.returncode:
        raise RuntimeError('Mihomo 未接受当前节点配置，已隐藏可能含凭据的诊断内容')
    process = subprocess.Popen([str(binary), '-d', str(directory), '-f', str(config_file)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    (directory / 'process.json').write_text(json.dumps({'pid': process.pid}), encoding='utf-8')
    for _ in range(60):
        if process.poll() is not None:
            raise RuntimeError('云端代理进程提前退出，未继续采集')
        try:
            with socket.create_connection(('127.0.0.1', PORT), timeout=0.3):
                break
        except OSError:
            time.sleep(0.25)
    else:
        raise RuntimeError('云端代理未在限定时间内启动')
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write('proxy_url=http://127.0.0.1:%s\n' % PORT)
    print('Cloud proxy started with one private node; listening on loopback only.')


def diagnose_node(node):
    # Report connection stages only; never report upstream hosts or credentials.
    try:
        addresses = socket.getaddrinfo(node['server'], node['port'], type=socket.SOCK_STREAM)
        resolved = bool(addresses)
    except OSError:
        return {'nodeDnsResolved': False, 'nodeTcpReachable': False}
    try:
        with socket.create_connection((node['server'], node['port']), timeout=8):
            pass
        reachable = True
    except OSError:
        reachable = False
    return {'nodeDnsResolved': resolved, 'nodeTcpReachable': reachable}


def stop():
    directory = runtime_directory()
    pid_file = directory / 'process.json'
    if pid_file.exists():
        pid = json.loads(pid_file.read_text(encoding='utf-8')).get('pid')
        if isinstance(pid, int) and pid > 1:
            # Only signal the process whose executable is this job's own core.
            try:
                if Path('/proc/%s/exe' % pid).resolve() == directory / 'mihomo':
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
    # This exact job-owned directory is below RUNNER_TEMP; never use node paths.
    if directory.exists():
        shutil.rmtree(directory)
    print('Cloud proxy stopped and temporary node configuration removed.')


if __name__ == '__main__':
    try:
        {'start': start, 'stop': stop}[sys.argv[1]]()
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else '云端代理操作失败，节点凭据未输出'
        print(json.dumps({'error': message}, ensure_ascii=False), flush=True)
        sys.exit(1)
