"""Extract the OFFICIAL ADV scenario JSON from a scenarioscript bundle, verbatim.

This does ZERO transformation: it just pulls every TextAsset that is an ADV
scenario (has a "Methods" array) out of `DL/bundle_scenarioscript_char_<id>` and
writes them, sorted by FileName, as a plain array to `characters/<id>/scenario.json`.

The player consumes this official format directly (it switches on each method's
`Method` field). The only per-character resolution work — turning the encoded
resource names inside the Methods into real file paths + stand geometry — lives
entirely in build_manifest.py, NOT here. So a newly decrypted scenario for an
already-built character is "drop in and play": run this one command, done.
"""

import argparse
import story_paths
import json
import sys
from pathlib import Path

import UnityPy

from player_paths import data_root, bundle_directory
ROOT = data_root()
WORKSPACE = ROOT.parent
DL = bundle_directory()


def read_scenarios(scenario_bundle):
    env = UnityPy.load(str(scenario_bundle))
    assets = []
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        raw = data.m_Script
        text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        # An ADV scenario asset is the object carrying the Methods command list.
        if isinstance(parsed, dict) and "Methods" in parsed:
            assets.append(parsed)
    return sorted(assets, key=lambda item: item.get("FileName", ""))


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    story_paths.add_arguments(group)
    args = parser.parse_args()
    kind, story_id = story_paths.selection(args, 387)

    bundle_name = f"bundle_scenarioscript_{kind}_{story_id}"
    bundle = DL / bundle_name
    if not bundle.exists():  # 历史下载有些剧本 bundle 放在 DL/ss/
        bundle = DL / "ss" / bundle_name
    if not bundle.exists():
        sys.exit(f"scenario bundle not found: {bundle_name}")
    out = story_paths.directory(kind, story_id, ROOT) / "scenario.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    scenarios = read_scenarios(bundle)
    if not scenarios:
        raise RuntimeError(f"empty scenario bundle: {bundle_name}")
    payload = json.dumps(scenarios, ensure_ascii=False, separators=(",", ":"))
    out.write_text(payload, encoding="utf-8")
    # .js 版:挂 window.SCENARIO_DATA,供 file:// 双击直开(播放器优先 <script> 加载)
    data_key = story_paths.data_key(kind, story_id)
    out.with_suffix(".js").write_text(
        f'(window.SCENARIO_DATA=window.SCENARIO_DATA||{{}})["{data_key}"]={payload};',
        encoding="utf-8")

    methods = sum(len(s.get("Methods", [])) for s in scenarios)
    print(f"wrote {out}")
    print(f"scenarios={len(scenarios)} methods={methods}")


if __name__ == "__main__":
    main()
