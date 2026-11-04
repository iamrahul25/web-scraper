from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .crawler import Page
from .engines import SearchResult


def _slug(text: str, n: int = 50) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "query"


def write_outputs(
    out_dir: Path,
    query: str,
    settings: dict,
    results_by_engine: dict[str, list[SearchResult]],
    pages: list[Page],
    max_chars: int = 0,
) -> dict[str, Path]:
    stamp = datetime.now()
    run_dir = out_dir / f"{stamp:%Y%m%d-%H%M%S}_{_slug(query)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    ok = [p for p in pages if p.text]
    dupes = [p for p in pages if p.duplicate_of is not None]
    failed = [p for p in pages if not p.text and p.duplicate_of is None]
    total_chars = sum(p.chars for p in ok)

    data = {
        "query": query,
        "generated_at": stamp.isoformat(timespec="seconds"),
        "settings": settings,
        "stats": {"pages_total": len(pages), "pages_ok": len(ok), "pages_duplicate": len(dupes),
                  "pages_failed": len(failed), "total_chars": total_chars},
        "search_results": {e: [asdict(r) for r in rs] for e, rs in results_by_engine.items()},
        "pages": [asdict(p) for p in pages],
    }
    json_path = run_dir / "data.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    by_id = {p.id: p for p in pages}
    md: list[str] = [
        f"# Raw research data: {query}",
        "",
        f"- Generated: {stamp:%Y-%m-%d %H:%M:%S}",
        f"- Engines: {', '.join(results_by_engine) or '-'}",
        f"- Top results per engine: {settings['top']} | link depth: {settings['depth']} | links per page: {settings['links_per_page']}",
        f"- Pages with text: {len(ok)} of {len(pages)} ({len(dupes)} duplicates skipped) | total characters: {total_chars:,}",
        "",
        "## Search results",
        "",
    ]
    for engine, rs in results_by_engine.items():
        md.append(f"### {engine} ({len(rs)} results)")
        md.append("")
        if not rs:
            md.append("_No results (engine blocked the request or returned unrelated results)._")
        for r in rs:
            md.append(f"{r.rank}. [{r.title or r.url}]({r.url})" + (f" — {r.snippet}" if r.snippet else ""))
        md.append("")

    md += ["## Page contents", ""]
    for p in ok:
        text = p.text if not max_chars or len(p.text) <= max_chars else p.text[:max_chars] + "\n\n[... truncated ...]"
        md.append("---")
        md.append("")
        md.append(f"### [{p.id}] {p.title or p.url}")
        md.append("")
        md.append(f"- URL: {p.final_url or p.url}")
        if p.found_via:
            md.append("- Found via: " + ", ".join(f"{v['engine']} #{v['rank']}" for v in p.found_via))
        else:
            parent = by_id.get(p.parent_id)
            md.append(f"- Linked from: [{p.parent_id}] {parent.title if parent and parent.title else p.parent_url}"
                      + (f' (link text: "{p.link_anchor[:100]}")' if p.link_anchor else ""))
        md.append(f"- Depth: {p.depth}")
        if p.author:
            md.append(f"- Author: {p.author}")
        if p.published:
            md.append(f"- Published: {p.published}")
        md += ["", text, ""]

    if failed:
        md += ["---", "", "## Pages that could not be read", ""]
        md += [f"- [{p.id}] {p.url} — {p.error}" for p in failed]
        md.append("")

    md_path = run_dir / "raw_data.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    return {"dir": run_dir, "markdown": md_path, "json": json_path}
