"""Explicit, non-destructive installer for an existing AstrBot checkout."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from companion import core


def main():
    parser = argparse.ArgumentParser(description="Copy the companion plugin into an installed AstrBot")
    parser.add_argument("--astrbot-root", required=True, type=Path)
    args = parser.parse_args()
    target = args.astrbot_root.resolve()
    if not target.is_dir():
        parser.error(f"AstrBot directory not found: {target}")
    plugins = target / "data" / "plugins"
    if not plugins.parent.is_dir():
        parser.error(f"Unexpected AstrBot layout, no data directory: {plugins.parent}")
    plugins.mkdir(parents=True, exist_ok=True)
    destination = plugins / "astrbot_plugin_qq_gpt_companion"
    if destination.exists():
        parser.error(f"Plugin exists, refusing to overwrite: {destination}")
    source = core.ROOT / "astrbot_plugin_qq_gpt_companion"
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"Copied plugin into: {destination}")
    print("Open AstrBot WebUI, configure local bridge token and allowed QQ groups.")
    print("No credentials or QQ session were modified.")


if __name__ == "__main__":
    main()
