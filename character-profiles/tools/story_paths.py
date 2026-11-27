"""Shared paths and IDs for the four official story families."""
import sys
from pathlib import Path

from player_paths import data_root
ROOT = data_root()
FOLDERS = {"event": "events", "main": "main", "other": "other"}
KINDS = ("main", "event", "other", "char")


def directory(kind, story_id, root=None):
    root = Path(root) if root is not None else ROOT
    if kind == "char":
        return root / "characters" / str(int(story_id))
    return root / "stories" / FOLDERS[kind] / str(int(story_id))


def data_key(kind, story_id):
    return str(int(story_id)) if kind == "char" else f"{kind}:{int(story_id)}"


def add_arguments(group):
    for kind in KINDS:
        group.add_argument("--" + kind, type=int)


def selection(args, default=387):
    for kind in KINDS:
        if getattr(args, kind) is not None:
            return kind, getattr(args, kind)
    return "char", default
