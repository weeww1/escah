"""Image-only updater. No scenario, voice, music or story installation calls.

The desktop shell starts this worker on demand and exchanges JSON lines. Checking
only fetches the two official catalogs; downloading requires a separate action.
"""
import contextlib
import datetime
import json
import os
import re
import queue
import sys
import threading
import time
import urllib.request
from pathlib import Path

import update_manifest as um

ROOT = um.ROOT
GROUPS = {
    "stands": {"standimage", "standimages", "standfullimage", "profileimages"},
    "pictures": {"bgimage", "eventimage", "home2bgimages", "tutorialimage"},
    "icons": {"episodeicons", "miniicons", "names", "usericon", "equipcharactericon",
              "home2charathumbnails", "home2bgthumbnails", "eventthums", "voteicons",
              "imagessubdivision", "itemimagesubdivision", "itemimages", "image",
              "gallerymusicjackets"},
}
NAME_PATTERN = re.compile(r"bundle_[A-Za-z0-9_-]+\Z")
VERSION = 1


def image_group(name):
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        return None
    if name.startswith("bundle_chara_c-panel_"):
        return "icons"
    kind = name.split("_", 2)[1]
    return next((group for group, kinds in GROUPS.items() if kind in kinds), None)


def local_path(root, relative):
    """Receipts and bundle asset names cannot escape the installation folder."""
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("图片路径无效")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("图片路径超出播放器目录")
    return path


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class ImageUpdater:
    def __init__(self, root=ROOT, notify=None):
        self.root = Path(root)
        self.receipt_path = self.root / "data" / "image_installed.json"
        self.geometry_path = self.root / "data" / "stand_infos.json"
        self.receipts = read_json(self.receipt_path)
        self.records = None
        self.checked_at = None
        self.notify = notify or (lambda value: None)
        self.image_index = None
        self.geometry = read_json(self.geometry_path)

    def installed(self, name, record):
        receipt = self.receipts.get(name, {})
        if receipt.get("version") != VERSION or receipt.get("hash") != record["hash"]:
            return False
        files = receipt.get("files")
        if not isinstance(files, dict) or not files:
            return False
        try:
            for relative, size in files.items():
                path = local_path(self.root, relative)
                if not path.is_file() or path.stat().st_size != size or size <= 0:
                    return False
            if receipt.get("geometry"):
                if any(self.geometry.get(key) != value for key, value in receipt["geometry"].items()):
                    return False
            return True
        except (OSError, ValueError, TypeError):
            return False

    def plan(self):
        groups = {key: {"total": 0, "pending": 0, "changed": 0, "repair": 0,
                        "unverified": 0, "bytes": 0} for key in GROUPS}
        for name, record in self.records.items():
            group = image_group(name)
            if not group:
                continue
            stats = groups[group]
            stats["total"] += 1
            if self.installed(name, record):
                continue
            stats["pending"] += 1
            stats["bytes"] += max(0, int(record.get("size") or 0))
            previous = self.receipts.get(name)
            status = ("unverified" if not previous else
                      "changed" if previous.get("hash") != record["hash"] else "repair")
            stats[status] += 1
        return {"groups": groups, "checkedAt": self.checked_at,
                "pending": sum(g["pending"] for g in groups.values()),
                "bytes": sum(g["bytes"] for g in groups.values())}

    def check(self):
        self.records = None
        self.notify({"phase": "checking", "message": "正在读取官方图片目录…"})
        # Commit to memory only after both halves succeed. Checking does not
        # create downloaded bundles, modify URL baselines or mark images installed.
        records = um.fetch_manifest_records()
        if not any(image_group(n) for n in records):
            raise ValueError("官方目录中没有可识别的图片资源")
        for name, record in records.items():
            if image_group(name):
                if not record.get("hash") or int(record.get("size") or 0) <= 0 or record.get("crc") is None:
                    raise ValueError("官方图片目录不完整，请稍后重试")
        self.records = records
        self.checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return self.plan()

    def ensure_bundle(self, name, record):
        if not image_group(name):
            raise ValueError("禁止下载非图片资源")
        from fill_stories import validate_bundle
        dest = local_path(self.root, "DL/" + name)
        # Reuse current or historical caches only after official size/CRC checks.
        for candidate in (dest, self.root.parent / "DL" / name):
            try:
                validate_bundle(candidate, record)
                return candidate
            except Exception:
                pass
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".image-part")
        request = urllib.request.Request(um.CDN + name, headers={"User-Agent": "image-updater/1.0"})
        try:
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(request, timeout=20) as response, partial.open("wb") as out:
                        received, last_update = 0, 0
                        while chunk := response.read(256 * 1024):
                            received += len(chunk)
                            if received > int(record["size"]):
                                raise ValueError("资源大小超出官方记录")
                            out.write(chunk)
                            now = time.monotonic()
                            if now - last_update >= .4 or received == int(record["size"]):
                                self.notify({"receivedBytes": received, "transferTotal": int(record["size"])})
                                last_update = now
                    validate_bundle(partial, record)
                    os.replace(partial, dest)
                    return dest
                except Exception:
                    if attempt:
                        raise
                    time.sleep(0.5)
        finally:
            partial.unlink(missing_ok=True)

    def export_bundle(self, name, bundle):
        import UnityPy
        import export as image_export
        from sort_images import route

        if self.image_index is None:
            self.image_index = {p.name.lower(): p for p in sorted((self.root / "images").rglob("*.png"))}
        env = UnityPy.load(str(bundle))
        containers = {obj.path_id: key for key, obj in env.container.items()}
        used = set()
        files, geometry = {}, {}
        for obj in env.objects:
            if obj.type.name == "TextAsset" and name.startswith("bundle_standimage_"):
                data = obj.read()
                text = data.m_Script
                if not isinstance(text, str):
                    text = bytes(text).decode("utf-8")
                if "ExpressionCutRectLeft" in text:
                    info = json.loads(text)
                    group = info.get("ImageGroup", {})
                    base = str(group.get("BaseImage", "")).lower()
                    if base:
                        geo = {key: int(info.get(field, 0)) for key, field in (
                            ("alphaX", "AlphaCutRectLeft"), ("alphaY", "AlphaCutRectTop"),
                            ("alphaWidth", "AlphaCutRectWidth"), ("alphaHeight", "AlphaCutRectHeight"),
                            ("x", "ExpressionCutRectLeft"), ("y", "ExpressionCutRectTop"),
                            ("width", "ExpressionCutRectWidth"), ("height", "ExpressionCutRectHeight"))}
                        geometry["_".join(base.split("_")[:3])] = {"baseRef": base, "expressionRef": "", **geo}
                        for expr in group.get("ExpressionImages", []):
                            geometry[expr.lower()] = {"baseRef": base, "expressionRef": expr.lower(), **geo}
                continue
            if obj.type.name != "Texture2D":
                continue
            texture = obj.read()
            image = texture.image
            if image is None:
                raise ValueError("图片解包失败")
            stem = image_export.pick_best_name(containers.get(obj.path_id) or texture.m_Name, "lower")
            stem = image_export.safe_name(stem)
            if not stem:
                raise ValueError("图片没有有效文件名")
            original, suffix = stem, 2
            while stem.lower() in used:
                stem = f"{original}_{suffix}"
                suffix += 1
            used.add(stem.lower())
            filename = stem + ".png"
            if name.startswith("bundle_episodeicons_episode_character_"):
                relative = "icons/episode/" + filename
            else:
                known = self.image_index.get(filename.lower())
                relative = known.relative_to(self.root).as_posix() if known else f"images/{route(filename)}/{filename}"
            path = local_path(self.root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".image-part")
            try:
                image.save(partial, format="PNG", compress_level=3)
                os.replace(partial, path)
            finally:
                partial.unlink(missing_ok=True)
            files[relative] = path.stat().st_size
            self.image_index[filename.lower()] = path
        if not files:
            raise ValueError("图片资源中没有可导出的 Texture2D，未记为完成")
        if geometry:
            self.geometry.update(geometry)
            um.atomic_write_text(self.geometry_path, json.dumps(self.geometry, ensure_ascii=False, separators=(",", ":")))
        return files, geometry

    def refresh_catalog(self):
        """Refresh image/gallery metadata, preserving all existing story fields.

        New characters can be browsed without creating/downloading scenarios.
        Existing scenario and manifest files are never rewritten here.
        """
        import build_catalog as catalog
        catalog.STAND_INFOS = read_json(self.geometry_path)
        entries = {int(e["id"]): e for e in catalog.load_catalog_data()}
        ids = set(entries)
        for name in self.records:
            match = re.fullmatch(r"bundle_(?:episodeicons_episode_character|chara_c-panel)_(\d+)", name)
            if match:
                ids.add(int(match[1]))
        for cid in sorted(ids):
            forms = catalog.collect_forms(cid)
            full = catalog.full_stand_image(cid)
            if not forms or not full:
                continue
            old = self.root / "characters" / str(cid) / "runtime.js"
            runtime = {}
            if old.exists():
                text = old.read_text(encoding="utf-8")
                marker = text.find("]=")
                if marker >= 0:
                    runtime, _ = json.JSONDecoder().raw_decode(text[marker + 2:].lstrip())
            entry = {**entries.get(cid, {}), **runtime, "id": cid, "forms": forms}
            entry["stand"] = catalog.rel(full)
            info = catalog.load_info(cid)
            entry.setdefault("name", info.get("name") or f"角色 {cid}")
            entry.setdefault("chapters", 0)
            default = next((f for f in forms if f.get("default")), forms[0])
            entry["base"] = default["base"]
            entry["expressions"] = [e["src"] for e in default["expressions"]]
            if default.get("box"):
                entry["box"] = default["box"]
            for key, candidates in {
                "panel": [f"images/panels/chara_c-panel_{cid}.png", f"images/panels/chara_c-panel_{cid:03d}.png"],
                "mini": [f"images/panels/character_mini_{cid:05d}.png"],
                "stand": [f"images/stands/stand_{cid:05d}.png"],
                "nameImgFull": [f"images/names/name_{cid:05d}.png"],
            }.items():
                found = next((p for p in candidates if (self.root / p).is_file()), None)
                if found:
                    entry[key] = found
            crop = catalog.nameplate_card_crop(cid)
            if crop:
                entry["nameImg"] = crop
            old.parent.mkdir(parents=True, exist_ok=True)
            catalog.write_runtime_script(cid, entry)
            entries[cid] = catalog.catalog_entry(entry)
        catalog.write_catalog_data([entries[cid] for cid in sorted(entries)])

    def download(self, groups, names=None):
        if self.records is None:
            raise ValueError("请先检测更新")
        if not isinstance(groups, list) or not groups or any(g not in GROUPS for g in groups):
            raise ValueError("请选择有效的图片分类")
        todo = [(name, record) for name, record in self.records.items()
                if image_group(name) in groups and (names is None or name in names)
                and not self.installed(name, record)]
        failures = []
        completed = 0
        try:
            for count, (name, record) in enumerate(todo, 1):
                self.notify({"phase": "downloading", "completed": count - 1, "total": len(todo),
                             "failed": len(failures), "message": "下载并校验图片资源"})
                try:
                    bundle = self.ensure_bundle(name, record)
                    self.notify({"phase": "extracting", "completed": count - 1, "total": len(todo),
                                 "failed": len(failures), "message": "正在解包图片"})
                    files, geometry = self.export_bundle(name, bundle)
                    self.receipts[name] = {"version": VERSION, "hash": record["hash"],
                                           "files": files, "geometry": geometry}
                    completed += 1
                except Exception as exc:
                    failures.append({"name": name, "error": str(exc)})
                if count % 10 == 0:
                    self.save_receipts()
                self.notify({"phase": "downloading", "completed": count, "total": len(todo),
                             "failed": len(failures), "message": "正在补齐图片"})
        finally:
            self.save_receipts()
        self.notify({"phase": "catalog", "message": "正在刷新本地立绘目录…"})
        self.refresh_catalog()
        result = {"completed": completed, "failed": len(failures), "plan": self.plan()}
        um.atomic_write_text(self.root / "data" / "image_update_report.json",
                             json.dumps({**result, "failures": failures}, ensure_ascii=False, indent=2))
        return result

    def save_receipts(self):
        um.atomic_write_text(self.receipt_path, json.dumps(self.receipts, ensure_ascii=False, separators=(",", ":")))


def serve():
    """Only this stdout stream is protocol; dependency printouts go to stderr."""
    # On Windows NumPy's DLL initialization can deadlock against a second thread
    # blocked in stdin.read (numpy/numpy#24290). UnityPy imports it indirectly even
    # for Texture2D images. Initialize before starting the pipe reader below.
    import numpy  # noqa: F401
    output = sys.stdout
    output.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stdin.reconfigure(encoding="utf-8")
    emit = lambda value: print(json.dumps(value, ensure_ascii=False), file=output, flush=True)
    from resource_catalog import ResourceUpdater
    updater = ResourceUpdater(notify=lambda info: emit({"type": "progress", **info}))
    messages = queue.Queue()
    disconnected, active = threading.Event(), threading.Event()

    def read_requests():
        for line in sys.stdin:
            messages.put(line)
        disconnected.set()
        messages.put(None)

    def stop_if_parent_exits():
        disconnected.wait()
        # A launcher being closed must not leave a detached bulk download alive.
        # Grace allows a final small ping/result to flush when piping input.
        time.sleep(0.2)
        if active.is_set():
            os._exit(0)

    threading.Thread(target=read_requests, daemon=True).start()
    threading.Thread(target=stop_if_parent_exits, daemon=True).start()
    with contextlib.redirect_stdout(sys.stderr):
        while (line := messages.get()) is not None:
            request = {}
            try:
                request = json.loads(line)
                action = request.get("action")
                if action in ("check", "download", "catalog", "target-check", "target-download"):
                    if disconnected.is_set():
                        raise ValueError("播放器连接已关闭")
                    active.set()
                if action == "catalog":
                    result = updater.refresh_lists(request.get("groups"))
                elif action == "target-check":
                    result = updater.target_check(request.get("groups"))
                elif action == "target-download":
                    result = updater.target_download(request.get("groups"))
                elif action == "check":
                    # An unsuccessful new check invalidates the previous plan.
                    updater.records = None
                    result = updater.check()
                elif action == "download":
                    result = updater.download(request.get("groups"))
                elif action == "ping":
                    result = {"version": 2, "catalogs": True, "perTargetDownload": True, "localService": False}
                else:
                    raise ValueError("未知操作")
                emit({"type": "result", "id": request.get("id"), "value": result})
            except Exception as exc:
                emit({"type": "error", "id": request.get("id"), "message": str(exc)})
            finally:
                active.clear()


if __name__ == "__main__":
    serve()
