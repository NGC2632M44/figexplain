"""Multi-modal LLM explanation of figures + whole-paper logical synthesis.

Pipeline (the user's requested logic):
  1. For each figure: send image + caption + refs to a vision model, get a
     per-panel Chinese annotation + a one-line "what this figure establishes".
     This derives the figure-level logical structure.
  2. Collect the figure-derived logical structure and compare it against the
     Abstract / Introduction (the "overview" sections): list agreements and the
     differences/omissions (points present in figures but missing from the
     overview, and vice versa).
  3. Locate, inside the full main text, the specific sentences that support
     each difference/omission (return verbatim quotes + page-ish location).
  4. Synthesize a final bullet list of the article's key points, combining the
     figure information with the main text.

All calls go to an OpenAI-compatible chat/completions endpoint with vision.
"""
from __future__ import annotations
import json
import base64
import time
from typing import Any

import requests

from .pdf_extract import Figure

# 连接策略(实测 2026-08-01):
# - 显式禁用代理:Claude Code 注入的 HTTP(S)_PROXY 转发境外 LLM 端点会断连
# - 每请求新建连接(Connection: close):babelark 掐断 keep-alive 复用连接
#   (Session 复用第二次请求必 RemoteDisconnected;逐请求稳定)
# - Windows 端口耗尽(WinError 10048):靠重试 backoff 等待 TIME_WAIT 释放

# Some vision models take much longer on the first request (cold start /
# image encoding). Allow a longer per-call timeout and a couple of retries
# for transient network/timeout errors.
_DEFAULT_TIMEOUT = 240
_MAX_RETRIES = 4
# Per-figure explanation JSON for a multi-panel figure easily exceeds
# 1500 tokens. The model silently truncates mid-string when it hits
# max_tokens, producing unbalanced JSON no parser can recover. Bump
# well above the observed output size so the JSON can finish.
# (2026-08-01:PNAS 长 caption 图实测 6000 仍截断,提到 10000 + 解析失败重试)
_FIGURE_MAX_TOKENS = 10000
_SYNTH_MAX_TOKENS = 8000


def _chat(base_url: str, api_key: str, model: str, messages: list[dict],
          temperature: float = 0.2, max_tokens: int = 1500,
          timeout: int = _DEFAULT_TIMEOUT) -> str:
    """Call an OpenAI-compatible /v1/chat/completions endpoint, return text.

    Retries on transient errors (timeout / connection / 5xx) with a short
    backoff so a single slow response doesn't fail the whole run. 4xx errors
    with a body surface immediately (they're usually content/policy rejections
    that won't be fixed by retrying).
    """
    url = base_url.rstrip("/")
    if not url.endswith("/v1/chat/completions"):
        if url.endswith("/v1"):
            url = url + "/chat/completions"
        else:
            url = url + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    last_err = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            # Connection: close 禁用 keep-alive(babelark 掐复用连接);
            # proxies=None 禁代理(直连)
            resp = requests.post(url, headers={**headers, "Connection": "close"},
                                 json=payload, timeout=timeout,
                                 proxies={"http": None, "https": None})
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = RuntimeError(f"LLM 网络超时/连接失败: {e}")
            if attempt < _MAX_RETRIES:
                # 长 backoff:10048(端口耗尽)需等 TIME_WAIT 释放(120s 级)
                time.sleep(20 * (attempt + 1))
                continue
            raise last_err
        if resp.status_code >= 500:
            last_err = RuntimeError(f"LLM 请求失败 {resp.status_code}: {resp.text[:400]}")
            if attempt < _MAX_RETRIES:
                time.sleep(3 * (attempt + 1))
                continue
            raise last_err
        if resp.status_code >= 400:
            raise RuntimeError(f"LLM 请求失败 {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"LLM 响应解析失败: {e}; body={str(data)[:300]}") from e
    raise last_err  # type: ignore[misc]


def _json_response(text: str) -> Any:
    """Try to extract a JSON object from an LLM text response.

    Tolerant of: ```json fences, prose before/after, thinking tokens
    prefixed by the gateway, and partial trailing junk.
    """
    s = text.strip()
    if not s:
        raise RuntimeError("empty LLM response")
    # strip ```json ... ``` fences (and bare ```)
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
    if s.endswith("```"):
        s = s[:-3]
    s = s.strip()
    # find the outermost balanced { ... } by scanning from the first '{'
    start = s.find("{")
    if start < 0:
        raise RuntimeError(f"no JSON object found in response: {s[:200]!r}")
    depth = 0
    in_str = False
    esc = False
    end = -1
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
    if end < 0:
        # unbalanced — try json.loads on the tail anyway for a better error
        raise RuntimeError(f"unbalanced JSON in response: {s[start:start+200]!r}")
    obj_str = s[start:end + 1]
    return json.loads(obj_str)


# --- Stage 1: per-figure panel annotations + figure gist ---------------------

FIGURE_PROMPT_ZH = """你是一个科研论文图表解读助手。下面给出一篇论文里某一张图（Figure {idx}）的：
1. 图像本身
2. 原文 caption（已按子图标号 A/B/C... 切分）
3. 正文里引用这张图的若干句子（refs）

请基于【图像内容】和 caption，对每一个子图（panel）给出一句精炼的中文解读（说明这个 panel 展示了什么、得到什么结论）。
然后给出一句"本图核心结论"（这张图整体上证明了/展示了什么）。

严格按下面的 JSON 格式输出，不要任何额外文字：
{{
  "panels": [
    {{"label": "A", "caption_en": "原文该 panel 的 caption 片段", "annotation_zh": "中文解读"}},
    ...
  ],
  "gist_zh": "本图核心结论一句话",
  "logical_role": "这张图在文章逻辑里扮演什么角色（如：建立模型/验证假设/揭示机制/对比条件）"
}}

caption 切分如下：
{caption_segments}

正文引用句（refs）：
{refs}

注意：如果某 panel 的 caption 片段为空，仍保留 label 并尽力从图像推断。不要编造图像里没有的内容。

输出要求（重要）：
- 只输出 JSON 对象本身，不要输出任何思考过程、解释、Markdown 标题
- 不要使用 ```json 代码围栏
- JSON 必须可被 json.loads 直接解析；字符串值里的双引号用反斜杠加引号转义
- 如果你的推理过程需要打草稿，请在 JSON 之外绝不出现；只返回最终 JSON"""


def explain_figure(fig: Figure, base_url: str, api_key: str, model: str) -> dict:
    """Return {panels:[...], gist_zh, logical_role} for one figure."""
    if not fig.image_b64:
        return {"panels": [{"label": p.label, "caption_en": p.caption_segment,
                            "annotation_zh": "（无图像，无法解读）"} for p in fig.panels],
                "gist_zh": "", "logical_role": ""}
    cap_segs = "\n".join(f"[{p.label}] {p.caption_segment}" for p in fig.panels) or "(未切分到子图)"
    refs_txt = "\n".join(f"- {r}" for r in fig.refs) or "(未抽取到正文引用句)"
    prompt_text = FIGURE_PROMPT_ZH.format(
        idx=fig.index, caption_segments=cap_segs, refs=refs_txt,
    )
    data_url = f"data:{fig.image_mime};base64,{fig.image_b64}"
    messages = [
        {"role": "system", "content": "你是科研图表解读助手，输出 JSON。"},
        {"role": "user", "content": [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ]
    raw = _chat(base_url, api_key, model, messages, max_tokens=_FIGURE_MAX_TOKENS)
    try:
        return _json_response(raw)
    except Exception as e:
        # 解析失败(多为 max_tokens 截断产生 unbalanced JSON):重试一次,
        # 明确要求只输出完整 JSON;仍失败才回退占位(附原文便于排查)
        try:
            retry_msgs = messages + [{"role": "assistant", "content": raw},
                                     {"role": "user", "content": "你上次的输出 JSON 不完整(被截断或格式错误)。请重新只输出完整的 JSON 对象,不要任何额外文字。"}]
            raw2 = _chat(base_url, api_key, model, retry_msgs, max_tokens=_FIGURE_MAX_TOKENS)
            return _json_response(raw2)
        except Exception as e2:
            return {"panels": [], "gist_zh": f"（解析失败: {e} / 重试: {e2}）",
                    "logical_role": "", "_raw": raw[:1200]}


# --- Stage 2+3: compare figure structure vs abstract/intro, locate in text ---

DIFF_PROMPT_ZH = """你是一个科研论文逻辑结构分析助手。

下面给你：
A) 从全部图表解读中归纳出的"图表逻辑结构"（每张图证明了什么、各图之间的逻辑关系）。
B) 论文的摘要（Abstract）。
C) 论文的引言/概述部分（Introduction）。
D) 论文正文全文（供定位用，可能被截断）。

任务：
1. 先用中文归纳这篇文章的"图表逻辑结构"（按图编号列出每张图的核心结论，并说明图与图之间如何递进/支撑）。
2. 把这个图表逻辑结构与摘要、引言做对比，列出：
   - "图表里有、但摘要/引言里没说或说得不够"的差异与遗漏（omissions）
   - "摘要/引言里有、但图表没有直接支撑"的差异
   每一条要写明涉及哪张图。
3. 对上面每一条差异/遗漏，到正文 D 里定位支撑它的具体句子，原样引用（英文原文 + 中文简译），并尽量给出所在小节关键词。
4. 最后，综合图表信息和正文，用要点（bullet）总结这篇文章的所有关键点（中文，10 条以内）。

严格按下面 JSON 输出，不要额外文字：
{{
  "figure_logical_structure": "中文，可分点",
  "differences": [
    {{"type": "omission|overview_only", "figure": "Fig. N 或 多张", "point_zh": "差异点", "evidence_quote_en": "正文原句", "evidence_zh": "中文简译", "location_hint": "所在小节/上下文关键词"}}
  ],
  "key_points": ["要点1", "要点2", ...]
}}

A) 图表逻辑结构（来自各图解读）：
{figure_structure}

B) 摘要：
{abstract}

C) 引言/概述：
{intro}

D) 正文全文：
{full_text}

输出要求（重要）：
- 只输出 JSON 对象本身，不要输出任何思考过程、解释、Markdown 标题
- 不要使用 json 代码围栏
- JSON 必须可被 json.loads 直接解析；字符串值里的引号用反斜杠转义
"""


def synthesize(figure_explanations: list[dict], abstract: str, intro: str,
               full_text: str, base_url: str, api_key: str, model: str) -> dict:
    """Stage 2-4: structure, diff, locate, key points. Returns parsed JSON."""
    # build figure structure summary from per-figure explanations
    fs_parts = []
    for fe in figure_explanations:
        idx = fe.get("index", "?")
        gist = fe.get("gist_zh", "")
        role = fe.get("logical_role", "")
        fs_parts.append(f"图 {idx}：{gist}（角色：{role}）")
    figure_structure = "\n".join(fs_parts) or "(未提取到图表解读)"

    prompt = DIFF_PROMPT_ZH.format(
        figure_structure=figure_structure,
        abstract=abstract or "(未提取到摘要)",
        intro=intro or "(未提取到引言，请从全文D中自行定位)",
        full_text=full_text,
    )
    messages = [
        {"role": "system", "content": "你是科研论文逻辑结构分析助手，输出 JSON。"},
        {"role": "user", "content": prompt},
    ]
    raw = _chat(base_url, api_key, model, messages, temperature=0.2,
                max_tokens=_SYNTH_MAX_TOKENS, timeout=_DEFAULT_TIMEOUT)
    try:
        return _json_response(raw)
    except Exception as e:
        return {"figure_logical_structure": f"（解析失败: {e}）",
                "differences": [], "key_points": [],
                "_raw": raw[:1500]}
