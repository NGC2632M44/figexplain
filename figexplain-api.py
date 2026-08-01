"""figexplain 服务化 API(零依赖,标准库 http.server)。

供 dockercc 容器(或任意客户端)调用:
  POST /explain  {"item_key": "4IX5ZWDT"}   -> 启动解读任务,返回 job_id
  GET  /jobs/<job_id>                        -> 轮询状态/结果
  GET  /health

设计:
  - 任务在后台线程跑(一篇 10 图约 15-25 分钟),job 状态存内存
  - LLM 调用直连(见 llm_explain._chat 的 proxies=None),不走代理 env
  - 复用 figexplain 包全流程:提取 -> 逐图解读 -> 综合分析 -> 写 Zotero 笔记
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import uuid
import http.server

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from figexplain import config as cfg
from figexplain import zotero_local as zl
from figexplain import pdf_extract as pe
from figexplain import llm_explain as llm
from figexplain import note_writer as nw
from run import resolve_pdf

PORT = 8788
JOBS: dict[str, dict] = {}


def _article_from_key(key: str) -> dict:
    # Zotero 容错:API 不可达时退化为最小条目,PDF 走 storage 目录列举兜底
    d = {}
    try:
        d = zl.get_item(key)
    except Exception:
        pass
    if d.get("itemType") == "attachment" and d.get("parentItem"):
        try:
            p = zl.get_item(d["parentItem"])
            if p.get("itemType") in ("journalArticle", "preprint", "bookSection", "thesis", "conferencePaper"):
                d = p
        except Exception:
            pass
    return {
        "key": d.get("key", key),
        "title": d.get("title", "") or "(Zotero 未连接)",
        "date": d.get("date", ""),
        "authors": ", ".join(c.get("lastName", "") for c in d.get("creators", [])[:3]),
        "doi": d.get("DOI", ""),
    }


def _run_pipeline(job: dict, item_key: str) -> None:
    """后台执行全流程,结果写进 job dict。"""
    log = job["log"]
    def say(msg: str) -> None:
        log.append(msg)
        job["log_tail"] = msg
    try:
        settings = cfg.load_config()
        article = _article_from_key(item_key)
        say(f"选定文献:{article['title']}")
        storage_dir = settings.get("zotero_storage_dir") or os.path.join(
            os.path.expanduser("~"), "Zotero", "storage")
        pdf_path = resolve_pdf(article["key"], storage_dir)
        if not pdf_path or not os.path.exists(pdf_path):
            job["status"] = "failed"
            job["error"] = f"找不到 PDF:{pdf_path}"
            return
        say(f"PDF:{os.path.basename(pdf_path)}")

        say("[1/4] 提取图表与文本…")
        figures = pe.extract_figures(pdf_path)
        if not figures:
            job["status"] = "failed"
            job["error"] = "未提取到任何图表(caption)"
            return
        full_text = pe.fulltext(pdf_path)
        abstract, intro = pe.abstract_and_intro(full_text)
        say(f"  图 {len(figures)} 张 | 摘要 {len(abstract)} 字 | 引言 {len(intro)} 字")

        e_base = settings.get("explain_base_url") or settings["openai_base_url"]
        e_key = settings.get("explain_api_key") or settings["openai_api_key"]
        e_model = settings.get("explain_model") or settings.get("openai_model") or "gpt-4o"
        say("[2/4] 逐图解读…")
        figure_explanations = []
        for i, fig in enumerate(figures):
            try:
                fe = llm.explain_figure(fig, e_base, e_key, e_model)
                fe["index"] = fig.index
                np_ = len(fe.get("panels", []))
                say(f"  Fig {fig.index} OK ({np_} panels)")
            except Exception as e:
                say(f"  Fig {fig.index} 失败:{e}")
                fe = {"index": fig.index, "panels": [], "gist_zh": f"(解读失败:{e})", "logical_role": ""}
            figure_explanations.append(fe)
            job["figures_done"] = i + 1

        s_base = settings.get("synthesize_base_url") or settings["openai_base_url"]
        s_key = settings.get("synthesize_api_key") or settings["openai_api_key"]
        s_model = settings.get("synthesize_model") or settings.get("openai_model") or "gpt-4o"
        say("[3/4] 综合分析…")
        try:
            synthesis = llm.synthesize(figure_explanations, abstract, intro, full_text, s_base, s_key, s_model)
        except Exception as e:
            synthesis = {"figure_logical_structure": "", "differences": [], "key_points": [], "_raw": str(e)}
        job["synthesis"] = synthesis

        say("[4/4] 写 Zotero 笔记…")
        note_result = {"key": None, "parented": False, "message": ""}
        try:
            note_result = nw.write_note(article, figures, figure_explanations, synthesis)
        except Exception as e:
            note_result = {"key": None, "parented": False, "message": f"写入失败:{e}"}
        local_html = ""
        try:
            local_html = nw.build_note_html(article, figures, figure_explanations, synthesis)
            out = os.path.join(HERE, f"figexplain_{article['key']}.html")
            with open(out, "w", encoding="utf-8") as f:
                f.write(local_html)
        except Exception:
            pass

        job["status"] = "done"
        job["article"] = article
        job["figures"] = [{"index": f.index, "panels": [p.label for p in f.panels]} for f in figures]
        job["note"] = note_result
        job["local_html"] = local_html
        say(f"完成:note_key={note_result.get('key')} parented={note_result.get('parented')}")
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict, is_json: bool = True) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, {"ok": True, "jobs": len(JOBS)})
        if path.startswith("/jobs/"):
            jid = path[len("/jobs/"):]
            job = JOBS.get(jid)
            if not job:
                return self._send(404, {"error": f"job {jid} not found"})
            return self._send(200, {k: v for k, v in job.items() if k != "log"})
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path != "/explain":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "invalid JSON body"})
        item_key = str(body.get("item_key", "")).strip()
        if not item_key:
            return self._send(400, {"error": "item_key required"})
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "status": "queued", "log": [], "log_tail": "", "figures_done": 0}
        JOBS[jid] = job
        t = threading.Thread(target=_run_pipeline, args=(job, item_key), daemon=True)
        t.start()
        self._send(202, {"job_id": jid, "status": "queued"})


def main() -> None:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"figexplain-api on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
