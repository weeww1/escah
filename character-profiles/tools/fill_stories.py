"""Fill official story families, validate cached/downloaded bundles, then install.

Downloads stay resumable at bundle/story boundaries. The checker history is never
used as an installation receipt: only a verified playable installation is saved.
"""
import argparse
import concurrent.futures
import importlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
import urllib.parse
import zlib
from pathlib import Path

import download_char as dc
import story_paths as sp
import update_manifest as um

# Static imports for the frozen downloader's dynamic pipeline.
if False:
    import export, sort_images, extract_scenario, extract_audio, build_manifest
    import build_catalog, build_story_catalog

ROOT = sp.ROOT
LABELS = {"main": "主线", "event": "活动", "other": "其他故事", "char": "角色故事"}
RECEIPTS = ROOT / "data" / "story_installed.json"
BUNDLE_RECEIPTS = ROOT / "data" / "story_bundle_cache.json"
REPORT = ROOT / "data" / "story_fill_report.json"
PIPELINE_VERSION = 1
SCRIPT_PATTERN = re.compile(r"bundle_scenarioscript_(main|event|other|char)_(\d+)\Z")


def read_state(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"记录文件损坏: {path}")
    return value


def save_state(path, value):
    um.atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def plan(records, kinds, ids=None):
    result = []
    for name in records:
        match = SCRIPT_PATTERN.fullmatch(name)
        if match and match[1] in kinds and (ids is None or int(match[2]) in ids):
            result.append((match[1], int(match[2]), name))
    return sorted(result, key=lambda item: (sp.KINDS.index(item[0]), item[1]))


def bundle_crc(path):
    """Unity's CRC covers decompressed data blocks, not the compressed file."""
    from UnityPy.files.BundleFile import BundleFile
    from UnityPy.streams import EndianBinaryReader

    class CRCBundle(BundleFile):
        def read_files(self, reader, files):
            self.calculated_crc = zlib.crc32(reader.bytes)

    bundle = CRCBundle(EndianBinaryReader(path.read_bytes()), parent=None)
    return bundle.calculated_crc


def validate_bundle(path, record):
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("文件不存在或为空")
    if record.get("size") is not None and path.stat().st_size != int(record["size"]):
        raise ValueError("文件大小与官方目录不符")
    crc = bundle_crc(path)
    if record.get("crc") is None or crc != int(record["crc"]):
        raise ValueError("资源 CRC 与官方目录不符")
    return {"hash": record["hash"], "crc": crc, "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns}


class BundleStore:
    def __init__(self, records, progress=None):
        self.records = records
        self.progress = progress
        self.progress_lock = threading.Lock()
        self.progress_state = {}
        self.dl = ROOT / "DL"
        self.dl.mkdir(parents=True, exist_ok=True)
        self.cache = read_state(BUNDLE_RECEIPTS)
        self.have = {}
        # Prefer the player's copy; read the historical cache without changing it.
        for folder in (ROOT.parent / "DL", self.dl):
            if folder.exists():
                self.have.update({p.name: p for p in folder.rglob("bundle_*") if p.is_file()})
        self.ready = set()
        self.changed = set()

    def notify(self, **fields):
        if self.progress:
            with self.progress_lock:
                self.progress_state.update(fields)
                self.progress(dict(self.progress_state))

    def _ensure(self, name):
        self.notify(bundleName=name, receivedBytes=0, transferTotal=0)
        record = self.records[name]
        dest = self.dl / name
        existing = self.have.get(name)
        previous = self.cache.get(name, {})
        if existing is not None:
            try:
                stat = existing.stat()
                cached = (previous.get("hash") == record["hash"]
                          and previous.get("crc") == record.get("crc")
                          and previous.get("size") == stat.st_size == record.get("size")
                          and previous.get("mtime_ns") == stat.st_mtime_ns)
                receipt = previous if cached else validate_bundle(existing, record)
                if existing != dest:
                    # The pipeline expects flat DL paths. A link avoids a duplicate
                    # large cache; replacing downloads never writes into the link.
                    temporary = dest.with_name(dest.name + ".linktmp")
                    temporary.unlink(missing_ok=True)
                    try:
                        os.link(existing, temporary)
                    except OSError:
                        shutil.copyfile(existing, temporary)
                    os.replace(temporary, dest)
                    receipt = {**receipt, "mtime_ns": dest.stat().st_mtime_ns}
                return name, receipt, False
            except Exception:
                pass
        request = urllib.request.Request(um.CDN + urllib.parse.quote(name, safe=''), headers={"User-Agent": "story-filler/1.0"})
        partial = dest.with_name(dest.name + ".part")
        try:
            for attempt in range(3):
                # The caller's progress callback also handles cooperative
                # cancellation. Never begin another retry after cancellation.
                self.notify(bundleName=name, receivedBytes=0, transferTotal=0)
                try:
                    with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as out:
                        received, last_update = 0, 0
                        total = int(record.get("size") or response.headers.get("Content-Length") or 0)
                        while chunk := response.read(256 * 1024):
                            out.write(chunk)
                            received += len(chunk)
                            now = time.monotonic()
                            if now - last_update >= .5 or received == total:
                                self.notify(bundleName=name, receivedBytes=received, transferTotal=total)
                                last_update = now
                    receipt = validate_bundle(partial, record)
                    os.replace(partial, dest)
                    print(f"  下载: {name} ({receipt['size']:,} B)")
                    return name, receipt, True
                except Exception:
                    self.notify()
                    if attempt == 2:
                        raise
                    time.sleep(attempt + 1)
        finally:
            partial.unlink(missing_ok=True)

    def ensure(self, names, phase="verifying"):
        todo = sorted(set(names) - self.ready)
        self.notify(phase=phase, resourceDone=0, resourceTotal=len(todo),
                    bundleName="", receivedBytes=0, transferTotal=0)
        errors = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self._ensure, name): name for name in todo}
            for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
                name = futures[future]
                try:
                    _, receipt, changed = future.result()
                    self.cache[name] = receipt
                    self.have[name] = self.dl / name
                    self.ready.add(name)
                    if changed:
                        self.changed.add(name)
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
                self.notify(resourceDone=index, bundleName=name, receivedBytes=0, transferTotal=0)
        save_state(BUNDLE_RECEIPTS, self.cache)
        if errors:
            raise RuntimeError("资源下载/校验失败: " + "; ".join(errors))


def initial_bundles(kind, story_id, index):
    names = {f"bundle_scenarioscript_{kind}_{story_id}"}
    names.update(n for n in index if n.startswith(f"bundle_voice_adv_{kind}_{story_id}_"))
    if kind == "char":
        names.update(dc.match_bundles(index, story_id, full=False))
    return names


def install_status(kind, story_id, require_runtime=True):
    folder = sp.directory(kind, story_id, ROOT)
    try:
        scenarios = json.loads((folder / "scenario.json").read_text(encoding="utf-8"))
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(scenarios, list) or not scenarios:
            return False, "剧本为空或损坏"
        from build_manifest import collect_refs
        refs = collect_refs(scenarios)
        for section, required in zip(("stands", "images", "audio"), refs):
            if manifest.get("missing", {}).get(section):
                return False, f"{section} 仍有未解析资源"
            for ref in required:
                if ref.lower() not in manifest.get(section, {}):
                    return False, f"{section} 引用未登记: {ref}"
        paths = list(manifest.get("images", {}).values()) + list(manifest.get("audio", {}).values())
        for layer in manifest.get("stands", {}).values():
            paths.extend(layer[key] for key in ("base", "expression", "full") if layer.get(key))
        for path in paths:
            candidates = (ROOT / path, ROOT.parent / path)
            if not any(p.is_file() and p.stat().st_size > 0 for p in candidates):
                return False, f"文件缺失或为空: {path}"
        files = ("scenario.js", "manifest.js", "runtime.js") if require_runtime else ("scenario.js", "manifest.js")
        for filename in files:
            if not (folder / filename).is_file() or (folder / filename).stat().st_size == 0:
                return False, filename + " 缺失"
        return True, "ok"
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return False, str(exc)


def create_info(kind, story_id):
    """Use captured original titles; keep existing names and translations intact."""
    if kind == "char":
        from scrape_wiki_info import update_profile
        return update_profile(story_id)
    folder = sp.directory(kind, story_id, ROOT)
    path = folder / "info.json"
    info = read_state(path)
    scenarios = json.loads((folder / "scenario.json").read_text(encoding="utf-8"))
    # Missing official titles use the browser's normal episode labels, rather
    # than exposing internal script filenames as chapter names.
    titles = [""] * len(scenarios)
    catalog_path = folder.parent / "title-catalog.json"
    captured = read_state(catalog_path)
    title = f"{LABELS[kind]} {story_id}"
    if kind == "event":
        event = captured.get("events", {}).get(str(story_id), {})
        title = event.get("title") or title
        titles = event.get("chapterTitles") or titles
    elif kind == "other":
        episodes = {int(e["value2"]): e for group in captured.get("groups", [])
                    for e in group.get("episodes", [])
                    if e.get("type") == "Other" and int(e.get("value1", -1)) == story_id}
        for index, scenario in enumerate(scenarios):
            match = re.match(r"adv_other_\d+_(\d+)", scenario.get("FileName", ""))
            if match:
                titles[index] = episodes.get(int(match[1]), {}).get("title", "")
    info.setdefault("id", story_id)
    info.setdefault("title", title)
    info.setdefault("chapterTitles", titles)
    save_state(path, info)


def run_pipeline(kind, story_id, names, store):
    folder = sp.directory(kind, story_id, ROOT)
    folder.mkdir(parents=True, exist_ok=True)
    core = ("scenario.json", "scenario.js", "manifest.json", "manifest.js", "runtime.js", "info.json")
    backup = {name: (folder / name).read_bytes() if (folder / name).exists() else None for name in core}
    image_names = [n for n in names if any(k in n for k in dc.IMAGE_BUNDLE_KEYS)]
    list_file = ROOT / "data" / "_story_export_list.tmp"
    try:
        store.notify(phase="extracting", resourceDone=0, resourceTotal=0,
                     bundleName="", receivedBytes=0, transferTotal=0)
        if image_names:
            list_file.write_text("\n".join(image_names), encoding="utf-8")
            export_args = ["-i", str(store.dl), "-o", str(ROOT / "images"), "--hex-case", "lower",
                           "--bundle-list", str(list_file)]
            if not set(image_names) & store.changed:
                export_args.append("--skip-existing")
            importlib.import_module("export")._EXISTING_INDEX.clear()
            dc._run_step_inproc("export", export_args)
            dc._run_step_inproc("sort_images", [])
        importlib.import_module("build_char387_web_data")._POOL_INDEX.clear()
        for module in ("extract_scenario", "extract_audio", "build_manifest"):
            store.notify(phase=module.replace("_", "-"))
            dc._run_step_inproc(module, ["--" + kind, str(story_id)])
        if kind == "char":
            store.notify(phase="profile", resourceDone=0, resourceTotal=1)
        profile = create_info(kind, story_id)
        store.profile_warning = (profile or {}).get("warning")
        if kind == "char":
            store.notify(phase="profile", resourceDone=1, resourceTotal=1)
        # Validate resources before publishing into the browser's catalog.
        ok, reason = install_status(kind, story_id, require_runtime=False)
        if not ok:
            raise RuntimeError("剧情未补齐: " + reason)
        if kind == "char":
            store.notify(phase="build-catalog")
            dc._run_step_inproc("build_catalog", ["--char", str(story_id), "--quiet"])
            dc.mark_installed(story_id)
        else:
            import build_story_catalog
            build_story_catalog.build_kind(kind)
        return install_status(kind, story_id)
    except BaseException:
        # An unsuccessful update must not replace a previously playable script.
        for name, content in backup.items():
            path = folder / name
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise
    finally:
        list_file.unlink(missing_ok=True)


def fill(kinds, ids=None, records=None, *, verify_bundles=False, progress=None):
    receipts = read_state(RECEIPTS)
    records = um.fetch_manifest_records() if records is None else records
    selected = plan(records, kinds, ids)
    if not selected:
        raise ValueError("官方目录中没有符合选择的剧情")
    um.merge_latest_urls(records)
    store = BundleStore(records, progress)
    report = {"selected": list(kinds), "results": []}
    for count, (kind, story_id, script) in enumerate(selected, 1):
        store.profile_warning = None
        key = sp.data_key(kind, story_id)
        print(f"\n[{count}/{len(selected)}] {LABELS[kind]} {story_id}", flush=True)
        try:
            previous = receipts.get(key, {})
            fingerprint = previous.get("bundles", {})
            up_to_date = (previous.get("pipeline_version") == PIPELINE_VERSION
                          and script in fingerprint
                          and initial_bundles(kind, story_id, records) <= fingerprint.keys()
                          and all(n in records and records[n]["hash"] == h for n, h in fingerprint.items()))
            # Explicit repair also checks the original bundles used by the
            # native player, even when exported browser resources are complete.
            if up_to_date and verify_bundles:
                store.ensure(fingerprint)
            if up_to_date and install_status(kind, story_id)[0]:
                if kind == "char":
                    store.notify(phase="profile", resourceDone=0, resourceTotal=1,
                                 bundleName="", receivedBytes=0, transferTotal=0)
                    profile = create_info(kind, story_id)
                    store.profile_warning = (profile or {}).get("warning")
                    store.notify(phase="profile", resourceDone=1, resourceTotal=1)
                    dc._run_step_inproc("build_catalog", ["--char", str(story_id), "--quiet"])
                state = "skip"
                print("  已完整且与官方目录一致，跳过")
            else:
                names = initial_bundles(kind, story_id, records)
                store.ensure(names, phase="base-resources")
                store.notify(phase="dependencies", resourceDone=0, resourceTotal=0,
                             bundleName="", receivedBytes=0, transferTotal=0)
                dependencies, unknown = dc.scenario_bundle_deps(store.have[script], records)
                if unknown:
                    raise RuntimeError("官方目录缺少引用资源: " + ", ".join(unknown))
                names.update(dependencies)
                store.ensure(names, phase="story-resources")
                ok, reason = run_pipeline(kind, story_id, sorted(names), store)
                if not ok:
                    raise RuntimeError(reason)
                receipts[key] = {"pipeline_version": PIPELINE_VERSION,
                                 "bundles": {n: records[n]["hash"] for n in sorted(names)}}
                save_state(RECEIPTS, receipts)
                state = "ok"
                print("  已补齐并加入播放器")
            row = {"kind": kind, "id": story_id, "state": state}
            if getattr(store, "profile_warning", None):
                row["profileWarning"] = store.profile_warning
            report["results"].append(row)
        except Exception as exc:
            print(f"  未完成: {exc}")
            report["results"].append({"kind": kind, "id": story_id, "state": "fail", "reason": str(exc)})
        save_state(REPORT, report)
    print("\n" + "=" * 46 + "\n  补齐结果")
    for kind in kinds:
        rows = [r for r in report["results"] if r["kind"] == kind]
        counts = {state: sum(r["state"] == state for r in rows) for state in ("ok", "skip", "fail")}
        print(f"  {LABELS[kind]}: 新补齐 {counts['ok']}，已完整 {counts['skip']}，未完成 {counts['fail']}")
    print(f"  记录: {REPORT}\n  再次选择同一项会跳过已完成的剧情，重试未完成项。")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=[*sp.KINDS, "all"], required=True)
    parser.add_argument("--ids", help="只补齐指定 ID，例如 61 或 92-95")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    report = fill(sp.KINDS if args.kind == "all" else (args.kind,),
                  set(dc.parse_ids(args.ids)) if args.ids else None)
    if any(r["state"] == "fail" for r in report["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
