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


def create_note(note_html: str, tags: list[str] | None = None,
                parent_item_key: str | None = None) -> dict:
    """创建 note 并返回信息。

    Zotero 本地 HTTP API (127.0.0.1:23119) 限制（实测 Zotero 7）：
      - DELETE/PATCH/PUT 一律 501，无法事后把顶层 note 改成子笔记
      - /api/users/0/items POST 不支持（"Endpoint does not support method"）
      - /connector/saveItems 接受 note，但忽略 item 内的 parentItem 字段，
        写入的是「当前 save target」（默认 = 选中的分类），无法直接生成
        挂在父文献下的子笔记

    因此策略：
      1. 优先尝试把 parentItem 同时放进 item 内和 payload 顶层（对部分
         Zotero 版本可能生效），写入后校验是否真成了子项
      2. 不行就回退为顶层 note，并在返回里给出 message，由调用方决定
         是否在 note 顶部加「请手动拖入对应文献」的提示

    返回: {"key": None, "parented": bool, "message": str}
    """
    tag_objs = [{"tag": tg} for tg in (tags or [])]
    base_item = {
        "itemType": "note",
        "note": note_html,
        "tags": tag_objs,
    }

    if parent_item_key:
        payload = {
            "items": [dict(base_item, parentItem=parent_item_key)],
            "parentItem": parent_item_key,
        }
        try:
            s, t = _request("POST", "/connector/saveItems", payload)
            if s == 201:
                import time as _t
                _t.sleep(1.0)
                if _note_is_child(parent_item_key, tags or []):
                    return {"key": None, "parented": True,
                            "message": "已作为子笔记写入父条目下"}
        except ZoteroLocalError:
            pass

    payload = {"items": [base_item]}
    s, t = _request("POST", "/connector/saveItems", payload)
    if s != 201:
        raise ZoteroLocalError(f"创建笔记失败 ({s}): {t[:200]}")
    return {"key": None, "parented": False,
            "message": "已写入为顶层笔记（Zotero 本地 API 无法直接挂到父条目下，请在 Zotero 中手动拖入）"}


def _note_is_child(parent_key: str, tags: list[str]) -> bool:
    """检查刚创建的 note 是否作为子项出现在 parent_key 下。"""
    try:
        s, t = _request("GET", f"/api/users/0/items/{parent_key}/children?format=json")
        if s != 200:
            return False
        children = json.loads(t)
        tag_set = set(tags)
        for c in children:
            d = c["data"]
            if d.get("itemType") == "note" and tag_set.issubset(
                    {x.get("tag") for x in d.get("tags", [])}):
                return True
    except Exception:
        pass
    return False
