"""Portable settings: all writable application data stays beside the launcher."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    root = Path(os.environ.get("JSR_EASY_DATA_DIR", application_root() / "UserData"))
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def load_settings() -> dict:
    try:
        obj = json.loads((data_root() / "settings.json").read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, obj: dict) -> None:
    """Atomic replacement; avoid truncating a saved configuration on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_settings(settings: dict) -> None:
    write_json(data_root() / "settings.json", settings)
