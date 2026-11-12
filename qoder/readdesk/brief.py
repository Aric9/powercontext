"""证据简报生成：按文档分组、带 `文件#L起-L止` 引用、覆盖统计与使用说明。"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from readdesk.retrieve import Evidence, hybrid_search
from readdesk.store import Store
from readdesk.util import slugify

MAX_PER_DOC = 4


def generate_brief_markdown(store: Store, topic: str, limit: int = 24) -> str:
    """检索并生成 Markdown 简报文本。"""
    results = hybrid_search(store, topic, limit=limit)
    groups: dict[str, list[Evidence]] = {}
    order: list[str] = []
    for e in results:
        if e.doc_id not in groups:
            groups[e.doc_id] = []
            order.append(e.doc_id)
        if len(groups[e.doc_id]) < MAX_PER_DOC:
            groups[e.doc_id].append(e)
    grouped = [groups[d] for d in order]
    covered = sum(len(g) for g in grouped)
    citations = sorted({e.citation for g in grouped for e in g})

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = [f"# 证据简报：{topic}", "", f"- 生成时间：{now}", f"- 检索词：{topic}",
                        f"- 命中证据：{covered} 条（去重引用 {len(citations)} 个）", "", "## 证据", ""]
    if not grouped:
        lines.append("（没有检索到任何证据，请更换关键词或先导入笔记。）")
    for gi, group in enumerate(grouped, start=1):
        first = group[0]
        meta_line = f"- 文件：`{first.path}` · 年份：{first.year or '未知'} · 标签：{', '.join(first.tags) or '无'}"
        lines.append(f"### {gi}. {first.title}")
        lines.append(meta_line)
        lines.append("")
        for e in group:
            lines.append(f"**[{e.citation}] {e.section}** （命中：{' + '.join(e.matched_by)}，score={e.score:.4f}）")
            lines.append("")
            body = e.text.split("\n\n", 1)[-1] if "\n\n" in e.text else e.text
            para = " ".join(body.split())
            lines.append(f"> {para}")
            lines.append("")
    stats = store.doc_count()
    total_chunks = store.chunk_count()
    tag_dist: dict[str, int] = {}
    for g in grouped:
        for t in g[0].tags:
            tag_dist[t] = tag_dist.get(t, 0) + 1
    tag_summary = "、".join(f"{t}（{c}）" for t, c in sorted(tag_dist.items(), key=lambda x: -x[1])) or "无"
    lines += [
        "## 覆盖统计",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 文档库总数 | {stats} |",
        f"| 本简报覆盖文档 | {len(grouped)} |",
        f"| 证据块数 | {covered} |",
        f"| 去重引用数 | {len(citations)} |",
        f"| 库内块总数 | {total_chunks} |",
        "",
        f"标签分布：{tag_summary}",
        "",
        "## 使用说明",
        "",
        "- 引用格式 `文件#L起-L止` 为导入源文件的 1-based 闭区间行号，可直接 `sed -n '起,止p' 文件` 复核原文。",
        "- 本简报由本地混合检索（dense + 全文 + 元数据过滤，RRF k=60）生成，未调用任何在线模型，数据不出本机。",
        "- 段落原文较长时写作请回到源文件确认上下文；覆盖率低时请尝试更具体或更多同义词的查询。",
        "",
    ]
    return "\n".join(lines)


def write_brief(store: Store, topic: str, out: Path | None = None, limit: int = 24) -> Path:
    """生成并写出简报到 Markdown 文件，返回文件路径。"""
    md = generate_brief_markdown(store, topic, limit=limit)
    if out is None:
        store.settings.ensure()
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = store.settings.briefs_dir / f"{slugify(topic)}-{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    return out
