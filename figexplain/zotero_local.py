"""Zotero 本地 HTTP API (127.0.0.1:23119) 客户端。

能力（经实测确认）：
  - GET  /api/users/0/collections/{id}/items/top   列分类下的条目
  - POST /connector/getSelectedCollection          取当前选中分类
  - GET  /api/users/0/items/{key}                   条目详情
  - GET  /api/users/0/items/{key}/children          子附件/笔记
  - POST /connector/saveItems                        创建顶层 note（写入当前 save target）
限制（实测）：
  - 无 "getSelectedItems" 端点；用 getSelectedCollection + 列分类条目 + 序号选择 代替
  - saveItems 忽略 parentItem，无法创建挂在父文献下的子笔记；note 写成顶层
  - DELETE/PATCH/PUT 一律 501，不可用
"""
from __future__ import annotations
import json
import urllib.error
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:23119"
TIMEOUT = 15


class ZoteroLocalError(RuntimeError):
    pass


def _request(method: str, path: str, body: Any = None, ctype: str = "application/json") -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise ZoteroLocalError(f"无法连接 Zotero 本地服务 ({BASE})。请确认 Zotero 已启动。原因: {e}") from e
    except Exception as e:
        raise ZoteroLocalError(f"请求 {method} {path} 失败: {e}") from e


def ping() -> bool:
    """Zotero 是否在运行。"""
    try:
        s, _ = _request("GET", "/connector/ping")
    except ZoteroLocalError:
        return False
    return s == 200


def get_selected_collection() -> dict:
    """返回当前选中的分类 {id, name, libraryID, ...}。id 为 int。"""
    s, t = _request("POST", "/connector/getSelectedCollection", {})
    if s != 200:
        raise ZoteroLocalError(f"getSelectedCollection 失败 ({s}): {t[:120]}")
    return json.loads(t)


def list_articles(collection_id: int) -> list[dict]:
    """列出分类下的 journalArticle 顶层条目（key/title/ creators/ date）。"""
    url = f"/api/users/0/collections/{collection_id}/items/top?format=json&limit=200&itemType=journalArticle"
    s, t = _request("GET", url)
    if s != 200:
        raise ZoteroLocalError(f"列分类条目失败 ({s}): {t[:120]}")
    items = json.loads(t)
    out = []
    for it in items:
        d = it["data"]
        authors = ", ".join(c.get("lastName", "") for c in d.get("creators", [])[:3])
        out.append({
            "key": it["key"],
            "title": d.get("title", ""),
            "date": d.get("date", ""),
            "authors": authors,
            "doi": d.get("DOI", ""),
            "itemType": d.get("itemType", ""),
        })
    return out


def get_item(key: str) -> dict:
    s, t = _request("GET", f"/api/users/0/items/{key}?format=json")
    if s != 200:
        raise ZoteroLocalError(f"读取条目 {key} 失败 ({s}): {t[:120]}")
    return json.loads(t)["data"]


def get_children(key: str) -> list[dict]:
    s, t = _request("GET", f"/api/users/0/items/{key}/children?format=json")
    if s != 200:
        raise ZoteroLocalError(f"读取子项 {key} 失败 ({s}): {t[:120]}")
    return [c["data"] for c in json.loads(t)]


def find_pdf_attachment(parent_key: str) -> dict | None:
    """从父条目子项里找 PDF 附件（imported_url / imported_file 均可）。"""
    for c in get_children(parent_key):
        if c.get("itemType") == "attachment" and c.get("contentType") == "application/pdf":
            return c
    return None


def resolve_pdf_path(attachment_key: str, filename: str, storage_dir: str) -> str:
    """拼 storage 目录路径。用正斜杠，跨平台。"""
    import os
    # Zotero storage: <storage_dir>/<KEY>/<filename>
    # KEY 大写 8 字符
    return os.path.join(storage_dir, attachment_key, filename).replace("\\", "/")


def create_note(note_html: str, tags: list[str] | None = None) -> bool:
    """创建顶层 note，写入 Zotero 当前 save target（通常是当前选中分类）。

    saveItems 返回 201 且 body 为空，无法拿到新 note 的 key；成功以状态码判断。
    """
    tag_objs = [{"tag": tg} for tg in (tags or [])]
    payload = {
        "items": [{
            "itemType": "note",
            "note": note_html,
            "tags": tag_objs,
        }],
    }
    s, t = _request("POST", "/connector/saveItems", payload)
    if s != 201:
        raise ZoteroLocalError(f"创建笔记失败 ({s}): {t[:200]}")
    return True
