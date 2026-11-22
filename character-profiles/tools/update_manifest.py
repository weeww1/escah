"""One-shot manifest updater — the low-request way to find ALL new content.

和你原来的 manifest 流程一样,但把「下最新加密 manifest → 解密 → 生成 URL 清单
→ 和 all_urls 基线 diff」串成一步。一次请求拿官方最新全量目录(icon/立绘/剧情/
音频全类型,每条带 hash),不用挨个敲 CDN。

  官方最新加密 manifest: https://cdn.e-heroines.net/choukou/AssetBundles/Manifest1Ex
                        (+ Manifest2Ex,另一半)AES-256-CBC 解密。

用法(在 tools/):
  python update_manifest.py            下最新 manifest,列出比 all_urls 新增的 bundle
  python update_manifest.py --apply     同上,并把新增合并进 all_urls.txt(更新基线)
  python update_manifest.py --episode    只看新增里的 episode icon
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from player_paths import data_root
ROOT = data_root()
WORKSPACE = ROOT.parent
CDN = "https://cdn.e-heroines.net/choukou/AssetBundles/"
MANIFESTS = ["Manifest1Ex", "Manifest2Ex"]
URL_DIR = ROOT / "manifest_data"   # web-player 里的副本,不碰项目根目录原件
ALL_URLS = URL_DIR / "all_urls.txt"
AES_KEY = b"}vq4p>cLPLpB6/Uk|dISq{jLg<o$G@Sr"
AES_IV = b"DGZv_YSCe0muI6%G"


def decode_manifest(enc):
    """Validate and decode an entire official manifest, including content hashes."""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    encrypted = base64.b64decode(b"".join(enc.split()), validate=True)
    raw = unpad(AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(encrypted), 16)
    data = json.loads(raw.decode("utf-8-sig"))
    records = {}

    def collect(value):
        if isinstance(value, dict):
            if "bundle_name" in value:
                name = value["bundle_name"]
                if not isinstance(name, str) or not name.startswith("bundle_") or any(c in name for c in "/\\\0"):
                    raise ValueError("manifest 中存在无效的 bundle 名称")
                digest = value.get("hash")
                if not isinstance(digest, str) or not digest:
                    raise ValueError(f"manifest 缺少内容 hash: {name}")
                record = {k: value.get(k) for k in ("hash", "crc", "size")}
                if name in records and records[name] != record:
                    raise ValueError(f"manifest 存在冲突记录: {name}")
                records[name] = record
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(data)
    if not records:
        raise ValueError("manifest 没有有效的 bundle 记录")
    return records


def fetch_manifest_records(progress=None):
    """Fetch both halves successfully before exposing a complete catalog."""
    records = {}
    for index, m in enumerate(MANIFESTS):
        found = None
        last_error = None
        for attempt in range(2):
            def notify(received=0, total=0, done=index):
                if progress:
                    progress({"phase": "manifest", "progressDone": done,
                              "progressTotal": len(MANIFESTS), "bundleName": m,
                              "receivedBytes": received, "transferTotal": total,
                              "manifestAttempt": attempt + 1})
            notify()
            req = urllib.request.Request(CDN + m, headers={"User-Agent": "update_manifest/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    total = int(r.headers.get("Content-Length") or 0)
                    chunks, received, last_update = [], 0, 0
                    while chunk := r.read(64 * 1024):
                        chunks.append(chunk)
                        received += len(chunk)
                        now = time.monotonic()
                        if now - last_update >= .2 or received == total:
                            notify(received, total)
                            last_update = now
                    found = decode_manifest(b"".join(chunks))
                break
            except Exception as exc:
                last_error = exc
                print(f"[失败 {attempt + 1}/2] 拉取 {m}: {exc}")
        if found is None:
            raise RuntimeError(f"未取得完整官方目录：{m} 失败，本地索引未更新") from last_error
        for name, record in found.items():
            if name in records and records[name] != record:
                raise ValueError(f"两份 manifest 的记录冲突: {name}")
            records[name] = record
        print(f"[manifest] {m}: {len(found)} bundle")
        notify(done=index + 1)
    return records


def fetch_manifest_bundles():
    return set(fetch_manifest_records())


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def merge_latest_urls(names):
    """Keep historical URLs and atomically add entries missing from both indexes."""
    new = sorted(set(names) - load_baseline())
    if not new:
        return 0
    original = ALL_URLS.read_text(encoding="utf-8") if ALL_URLS.exists() else ""
    text = original.rstrip("\r\n")
    if text:
        text += "\n"
    text += "\n".join(CDN + name for name in new) + "\n"
    atomic_write_text(ALL_URLS, text)
    return len(new)


def load_baseline():
    have = set()
    for p in (ALL_URLS, URL_DIR / "ss_urls.txt"):
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if "bundle_" in line:
                    have.add(line.rsplit("/", 1)[-1])
    return have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="把新增合并进 all_urls.txt")
    ap.add_argument("--episode", action="store_true", help="只看新增里的 episode icon")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        latest = fetch_manifest_bundles()
    except ImportError:
        sys.exit("需要 pycryptodome: pip install pycryptodome")
    except (RuntimeError, ValueError) as exc:
        sys.exit(str(exc))
    if not latest:
        sys.exit("没拿到 manifest(检查网络/代理)。")

    baseline = load_baseline()
    new = sorted(latest - baseline)
    print(f"\n官方最新 {len(latest)} bundle | 你的基线 {len(baseline)} | 新增 {len(new)}")

    if args.episode:
        ep = [n for n in new if n.startswith("bundle_episodeicons_episode_character_")]
        print(f"新增 episode icon {len(ep)}: " + " ".join(re.sub(r'\D', '', n) for n in ep))
        return

    if not new:
        print("已是最新,无新增。")
        return
    from collections import Counter
    kinds = Counter(re.match(r'bundle_([a-z0-9]+)', n).group(1) for n in new)
    print("新增分类:")
    for k, v in kinds.most_common():
        print(f"  {v:4d}  {k}")
    print("\n前 30 个新增:", " ".join(new[:30]))

    if args.apply:
        added = merge_latest_urls(latest)
        print(f"\n已把 {added} 个新增追加进 {ALL_URLS.name}(基线已更新)。")
        print("现在可以跑 download_char.py --scan / download_icons.py 等,会看到这些新内容。")


if __name__ == "__main__":
    main()
