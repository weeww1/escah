"""Shared data root for the CLI and the player's bundled worker."""
import sys
from pathlib import Path


def data_root():
    # Set only by player_worker before importing the existing pipeline. Renderer
    # requests cannot supply paths; the desktop main process pins this directory.
    override = getattr(sys, "_choukou_player_root", None)
    if override is not None:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundle_directory():
    root = data_root()
    if getattr(sys, "_choukou_player_root", None) is not None or (root / "DL").is_dir():
        return root / "DL"
    return root.parent / "DL"
