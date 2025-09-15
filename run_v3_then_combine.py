#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_v3_then_combine.py - v3 (Base64 HTML embed)
- Step 1: run py2mermaid_v3 to generate MD/HTML
- Step 2: combine all ```mermaid blocks into a single .mmd
- Step 3: inject the combined diagram into the HTML using data-code-b64 (same mechanism as per-file charts)
- Optional: include non-mermaid Markdown text as comments (%% ...) in the .mmd
"""
import sys
import argparse
import base64
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import py2mermaid_v3 as v3
import combine_mermaid_blocks as cmb

def md_non_mermaid_as_comments(md_text: str) -> str:
    out_lines = []
    in_code = False
    for line in md_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        out_lines.append("%%" if stripped == "" else ("%% " + line))
    return "\n".join(out_lines) + "\n"

def inject_combined_into_html(html_path: Path, combined_mmd: str, section_title: str = "Combined Diagram") -> None:
    html = html_path.read_text(encoding="utf-8", errors="replace")
    b64 = base64.b64encode(combined_mmd.encode("utf-8")).decode("ascii")
    snippet = (
        "\n<section id=\"combined-diagram\">\n"
        f"<h2>{section_title}</h2>\n"
        f"<div class=\"mermaid\" data-code-b64=\"{b64}\"></div>\n"
        "</section>\n"
    )
    lower = html.lower()
    start = lower.find('<section id="combined-diagram"')
    if start != -1:
        end = lower.find('</section>', start)
        if end != -1:
            end += len('</section>')
            html = html[:start] + snippet + html[end:]
        else:
            html += snippet
    else:
        idx = lower.rfind('</body>')
        if idx != -1:
            html = html[:idx] + snippet + html[idx:]
        else:
            html += snippet
    html_path.write_text(html, encoding="utf-8")

def run(root: Path,
        fmt: str,
        md_out: Path,
        html_out: Path,
        max_files: int,
        ignore_csv: str,
        mermaid_zip: str | None,
        mermaid_js: str | None,
        title: str | None,
        theme: str,
        collapse: bool,
        flow_dir: str,
        combined_out: Path,
        embed_combined_into_html: bool,
        include_md_text_in_mmd: bool):
    root = root.resolve()

    ignore = [s.strip() for s in (ignore_csv or "").split(",") if s.strip()]
    files = v3.scan_py_files(root, ignore, max_files)
    if not files:
        print("No .py files found under:", root, file=sys.stderr)
        sys.exit(2)

    charts_by_file: dict[Path, list[tuple[str, str]]] = {}
    for f in files:
        try:
            charts_by_file[f] = v3.build_for_file(f)
        except SyntaxError as e:
            print(f"[skip] {f} syntax error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[skip] {f} error: {e}", file=sys.stderr)

    wrote_md = False

    if fmt in ("md", "both"):
        v3.write_markdown(root, files, charts_by_file, md_out)
        wrote_md = True
        print(f"[v3] Wrote Markdown: {md_out} ({len(charts_by_file)} parsed file(s))")

    if fmt in ("html", "both"):
        mz = Path(mermaid_zip) if mermaid_zip else None
        mj = Path(mermaid_js) if mermaid_js else None
        v3.write_html(root, files, charts_by_file, html_out, mz, mj, title, theme, collapse)
        print(f"[v3] Wrote HTML: {html_out} ({len(charts_by_file)} parsed file(s))")

    if not wrote_md:
        tmp_md = md_out if md_out else (html_out.with_suffix(".md"))
        v3.write_markdown(root, files, charts_by_file, tmp_md)
        md_out = tmp_md
        wrote_md = True
        print(f"[v3] Also wrote Markdown (needed for combine): {md_out}")

    md_text = Path(md_out).read_text(encoding="utf-8", errors="replace")
    blocks = cmb.extract_blocks(md_text)
    if not blocks:
        print(f"[combine] No ```mermaid blocks found in {md_out}", file=sys.stderr)
        sys.exit(3)

    combined = cmb.combine_blocks(blocks, flow_dir=flow_dir)

    if include_md_text_in_mmd:
        combined = combined.rstrip() + "\n\n%% ---- Non-mermaid Markdown (as comments) ----\n" + md_non_mermaid_as_comments(md_text)

    combined_out.write_text(combined, encoding="utf-8")
    print(f"[combine] Combined {len(blocks)} block(s) -> {combined_out}")

    if embed_combined_into_html:
        if fmt in ("html", "both") and html_out.exists():
            inject_combined_into_html(html_out, combined_mmd=combined, section_title="Combined Diagram")
            print(f"[html] Embedded combined diagram into: {html_out}")
        else:
            print("[html] Skipped embedding (HTML not generated). Use --format html/both to enable.")

def main():
    ap = argparse.ArgumentParser(description="Run py2mermaid_v3 then combine Mermaid blocks into a single diagram, embed Base64 into HTML, and include MD comments in .mmd.")
    ap.add_argument("root", help="project folder to scan (python sources)")
    ap.add_argument("--format", choices=["md","html","both"], default="both", help="py2mermaid_v3 output format")
    ap.add_argument("--md-out", default="mermaid.md", help="Markdown output path (used for combine step)")
    ap.add_argument("--html-out", default="mermaid.html", help="HTML output path (optional)")
    ap.add_argument("--max-files", type=int, default=500)
    ap.add_argument("--ignore", default="venv,.venv,site-packages,__pycache__,.git,.hg,.mypy_cache,.pytest_cache")
    ap.add_argument("--mermaid-zip", default=None, help="path to mermaid-11.x zip to embed (optional)")
    ap.add_argument("--mermaid-js", default=None, help="path to mermaid.min.js if not using zip (optional)")
    ap.add_argument("--title", default=None, help="HTML page title")
    ap.add_argument("--theme", default="default")
    ap.add_argument("--collapse", action="store_true", help="HTML uses <details> blocks per chart")
    ap.add_argument("--flow-dir", choices=["TB","TD","LR","RL","BT"], default="TD", help="flow direction for combined diagram")
    ap.add_argument("--combined-out", default="combined.mmd", help="output single merged Mermaid diagram (.mmd)")
    ap.add_argument("--no-embed-combined-into-html", action="store_true", help="do not inject combined diagram into HTML")
    ap.add_argument("--no-include-md-text-in-mmd", action="store_true", help="do not include non-mermaid MD text as comments in .mmd")
    args = ap.parse_args()

    run(root=Path(args.root),
        fmt=args.format,
        md_out=Path(args.md_out),
        html_out=Path(args.html_out),
        max_files=args.max_files,
        ignore_csv=args.ignore,
        mermaid_zip=args.mermaid_zip,
        mermaid_js=args.mermaid_js,
        title=args.title,
        theme=args.theme,
        collapse=args.collapse,
        flow_dir=args.flow_dir,
        combined_out=Path(args.combined_out),
        embed_combined_into_html=not args.no_embed_combined_into_html,
        include_md_text_in_mmd=not args.no_include_md_text_in_mmd)

if __name__ == "__main__":
    main()
