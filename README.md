# figexplain

论文图表驱动的逻辑结构与要点总结工具。

从一篇论文的 PDF 出发：提取每张图（图像 + caption + 子图 panel）与正文引用句 → 用多模态 LLM 解读每个子图、归纳图表逻辑结构 → 与摘要/引言做对比、定位正文里的差异与遗漏 → 综合图表信息和正文总结关键要点 → 把结果写成一条 Zotero 笔记。

灵感来自 [zotero-figure](https://github.com/MuiseDestiny/zotero-figure)（PDF 图表提取思路）与一个本地 `fig_explain` 产物包（manifest schema + 自包含 HTML 模板），但本项目是**独立的 Python 脚本工具**，不依赖 Zotero 插件运行环境。

## 功能流程

```
PDF (Zotero 附件)
  │
  ├─ pdf_extract.py ── 每图图像 + caption + 子图分割 + 正文 Fig 引用句 + 摘要/引言
  │
  ├─ llm_explain.py ── 阶段1：每图 panel 中文注释 + 图核心结论 + 逻辑角色 (vision)
  │                   阶段2：图表逻辑结构 vs 摘要/引言 → 差异/遗漏 → 正文定位 → 要点总结
  │
  └─ note_writer.py ── 自包含 HTML 笔记 (图+panel表+结构+差异表+要点) → 写入 Zotero
```

## 依赖

```bash
pip install -r requirements.txt
```

需要：Python 3.10+，PyMuPDF、Pillow、requests。

## 用法

1. 启动 Zotero（本地 HTTP 服务 `127.0.0.1:23119` 随之运行）。
2. 在 Zotero 里点开目标文献所在的分类。
3. 运行：

```bash
python run.py            # 列出当前分类的文献，输入序号选择
python run.py <itemKey>  # 直接按 Zotero item key 处理
```

4. 首次运行会交互输入 OpenAI 兼容 `base_url` / `api_key` / 模型名 / Zotero storage 目录，存到 `~/.figexplain/config.json`（之后每次运行可回车保留或覆盖）。模型需支持 vision（如 `gpt-4o`、`qwen-vl-max`）。
5. 跑完后，Zotero 当前分类下会出现一条顶层笔记（标题 `图表解读：<原文献名>`，标签 `fig-explain` / `fig-explain-<key>`），同时生成一份本地 HTML 副本 `figexplain_<key>.html`。

## 已知约束（Zotero 本地 API 实测）

- 本地 HTTP API 不能创建"挂到父文献下的子笔记"（`saveItems` 忽略 `parentItem`），也没有"读取当前选中条目"的端点。因此笔记以**顶层 note**写入当前分类，内容里嵌原文献标题/作者/DOI/key 做关联。
- `DELETE/PATCH/PUT` 一律返回 501，不可用。

## 项目结构

```
run.py                     # 入口
figexplain/
  config.py                # ~/.figexplain/config.json，交互式配置
  zotero_local.py          # Zotero 本地 HTTP API 客户端
  pdf_extract.py           # 图表 / caption / panel / refs / 摘要 / 引言 提取
  llm_explain.py           # 多模态 LLM 解释（每图 + 综合）
  note_writer.py           # 笔记 HTML 组装 + 写回
requirements.txt
```

## 隐私说明

- API key 只存在本机 `~/.figexplain/config.json`，**不进仓库**（见 `.gitignore`）。
- 不向任何第三方服务发送数据，除你配置的 LLM 端点（图图像 + 文本）和本机 Zotero 外。
- 笔记本地副本 `figexplain_*.html` 已加入 `.gitignore`。

## License

MIT
