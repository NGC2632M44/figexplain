"""figexplain - 配置与依赖检查（仅用标准库）。"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".figexplain" / "config.json"

DEFAULT_CONFIG = {
    "openai_base_url": "https://api.openai.com",
    "openai_api_key": "",
    "openai_model": "gpt-4o",
    # Default Zotero storage location resolved at runtime from the user's home
    # dir, so no machine-specific path is baked into the source.
    "zotero_storage_dir": str(Path.home() / "Zotero" / "storage"),
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        return merged
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def interactive_config() -> dict:
    """每次运行交互式输入 base_url / key / model / storage dir，存档。

    预填当前保存值，直接回车保留。
    """
    cfg = load_config()
    print("\n===== figexplain 配置（直接回车保留当前值）=====")
    def ask(key, label, secret=False):
        cur = cfg.get(key, "")
        shown = ("*" * len(cur)) if (secret and cur) else cur
        val = input(f"{label} [{shown}]: ").strip()
        if val:
            cfg[key] = val
    ask("openai_base_url", "OpenAI 兼容 base_url")
    ask("openai_api_key", "API key", secret=True)
    ask("openai_model", "模型名（需支持 vision，如 gpt-4o / qwen-vl-max）")
    ask("zotero_storage_dir", "Zotero storage 目录路径")
    save_config(cfg)
    print(f"已保存到 {CONFIG_PATH}\n")
    return cfg


def ensure_deps() -> None:
    missing = []
    try:
        import fitz  # noqa: F401
    except Exception:
        missing.append("PyMuPDF")
    try:
        import PIL  # noqa: F401
    except Exception:
        missing.append("Pillow")
    try:
        import requests  # noqa: F401
    except Exception:
        missing.append("requests")
    if missing:
        print("缺少依赖:", ", ".join(missing))
        print("请运行: pip install -r requirements.txt")
        sys.exit(1)
