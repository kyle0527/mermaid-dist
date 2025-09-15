#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
py2mermaid_v2 — Generate Mermaid flowcharts from a Python project folder,
with *offline* HTML preview powered by Mermaid 11.x (works with mermaid-11.10.0.zip).

This is a drop-in enhancement of the original py2mermaid.py, adding:
- Robust escaping for Mermaid labels (quotes, backslashes, angle brackets, newlines).
- Support for Python 3.10+ syntax blocks: match/case, with/async-with, async for/def, class def.
- Optional HTML exporter that EMBEDS mermaid.min.js directly from the provided ZIP or JS path.
- Optional Markdown (MD) output (compatible with GitHub/VS Code Mermaid renderers).
- Small UX improvements: file index, collapsible sections, and a table of contents in HTML.

Usage examples:
  # Markdown only (default behavior)
  python py2mermaid_v2.py /path/to/project --out mermaid.md

  # HTML preview using the provided Mermaid 11 zip (fully offline, single-file HTML)
  python py2mermaid_v2.py /path/to/project --format html --html-out mermaid.html --mermaid-zip mermaid-11.10.0.zip

  # Both MD and HTML
  python py2mermaid_v2.py /path/to/project --format both --html-out mermaid.html --mermaid-zip mermaid-11.10.0.zip

Notes:
- The HTML mode tries to load Mermaid from either --mermaid-zip (preferred) or --mermaid-js.
- If neither is given, it will still produce HTML but rely on a CDN fallback (requires internet).
- For large projects, HTML rendering can be heavy; you can pass --collapse to make sections collapsible.

License: MIT
"""

import os, ast, sys, argparse, io, textwrap, html
from pathlib import Path
from typing import List, Tuple, Dict, Optional, Iterable
from zipfile import ZipFile

# ---------------------------- Core CFG builder ---------------------------- #

class Node:
    __slots__ = ("kind", "label", "id", "nexts")
    def __init__(self, kind: str, label: str):
        self.kind = kind  # "start", "op", "cond", "end"
        self.label = label
        self.id = None  # assigned later
        self.nexts: List["Node"] = []

    def __repr__(self):
        return f"<Node {self.kind}:{self.label[:20]!r}>"

class Graph:
    def __init__(self, title: str):
        self.title = title
        self.nodes: List[Node] = []
        self.start = self.add("start", f"Start: {title}")
        self.end = self.add("end", "End")
        self._counter = 0

    def add(self, kind: str, label: str) -> Node:
        n = Node(kind, label)
        n.id = f"n{len(self.nodes)}"
        self.nodes.append(n)
        return n

    def link(self, a: Node, b: Node):
        if b not in a.nexts:
            a.nexts.append(b)

    @staticmethod
    def _esc_mermaid_label(text: str) -> str:
        """
        Escape a string so it is safe inside Mermaid node label quotes.
        We wrap labels as "...", so we must escape: backslash, double-quotes, newlines.
        Also escape angle brackets to avoid accidental HTML interpretation in some renderers.
        """
        if text is None:
            return ""
        # Normalize to str
        text = str(text)
        # Replace backslash *first*
        text = text.replace("\\", "\\\\")
        # Replace double quotes
        text = text.replace('"', '\\"')
        # Normalize CRLF -> LF, then Mermaid line break sequence
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\n", "\\n")
        # Escape angle brackets to be safe in strict security mode
        text = text.replace("<", "&lt;").replace(">", "&gt;")
        # Avoid stray backticks interfering with Markdown fences
        text = text.replace("`", "\\`")
        return text

    def to_mermaid(self) -> str:
        lines = ["flowchart TD"]
        def fmt(n: Node) -> str:
            text = Graph._esc_mermaid_label(n.label)
            if n.kind == "cond":
                return f'{n.id}{{"{text}"}}'
            elif n.kind == "end":
                return f'{n.id}([ {text} ])'
            elif n.kind == "start":
                return f'{n.id}([ {text} ])'
            else:
                return f'{n.id}["{text}"]'
        for n in self.nodes:
            lines.append(f"    {fmt(n)}")
        for n in self.nodes:
            for idx, m in enumerate(n.nexts):
                if n.kind == "cond":
                    label = "True" if idx == 0 else ("False" if idx == 1 else "")
                    if label:
                        lines.append(f"    {n.id} -->|{label}| {m.id}")
                    else:
                        lines.append(f"    {n.id} --> {m.id}")
                else:
                    lines.append(f"    {n.id} --> {m.id}")
        return "\n".join(lines)

class Builder(ast.NodeVisitor):
    def __init__(self, title: str):
        self.g = Graph(title)

    # ----------------- public entry points ----------------- #
    def build_module(self, node: ast.AST) -> Graph:
        last = self.g.start
        last = self._build_block(node.body, last)
        self.g.link(last, self.g.end)
        return self.g

    def build_function(self, func: ast.AST) -> Graph:
        if isinstance(func, (ast.FunctionDef, getattr(ast, "AsyncFunctionDef", ast.FunctionDef))):
            title = getattr(func, "name", "<lambda>")
            params = []
            for a in func.args.args:
                params.append(a.arg)
            sig = f"{title}({', '.join(params)})"
        else:
            sig = str(func)
        self.g = Graph(sig)
        last = self.g.start
        body = getattr(func, "body", [])
        last = self._build_block(body, last)
        self.g.link(last, self.g.end)
        return self.g

    # -------------------- block/statement synthesizers -------------------- #

    def _build_block(self, stmts: List[ast.stmt], last: Node) -> Node:
        for s in stmts:
            last = self._build_stmt(s, last)
        return last

    def _label_expr(self, expr: Optional[ast.AST]) -> str:
        if expr is None:
            return ""
        try:
            # Python 3.9+
            return ast.unparse(expr)
        except Exception:
            # Fallback
            return expr.__class__.__name__

    def _op(self, text: str) -> Node:
        return self.g.add("op", text)

    def _cond(self, text: str) -> Node:
        return self.g.add("cond", text)

    # Main dispatcher
    def _build_stmt(self, s: ast.stmt, last: Node) -> Node:
        # ---- If / Elif / Else ----
        if isinstance(s, ast.If):
            cond = self._cond(f"if {self._label_expr(s.test)}")
            self.g.link(last, cond)
            true_tail = self._build_block(s.body, cond)
            if s.orelse:
                false_tail = self._build_block(s.orelse, cond)
                merge = self._op("merge")
                self.g.link(true_tail, merge)
                self.g.link(false_tail, merge)
                return merge
            else:
                merge = self._op("merge")
                self.g.link(true_tail, merge)
                self.g.link(cond, merge)  # false fall-through
                return merge

        # ---- For / While / AsyncFor ----
        elif isinstance(s, ast.For):
            hdr = self._cond(f"for {self._label_expr(s.target)} in {self._label_expr(s.iter)}")
            self.g.link(last, hdr)
            body_tail = self._build_block(s.body, hdr)
            self.g.link(body_tail, hdr)  # loop back
            merge = self._op("after for")
            self.g.link(hdr, merge)      # false branch (no iterations)
            return merge

        elif isinstance(s, getattr(ast, "AsyncFor", ast.For)):
            if isinstance(s, getattr(ast, "AsyncFor", ())):
                hdr = self._cond(f"async for {self._label_expr(s.target)} in {self._label_expr(s.iter)}")
                self.g.link(last, hdr)
                body_tail = self._build_block(s.body, hdr)
                self.g.link(body_tail, hdr)
                merge = self._op("after async for")
                self.g.link(hdr, merge)
                return merge

        elif isinstance(s, ast.While):
            hdr = self._cond(f"while {self._label_expr(s.test)}")
            self.g.link(last, hdr)
            body_tail = self._build_block(s.body, hdr)
            self.g.link(body_tail, hdr)  # loop back
            merge = self._op("after while")
            self.g.link(hdr, merge)      # false branch
            return merge

        # ---- With / AsyncWith ----
        elif isinstance(s, ast.With):
            items = "; ".join([self._label_expr(it.context_expr) for it in s.items])
            hdr = self._op(f"with {items}")
            self.g.link(last, hdr)
            return self._build_block(s.body, hdr)

        elif isinstance(s, getattr(ast, "AsyncWith", ast.With)):
            if isinstance(s, getattr(ast, "AsyncWith", ())):
                items = "; ".join([self._label_expr(it.context_expr) for it in s.items])
                hdr = self._op(f"async with {items}")
                self.g.link(last, hdr)
                return self._build_block(s.body, hdr)

        # ---- Try / Except / Finally ----
        elif isinstance(s, ast.Try):
            hdr = self._op("try")
            self.g.link(last, hdr)
            try_tail = self._build_block(s.body, hdr)
            exits = [try_tail]
            for h in s.handlers:
                lab = f"except {self._label_expr(h.type) or ''}".strip()
                hnode = self._op(lab)
                self.g.link(hdr, hnode)
                exits.append(self._build_block(h.body, hnode))
            # else: executed if no exception in try
            if s.orelse:
                enode = self._op("else")
                for e in [try_tail]:
                    self.g.link(e, enode)
                else_tail = self._build_block(s.orelse, enode)
                exits = [else_tail] + exits[1:]  # replace try-tail with else-tail
            if s.finalbody:
                fnode = self._op("finally")
                for e in exits:
                    self.g.link(e, fnode)
                tail = self._build_block(s.finalbody, fnode)
                return tail
            else:
                merge = self._op("after try")
                for e in exits:
                    self.g.link(e, merge)
                return merge

        # ---- Function / AsyncFunction / Class ----
        elif isinstance(s, (ast.FunctionDef, getattr(ast, "AsyncFunctionDef", ast.FunctionDef))):
            label = f"def {s.name}(...)"
            if isinstance(s, getattr(ast, "AsyncFunctionDef", ())):
                label = f"async {label}"
            n = self._op(label)
            self.g.link(last, n)
            return n

        elif isinstance(s, ast.ClassDef):
            n = self._op(f"class {s.name}")
            self.g.link(last, n)
            return n

        # ---- Return / Raise / Break / Continue ----
        elif isinstance(s, ast.Return):
            n = self._op(f"return {self._label_expr(s.value)}")
            self.g.link(last, n)
            self.g.link(n, self.g.end)  # show termination
            return n

        elif isinstance(s, ast.Raise):
            n = self._op(f"raise {self._label_expr(s.exc)}")
            self.g.link(last, n)
            self.g.link(n, self.g.end)
            return n

        elif isinstance(s, ast.Break):
            n = self._op("break")
            self.g.link(last, n)
            return n

        elif isinstance(s, ast.Continue):
            n = self._op("continue")
            self.g.link(last, n)
            return n

        # ---- Match/Case (Py 3.10+) ----
        elif hasattr(ast, "Match") and isinstance(s, getattr(ast, "Match")):
            head = self._op(f"match {self._label_expr(s.subject)}")
            self.g.link(last, head)
            exits = []
            for case in s.cases:
                pat = getattr(case, "pattern", None)
                guard = getattr(case, "guard", None)
                label = f"case {self._label_expr(pat)}"
                if guard is not None:
                    label += f" if {self._label_expr(guard)}"
                branch = self._op(label)
                self.g.link(head, branch)
                exits.append(self._build_block(case.body, branch))
            merge = self._op("after match")
            for e in exits:
                self.g.link(e, merge)
            return merge

        # ---- Simple statements (import, assign, expr, etc.) ----
        else:
            txt = type(s).__name__
            try:
                if isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Import, ast.ImportFrom)):
                    txt = ast.unparse(s).strip()
                elif isinstance(s, ast.Expr):
                    txt = ast.unparse(s.value).strip()
            except Exception:
                pass
            n = self._op(txt)
            self.g.link(last, n)
            return n

# ---------------------------- Project scanner ---------------------------- #

def scan_py_files(root: Path, ignore: List[str], max_files: int) -> List[Path]:
    files: List[Path] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        # apply ignore filters on directories early to prune traversal
        norm_dirpath = str(Path(dirpath))
        if any(token and token in norm_dirpath for token in ignore):
            continue
        # Optional: prune dirnames in-place for performance
        pruned = []
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if any(token and token in full for token in ignore):
                continue
            pruned.append(d)
        dirnames[:] = pruned

        for f in filenames:
            if f.endswith(".py"):
                files.append(Path(dirpath) / f)
                if len(files) >= max_files:
                    return files
    return files

def build_for_file(path: Path) -> List[Tuple[str, str]]:
    """Return list of (title, mermaid_text) for module-level and each function."""
    src = path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(src, filename=str(path))
    out: List[Tuple[str, str]] = []

    # module-level flow
    b = Builder(title=f"{path.name} (module)")
    g = b.build_module(tree)
    out.append((g.title, g.to_mermaid()))

    # functions (sync + async)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, getattr(ast, "AsyncFunctionDef", ast.FunctionDef))):
            bf = Builder(title=f"{path.name}::{node.name}")
            gf = bf.build_function(node)
            out.append((gf.title, gf.to_mermaid()))
    return out

# ---------------------------- Output writers ---------------------------- #

def write_markdown(root: Path, files: List[Path], charts_by_file: Dict[Path, List[Tuple[str, str]]], out_path: Path):
    lines: List[str] = []
    lines.append(f"# Mermaid Flowcharts for: {root}")
    for i, f in enumerate(files, 1):
        rel = f.relative_to(root)
        lines.append(f"\n\n## {i}. {rel}")
        for title, mer in charts_by_file[f]:
            lines.append(f"\n### {title}\n")
            lines.append("```mermaid")
            lines.append(mer)
            lines.append("```")
    out_path.write_text("\n".join(lines), encoding="utf-8")

def _read_mermaid_js(mermaid_zip: Optional[Path], mermaid_js: Optional[Path]) -> Optional[str]:
    # Priority 1: zip -> mermaid.min.js
    if mermaid_zip:
        zpath = Path(mermaid_zip)
        if zpath.exists():
            with ZipFile(zpath) as z:
                # prefer mermaid.min.js
                cand = [n for n in z.namelist() if n.endswith("mermaid.min.js")]
                if not cand:
                    cand = [n for n in z.namelist() if n.endswith("mermaid.js")]
                if cand:
                    with z.open(cand[0]) as fh:
                        return fh.read().decode("utf-8", errors="ignore")
    # Priority 2: direct JS path
    if mermaid_js:
        jpath = Path(mermaid_js)
        if jpath.exists():
            return jpath.read_text(encoding="utf-8", errors="ignore")
    return None

def write_html(root: Path,
               files: List[Path],
               charts_by_file: Dict[Path, List[Tuple[str, str]]],
               out_path: Path,
               mermaid_zip: Optional[Path],
               mermaid_js: Optional[Path],
               title: Optional[str] = None,
               theme: str = "default",
               collapse: bool = False):
    page_title = title or f"Mermaid Flowcharts for: {root}"
    # Build TOC
    toc_lines = []
    for i, f in enumerate(files, 1):
        rel = f.relative_to(root)
        toc_lines.append(f'<li><a href="#f{i}">{i}. {html.escape(str(rel))}</a></li>')
    toc_html = "<ul>" + "\n".join(toc_lines) + "</ul>"

    # Sections
    sections = []
    for i, f in enumerate(files, 1):
        rel = f.relative_to(root)
        section_head = f'<h2 id="f{i}">{i}. {html.escape(str(rel))}</h2>'
        inner = []
        for title, mer in charts_by_file[f]:
            safe_title = html.escape(title)
            block = f'<h3>{safe_title}</h3>\n<pre class="mermaid">{html.escape(mer, quote=False)}</pre>'
            if collapse:
                block = f'<details><summary>{safe_title}</summary>\n<pre class="mermaid">{html.escape(mer, quote=False)}</pre>\n</details>'
            inner.append(block)
        sections.append(section_head + "\n" + "\n".join(inner))
    body_html = "\n\n".join(sections)

    # Mermaid JS (embedded or CDN fallback)
    js_inline = _read_mermaid_js(mermaid_zip, mermaid_js)
    if js_inline is None:
        # Minimal fallback; requires internet
        js_tag = '<script defer src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>'
    else:
        js_tag = f"<script>{js_inline}</script>"

    html_out = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(page_title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 2rem; }}
    h1, h2, h3 {{ line-height: 1.25; }}
    nav.toc {{ background: #f5f5f5; padding: 1rem; border-radius: 8px; }}
    pre.mermaid {{ background: #fff; padding: 0.5rem; border: 1px solid #ddd; border-radius: 6px; overflow: auto; }}
    details > summary {{ cursor: pointer; font-weight: 600; }}
    .meta {{ color: #555; font-size: 0.9rem; margin-top: .5rem; }}
  </style>
  {js_tag}
  <script>
    // Initialize Mermaid 11
    document.addEventListener("DOMContentLoaded", function() {{
      if (window.mermaid && mermaid.initialize) {{
        mermaid.initialize({{
          startOnLoad: true,
          theme: "{theme}",
          securityLevel: "strict",
          flowchart: {{ htmlLabels: false }}
        }});
      }}
    }});
  </script>
</head>
<body>
  <h1>{html.escape(page_title)}</h1>
  <div class="meta">Generated by py2mermaid_v2. Mermaid runtime: {'embedded' if js_inline else 'CDN fallback'}.</div>
  <nav class="toc">
    <h2>Table of Contents</h2>
    {toc_html}
  </nav>
  {body_html}
</body>
</html>
"""
    out_path.write_text(html_out, encoding="utf-8")

# ---------------------------- CLI ---------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="project folder to scan")
    ap.add_argument("--out", default="mermaid.md", help="output Markdown file (when format includes md)")
    ap.add_argument("--html-out", default="mermaid.html", help="output HTML file (when format includes html)")
    ap.add_argument("--format", choices=["md", "html", "both"], default="md", help="output format")
    ap.add_argument("--max-files", type=int, default=500, help="max number of python files to process")
    ap.add_argument("--ignore", default="venv,.venv,site-packages,__pycache__,.git,.hg,.mypy_cache,.pytest_cache",
                    help="comma-separated substrings to ignore in paths")
    ap.add_argument("--mermaid-zip", default=None, help="path to mermaid-11.x zip (will embed mermaid.min.js)")
    ap.add_argument("--mermaid-js", default=None, help="path to mermaid.min.js (if not using zip)")
    ap.add_argument("--title", default=None, help="override page title in HTML")
    ap.add_argument("--theme", default="default", help="Mermaid theme for HTML output")
    ap.add_argument("--collapse", action="store_true", help="collapse each function/module chart in HTML")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    ignore = [s.strip() for s in args.ignore.split(",") if s.strip()]
    files = scan_py_files(root, ignore, args.max_files)

    if not files:
        print("No .py files found.", file=sys.stderr)
        sys.exit(1)

    charts_by_file: Dict[Path, List[Tuple[str, str]]] = {}
    for f in files:
        try:
            charts_by_file[f] = build_for_file(f)
        except SyntaxError as e:
            print(f"[skip] {f} syntax error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[skip] {f} error: {e}", file=sys.stderr)

    if args.format in ("md", "both"):
        md_out = Path(args.out)
        write_markdown(root, files, charts_by_file, md_out)
        print(f"Wrote {md_out} with {len(files)} file(s).")

    if args.format in ("html", "both"):
        html_out = Path(args.html_out)
        mermaid_zip = Path(args.mermaid_zip) if args.mermaid_zip else None
        mermaid_js = Path(args.mermaid_js) if args.mermaid_js else None
        write_html(root, files, charts_by_file, html_out, mermaid_zip, mermaid_js, args.title, args.theme, args.collapse)
        print(f"Wrote {html_out} with {len(files)} file(s).")

if __name__ == "__main__":
    main()
