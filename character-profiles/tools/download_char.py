"""Auto-download every DL bundle a character needs, by character id.

Replaces the manual "grep all_urls.txt + wget" step in PIPELINE.md. Two phases:

  A. id-tagged bundles — bundle names encode the char id in fixed positions
     (scenarioscript_char_358, standimage_stand_0358_*, names_name_00358, ...).
     We match id tokens against the URL index (all_urls.txt + ss_urls.txt).
  B. scenario-driven deps — after the scenarioscript bundle lands, parse the
     official scenario JSON and resolve every referenced resource to its bundle
     by the fixed naming convention (same one extract_audio.py documents):
       BackImage ref -> bundle_bgimage_<ref.lower()>
       Sound ref     -> bundle_sound_<ref.lower()>
       Music ref     -> bundle_music_<ref.lower()>
     (voices are already covered by bundle_voice_adv_char_<id>_* in phase A)

Usage (from anywhere):
  python download_char.py --id 358                # download what's missing
  python download_char.py --id 358 --dry-run      # only list
  python download_char.py --id 358 --pipeline     # then run export/extract/build
  python download_char.py --id 358 --full         # also icons/battle/item bundles

Existing non-empty DL files are skipped (delete or --force to redownload).
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# --- PyInstaller 静态收集提示 ---
# run_pipeline 用 importlib.import_module 动态调下列步骤模块,PyInstaller 静态分析看不到,
# 打进 exe 需在此显式列出。if False 让打包分析器扫到这些 import(收集进 exe),
# 运行时不执行 → 不产生副作用/循环 import。新增步骤模块务必加到这里,否则 exe 里会缺。
if False:  # noqa
    import export, sort_images, extract_scenario, extract_audio  # noqa
    import build_manifest, build_catalog, scrape_wiki_info, download_icons, build_cpak  # noqa

from player_paths import data_root, bundle_directory
ROOT = data_root()
WORKSPACE = ROOT.parent
# 一律以 web-player 为根:DL / export.py 优先用 web-player 里的,没有再回退项目根
DL = bundle_directory()
EXPORT_PY = ROOT / "export.py" if (ROOT / "export.py").exists() else WORKSPACE / "export.py"
# URL 清单放 web-player 自己的副本里,不碰项目根目录的原件
URL_DIR = ROOT / "manifest_data"
URL_LISTS = [URL_DIR / "all_urls.txt", URL_DIR / "ss_urls.txt"]
INSTALLED_CACHE = ROOT / "data" / "installed.json"


def install_status(char_id):
    """返回 (ok, reason)。ok=True 才算真装好。判定不是"文件在不在"这种松检查,
    而是核对 manifest 记录的完整性 + 每条解析路径的实际文件都在磁盘上 ——
    这样即使 358 缺了几条语音/几张图也能查出来(manifest 的 missing 字段 +
    逐条 stands/images/audio 路径存在性),不会误判"已装好"而跳过。"""
    char_dir = ROOT / "characters" / str(char_id)
    manifest = char_dir / "manifest.json"
    scenario = char_dir / "scenario.json"
    if not manifest.exists():
        return False, "manifest 缺失"
    if not scenario.exists():
        return False, "剧本缺失"
    try:
        m = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"manifest 损坏: {exc}"
    miss = m.get("missing", {})
    for kind in ("stands", "images", "audio"):
        if miss.get(kind):
            return False, f"{kind} 有 {len(miss[kind])} 项未解析(manifest.missing)"
    # 逐条核对解析出的路径文件是否真在磁盘(抓"解过但后来被删")
    broken = 0
    for section in ("images", "audio"):
        for _ref, path in (m.get(section) or {}).items():
            if not (ROOT / path).exists() and not (WORKSPACE / path).exists():
                broken += 1
    for _ref, layer in (m.get("stands") or {}).items():
        for key in ("base", "expression", "full"):
            p = layer.get(key)
            if p and not (ROOT / p).exists() and not (WORKSPACE / p).exists():
                broken += 1
                break
    if broken:
        return False, f"{broken} 个已登记的文件在磁盘上找不到(被删?)"
    return True, "ok"


def is_installed(char_id):
    return install_status(char_id)[0]


def load_installed():
    if INSTALLED_CACHE.exists():
        return json.loads(INSTALLED_CACHE.read_text(encoding="utf-8"))
    return {}


def mark_installed(char_id):
    import datetime
    data = load_installed()
    scen = ROOT / "characters" / str(char_id) / "scenario.json"
    chapters = len(json.loads(scen.read_text(encoding="utf-8"))) if scen.exists() else 0
    data[str(char_id)] = {"installed_at": datetime.date.today().isoformat(), "chapters": chapters}
    INSTALLED_CACHE.parent.mkdir(exist_ok=True)
    INSTALLED_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

# 默认只下 ADV 播放 + catalog 需要的类别(bundle_ 前缀之后的开头)。--full 解除限制。
ADV_PREFIXES = (
    "scenarioscript_char_",
    "voice_adv_char_",
    "standimage_stand_",        # 立绘几何+分层图(每套服装一个)
    "standimages_stand_",
    "profileimages_stand_",
    "systemvoice_",
    "names_name_",
    "chara_c-panel_",           # build_catalog.py 的角色面板图
    "miniicons_character_mini_",  # 章节胶囊头像
)


def existing_bundles():
    """Bundle files anywhere under DL/ (历史下载散在 DL 根和 DL/ss 等子目录)."""
    if not DL.exists():
        return {}
    return {p.name: p for p in DL.rglob("bundle_*") if p.is_file()}


def load_url_index():
    index = {}
    for path in URL_LISTS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("http"):
                index[line.rsplit("/", 1)[-1]] = line
    if not index:
        sys.exit(f"no URL lists found ({', '.join(str(p) for p in URL_LISTS)})")
    return index


def id_tokens(char_id, full):
    # 官方各类 bundle 对 ID 补零位数不一:无零(chara_c-panel_358)、3位(chara_c-panel_082)、
    # 4位(voice_..._0358)、5位(names_name_00358)。全都要覆盖,否则低 ID 角色会漏包(如 82 的面板图)。
    tokens = {str(char_id), f"{char_id:03d}", f"{char_id:04d}", f"{char_id:05d}"}
    if full:
        # 1XXXX = 同角色的变体 unit,3XXXX = 关联道具/票券图标
        tokens |= {f"1{char_id:04d}", f"3{char_id:04d}"}
    return tokens


def match_bundles(index, char_id, full):
    tokens = id_tokens(char_id, full)

    def matches_character(body):
        """Match the ID field of known bundle names, not any numeric field.

        In particular, ``bundle_voice_adv_char_100_1`` has character ID 100
        and voice-part number 1.  Treating every underscore-separated value as
        an ID made a request for character 1 pull every ``*_1`` voice bundle.
        """
        if body.startswith("voice_adv_char_"):
            # voice_adv_char_<character id>_<voice part>
            return body[len("voice_adv_char_"):].split("_", 1)[0] in tokens
        if body.startswith("standimage_stand_"):
            # standimage_stand_<character id>_<costume/expression hash>
            return body[len("standimage_stand_"):].split("_", 1)[0] in tokens
        for prefix in (
            "scenarioscript_char_", "standimages_stand_", "profileimages_stand_",
            "systemvoice_", "names_name_", "chara_c-panel_",
            "miniicons_character_mini_",
        ):
            if body.startswith(prefix):
                # These names put the character ID at the end.  This also
                # covers systemvoice_supporter_<character id>.
                return body.rsplit("_", 1)[-1] in tokens
        # --full includes non-ADV bundle families whose ID position is not
        # standardized.  Keep the old broad lookup only for those unknown
        # families; all normal ADV families use the exact rules above.
        return full and any(part in tokens for part in body.split("_"))

    hits = []
    for name in index:
        if not name.startswith("bundle_"):
            continue
        body = name[len("bundle_"):]
        if not full and not body.startswith(ADV_PREFIXES):
            continue
        if matches_character(body):
            hits.append(name)
    return sorted(hits)


def scenario_bundle_deps(bundle, index):
    """Parse any downloaded ADV scenario bundle and map its refs to bundle names.

    覆盖 build_manifest.collect_refs 的全部引用类型,保证 missing 归零:
      BackImage  -> bundle_bgimage_<ref.lower()>                       (整名直映)
      Sound      -> bundle_sound_<ref.lower()>   Music 同理
      StandImage -> bundle_standimage_<前三段.lower()>                  (客串角色立绘;
                    ref "stand_0188_8AEE967B_表情" 的表情段不进 bundle 名)
      EventImage -> bundle_eventimage_<哈希前缀><前两段.lower()>          (前缀未知,
                    ref "83438378_30333030_章节_页" 按后缀匹配索引)
    """
    from extract_scenario import read_scenarios      # same tools/ dir
    from extract_audio import collect_audio_refs
    scenarios = read_scenarios(bundle)
    sounds, musics = collect_audio_refs(scenarios)
    backgrounds, events, stands = set(), set(), set()
    for asset in scenarios:
        backgrounds |= set(asset.get("BackImages", []))
        events |= set(asset.get("EventImages", []))
        stands |= set(asset.get("StandImages", [])) | set(asset.get("StandFullImages", []))
        for method in asset.get("Methods", []):
            if method.get("BackImage"):
                backgrounds.add(method["BackImage"])
            if method.get("EventImage"):
                events.add(method["EventImage"])
            if method.get("StandImage"):
                stands.add(method["StandImage"])
    wanted, unknown = set(), []
    for kind, refs in (("bgimage", backgrounds), ("sound", sounds), ("music", musics)):
        for ref in sorted(refs):
            name = f"bundle_{kind}_{ref.lower()}"
            (wanted.add(name) if name in index else unknown.append(name))
    for ref in sorted(stands):
        name = "bundle_standimage_" + "_".join(ref.lower().split("_")[:3])
        (wanted.add(name) if name in index else unknown.append(name))
    event_index = [n for n in index if n.startswith("bundle_eventimage_")]
    for ref in sorted(events):
        suffix = "_".join(ref.lower().split("_")[:2])
        matches = [n for n in event_index if n.endswith(suffix)]
        if matches:
            wanted.update(matches)
        else:
            unknown.append(f"bundle_eventimage_*{suffix}")
    return sorted(wanted), unknown


def scenario_dep_bundles(char_id, index, have):
    """Character-compatible wrapper for phase-B dependency discovery."""
    bundle = have.get(f"bundle_scenarioscript_char_{char_id}")
    if not bundle:
        print(f"[deps] bundle_scenarioscript_char_{char_id} not on disk yet, skipping scenario deps")
        return [], []
    return scenario_bundle_deps(bundle, index)


def download(name, url, have, force, timeout=60, retries=3):
    existing = have.get(name)
    if existing is not None and existing.stat().st_size > 0 and not force:
        return "skip", name, existing.stat().st_size
    dest = DL / name
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "download_char/1.0"})
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            tmp.replace(dest)
            return "ok", name, dest.stat().st_size
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(min(2 * attempt, 8))
    tmp.unlink(missing_ok=True)
    return f"FAIL {last_err}", name, 0


def download_all(names, index, have, force, workers, dry_run):
    if dry_run:
        for name in names:
            state = "have" if name in have else "need"
            print(f"  [{state}] {name}")
        return 0
    DL.mkdir(exist_ok=True)
    ok = fail = skip = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(download, n, index[n], have, force) for n in names]
        for i, fut in enumerate(cf.as_completed(futures), 1):
            state, name, size = fut.result()
            if state == "ok":
                ok += 1
            elif state == "skip":
                skip += 1
            else:
                fail += 1
            print(f"[{i}/{len(names)}] {state:4s} {name} ({size:,}B)")
    print(f"-> ok={ok} skip={skip} fail={fail}")
    return fail


# 含图的 bundle 前缀(解图只处理这些,语音/剧本包不用喂给 export)
IMAGE_BUNDLE_KEYS = ("standimage", "profileimages", "bgimage", "eventimage",
                     "names_name", "chara_c-panel", "miniicons")


FROZEN = getattr(sys, "frozen", False)   # True = 已打包成 exe


def _run_step_inproc(module_name, argv):
    """打包(exe)模式下进程内直接调各步骤的 main(),不 subprocess(exe 里没有 python)。"""
    import importlib
    m = importlib.import_module(module_name)
    old = sys.argv
    sys.argv = [module_name] + argv
    try:
        m.main()
    except SystemExit as e:
        if e.code not in (0, None):
            raise RuntimeError(f"步骤 {module_name} 失败: {e.code}")
    finally:
        sys.argv = old


def run_pipeline(char_id, bundle_names=None):
    """PIPELINE.md 新角色四步 + catalog 重建,顺序固定。
    bundle_names 给定时,解图只处理本角色相关的图包(不再全库扫,快且不刷屏)。"""
    export_args = ["-i", str(DL), "-o", str(ROOT / "images"), "--skip-existing", "--hex-case", "lower"]
    list_file = None
    if bundle_names:
        image_bundles = [n for n in bundle_names if any(k in n for k in IMAGE_BUNDLE_KEYS)]
        list_file = ROOT / f"_export_list_{char_id}.tmp"
        list_file.write_text("\n".join(image_bundles), encoding="utf-8")
        export_args += ["--bundle-list", str(list_file)]
    # (模块名, argv) —— exe 模式进程内调 main();开发模式 subprocess 调脚本
    steps = [
        ("export", export_args),
        ("sort_images", []),
        ("extract_scenario", ["--char", str(char_id)]),
        ("extract_audio", ["--char", str(char_id)]),
        ("build_manifest", ["--char", str(char_id)]),
        ("scrape_wiki_info", ["--char", str(char_id)]),
        ("build_catalog", ["--quiet"]),
    ]
    script_of = {"export": EXPORT_PY, "sort_images": ROOT / "tools/sort_images.py",
                  "extract_scenario": ROOT / "tools/extract_scenario.py",
                  "extract_audio": ROOT / "tools/extract_audio.py",
                  "build_manifest": ROOT / "tools/build_manifest.py",
                  "scrape_wiki_info": ROOT / "tools/scrape_wiki_info.py",
                  "build_catalog": ROOT / "tools/build_catalog.py"}
    if FROZEN:   # exe 里模块常驻,清掉可能过期的图池/文件缓存
        for mn, attr in (("export", "_EXISTING_INDEX"), ("build_char387_web_data", "_POOL_INDEX")):
            try:
                import importlib
                getattr(importlib.import_module(mn), attr).clear()
            except Exception:
                pass
    try:
        for mod, argv in steps:
            if not (mod == "build_catalog" and "--quiet" in argv):
                print(f"\n=== {mod} {' '.join(argv[:2])} ===")
            if FROZEN:
                _run_step_inproc(mod, argv)
            else:
                result = subprocess.run([sys.executable, str(script_of[mod]), *argv], cwd=str(ROOT))
                if result.returncode != 0:
                    sys.exit(f"pipeline step failed (exit {result.returncode}): {mod}")
    finally:
        if list_file and list_file.exists():
            list_file.unlink()


def story_character_ids(index):
    """URL 索引里所有带剧情(scenarioscript)的角色 ID。"""
    ids = set()
    for name in index:
        m = re.fullmatch(r"bundle_scenarioscript_char_(\d+)", name)
        if m:
            ids.add(int(m.group(1)))
    return sorted(ids)


def scan_updates(index):
    """打开时按“完整 / 待修复 / 未下载”显示本地角色状态。"""
    all_ids = story_character_ids(index)
    complete = [c for c in all_ids if is_installed(c)]

    # 有任一本地角色产物、但未通过完整性检查的，叫“待修复”；
    # 不再把已有剧情/资料的角色误写成“未安装”。
    def has_local_data(cid):
        return any((
            (ROOT / "characters" / str(cid) / "scenario.json").exists(),
            (ROOT / "characters" / str(cid) / "manifest.json").exists(),
            (ROOT / "characters" / str(cid) / "info.json").exists(),
            (ROOT / "audio" / f"char{cid}").exists(),
        ))

    repair = [c for c in all_ids if c not in complete and has_local_data(c)]
    not_downloaded = [c for c in all_ids if c not in complete and c not in repair]
    print(
        f"剧情角色 {len(all_ids)} 个 | 完整安装 {len(complete)}"
        f" | 待修复 {len(repair)} | 未下载 {len(not_downloaded)}"
    )
    # ID 范围 + 中间没有剧情的号(输了也下不了,提前提示)
    if all_ids:
        idset = set(all_ids)
        gaps = [i for i in range(all_ids[0], all_ids[-1] + 1) if i not in idset]
        print(f"可下载 ID 范围 {all_ids[0]}-{all_ids[-1]}")
        if gaps:
            print(f"不可下载: {' '.join(map(str, gaps))}")
    if repair:
        print(f"待修复: {' '.join(map(str, repair))}")
    if not_downloaded:
        head = " ".join(map(str, sorted(not_downloaded, reverse=True)[:40]))
        print(f"未下载: {head}{' ...' if len(not_downloaded) > 40 else ''}")
    else:
        print("没有尚未下载的角色。")
    return repair + not_downloaded


def parse_ids(text):
    """'300 301,305-308' -> [300, 301, 305, 306, 307, 308]"""
    ids = []
    for token in text.replace(",", " ").replace("，", " ").split():
        if "-" in token:
            lo, hi = token.split("-", 1)
            ids.extend(range(int(lo), int(hi) + 1))
        else:
            ids.append(int(token))
    return sorted(set(ids))


def run_one(char_id, args, index):
    # 已装好且非强制 → 直接跳过,不下载(删了 DL 也不会重复下载)
    ok, reason = install_status(char_id)
    if ok and not args.force and not args.dry_run:
        print(f"角色 {char_id} 已完整安装,跳过。要重装加 --force")
        return True
    if not ok and (ROOT / "characters" / str(char_id) / "manifest.json").exists() and not args.dry_run:
        print(f"角色 {char_id} 装过但不完整({reason}),重新下载补齐…")
    have = existing_bundles()
    names = match_bundles(index, char_id, args.full)
    if not any(n == f"bundle_scenarioscript_char_{char_id}" for n in names):
        print(f"WARNING: no scenarioscript bundle for id {char_id} — 该 id 可能没有 ADV 剧情")
    print(f"\n[phase A] id-tagged bundles for {char_id}: {len(names)}")
    fails = download_all(names, index, have, args.force, args.workers, args.dry_run)

    if not args.dry_run:
        have = existing_bundles()  # 刚下的也算
        wanted, unknown = scenario_dep_bundles(char_id, index, have)
        print(f"\n[phase B] scenario deps (bg/sound/music): {len(wanted)}")
        fails += download_all(wanted, index, have, args.force, args.workers, False)
        for name in unknown:
            print(f"  UNKNOWN (not in url lists, manifest可能更新过): {name}")

    if args.pipeline and not args.dry_run:
        if fails:
            print(f"!! {char_id}: {fails} downloads failed — 跳过管线,重跑可续传")
            return False
        run_pipeline(char_id, list(names) + list(wanted))
        mark_installed(char_id)   # 记进安装清单 data/installed.json
        print(f"\nDone. 打开 http://localhost:8123/index.html?char={char_id}")
    return fails == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", help="character id(s), e.g. 358 / '300,305' / '301-310'; 不给则交互询问")
    parser.add_argument("--dry-run", action="store_true", help="list bundles, no download")
    parser.add_argument("--full", action="store_true", help="also non-ADV bundles (icons/battle/items)")
    parser.add_argument("--force", action="store_true", help="redownload existing files")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--pipeline", action="store_true", help="run export/extract/build after download")
    parser.add_argument("--check", action="store_true", help="体检所有已装角色的完整性,不下载")
    parser.add_argument("--scan", action="store_true", help="只检查有没有未安装的新角色,不下载")
    args = parser.parse_args()

    if args.scan:
        scan_updates(load_url_index())
        return

    if args.check:
        import glob
        ids = sorted(int(Path(p).parent.name)
                     for p in glob.glob(str(ROOT / "characters" / "*" / "manifest.json")))
        print(f"体检 {len(ids)} 个已装角色:")
        bad = []
        for cid in ids:
            ok, reason = install_status(cid)
            print(f"  {cid}: {'[OK] 完整' if ok else '[缺] ' + reason}")
            if not ok:
                bad.append(cid)
        if bad:
            print(f"\n不完整: {bad}")
            print(f"修复: python download_char.py --id {','.join(map(str, bad))} --pipeline --force")
        else:
            print("\n全部完整。")
        return

    index = load_url_index()

    if args.id:
        ids = parse_ids(str(args.id))
    else:
        # 双击 bat 的交互模式:先做更新检查(有没有新角色),再问要下载哪个
        print("=== 更新检查 ===")
        scan_updates(index)
        print("\n角色 ID 范围 1-394。支持多个和区间,例: 300  或  300 305  或  301-310")
        try:
            ids = parse_ids(input("要下载的角色 ID(直接回车跳过): ").strip())
        except (ValueError, EOFError):
            sys.exit("没听懂,请输入数字 ID(可空格分隔、可用 301-310 区间)")
        if not ids:
            print("未选择角色,结束。")
            return
        args.pipeline = True

    print(f"url index: {len(index)} bundles")
    results = {}
    for char_id in ids:
        print(f"\n{'='*20} 角色 {char_id} {'='*20}")
        results[char_id] = run_one(char_id, args, index)
    if len(ids) > 1:
        print("\n==== 汇总 ====")
        for cid, ok in results.items():
            print(f"  {cid}: {'OK' if ok else 'FAILED(可重跑续传)'}")


if __name__ == "__main__":
    main()
