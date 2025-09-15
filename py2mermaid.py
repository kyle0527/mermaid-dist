
#!/usr/bin/env python3

"""
py2mermaid — Generate Mermaid flowcharts from a Python project folder.

Usage:
  python py2mermaid.py /path/to/project --out mermaid.md [--max-files 500] [--ignore "venv,.venv,site-packages,__pycache__"]

What it does:
- Recursively scans for *.py under the given folder
- For each file, parses functions and top-level flow (simplified)
- Emits Mermaid `flowchart TD` for each function and a "module flow" for top-level
- Focuses on if/elif/else, for/while, try/except/finally, return/break/continue

Limitations (by design for speed and robustness):
- This is a *flowchart synthesizer*, not a precise compiler CFG.
- It does not resolve dynamic features (eval/exec, dynamic imports, decorators semantics).
- It summarizes complex expressions/statements into short labels.

Author: ChatGPT
License: MIT
"""

import os, ast, sys, argparse
from pathlib import Path
from typing import List, Tuple, Dict, Optional

# ---------------------------- Core CFG builder ---------------------------- #

class Node:
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

    def to_mermaid(self) -> str:
        lines = ["flowchart TD"]
        def fmt(n: Node) -> str:
            text = n.label.replace("`", "\\`").replace("\n", "\\n")
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
            for m in n.nexts:
                # annotate edges from cond nodes for first two branches
                if n.kind == "cond":
                    # best-effort: first child True, second False, rest unlabeled
                    idx = n.nexts.index(m)
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

    def build_module(self, node: ast.AST) -> Graph:
        last = self.g.start
        last = self._build_block(node.body, last)
        self.g.link(last, self.g.end)
        return self.g

    def build_function(self, func: ast.FunctionDef) -> Graph:
        title = f"{func.name}({', '.join(a.arg for a in func.args.args)})"
        self.g = Graph(title)
        last = self.g.start
        last = self._build_block(func.body, last)
        self.g.link(last, self.g.end)
        return self.g

    # -------------------- block/statement synthesizers -------------------- #

    def _build_block(self, stmts: List[ast.stmt], last: Node) -> Node:
        for s in stmts:
            last = self._build_stmt(s, last)
        return last

    def _label_expr(self, expr: Optional[ast.AST]) -> str:
        try:
            return ast.unparse(expr)  # py>=3.9
        except Exception:
            return expr.__class__.__name__ if expr else ""

    def _op(self, text: str) -> Node:
        return self.g.add("op", text)

    def _cond(self, text: str) -> Node:
        return self.g.add("cond", text)

    def _build_stmt(self, s: ast.stmt, last: Node) -> Node:
        if isinstance(s, ast.If):
            cond = self._cond(f"if {self._label_expr(s.test)}")
            self.g.link(last, cond)
            true_tail = self._build_block(s.body, cond)
            false_head = cond
            if s.orelse:
                # elif chain: fold into nested if
                false_tail = self._build_block(s.orelse, cond)
                # merge
                merge = self._op("merge")
                self.g.link(true_tail, merge)
                self.g.link(false_tail, merge)
                return merge
            else:
                merge = self._op("merge")
                self.g.link(true_tail, merge)
                self.g.link(cond, merge)  # false fall-through
                return merge

        elif isinstance(s, ast.For):
            hdr = self._cond(f"for {self._label_expr(s.target)} in {self._label_expr(s.iter)}")
            self.g.link(last, hdr)
            body_tail = self._build_block(s.body, hdr)
            self.g.link(body_tail, hdr)  # loop back
            merge = self._op("after for")
            self.g.link(hdr, merge)  # false branch (no iterations)
            return merge

        elif isinstance(s, ast.While):
            hdr = self._cond(f"while {self._label_expr(s.test)}")
            self.g.link(last, hdr)
            body_tail = self._build_block(s.body, hdr)
            self.g.link(body_tail, hdr)  # loop back
            merge = self._op("after while")
            self.g.link(hdr, merge)  # false branch
            return merge

        elif isinstance(s, ast.Try):
            hdr = self._op("try")
            self.g.link(last, hdr)
            try_tail = self._build_block(s.body, hdr)
            exits = [try_tail]
            for h in s.handlers:
                lab = f"except {self._label_expr(h.type) or ''}"
                hnode = self._op(lab.strip())
                self.g.link(hdr, hnode)
                exits.append(self._build_block(h.body, hnode))
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

        elif isinstance(s, ast.FunctionDef):
            # function nested definition: represent as op
            n = self._op(f"def {s.name}(...)")
            self.g.link(last, n)
            return n

        elif isinstance(s, ast.Return):
            n = self._op(f"return {self._label_expr(s.value)}")
            self.g.link(last, n)
            # connect to graph end to show termination; also allow fallthrough chaining
            self.g.link(n, self.g.end)
            return n  # next statements (if any) will still chain for linearity

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

        else:
            # generic simple statement
            txt = type(s).__name__
            if isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                try:
                    txt = ast.unparse(s).strip()
                except Exception:
                    txt = txt
            elif isinstance(s, ast.Expr):
                try:
                    txt = ast.unparse(s.value).strip()
                except Exception:
                    txt = "Expr"
            n = self._op(txt)
            self.g.link(last, n)
            return n

# ---------------------------- Project scanner ---------------------------- #

def scan_py_files(root: Path, ignore: List[str], max_files: int) -> List[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # apply ignore filters on directories
        dn = Path(dirpath).name
        if any(token and token in dirpath for token in ignore):
            continue
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

    # functions
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            bf = Builder(title=f"{path.name}::{node.name}")
            gf = bf.build_function(node)
            out.append((gf.title, gf.to_mermaid()))
    return out

# ---------------------------- CLI ---------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="project folder to scan")
    ap.add_argument("--out", default="mermaid.md", help="output Markdown file")
    ap.add_argument("--max-files", type=int, default=500, help="max number of python files to process")
    ap.add_argument("--ignore", default="venv,.venv,site-packages,__pycache__,.git,.hg,.mypy_cache,.pytest_cache",
                    help="comma-separated substrings to ignore in paths")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    ignore = [s.strip() for s in args.ignore.split(",") if s.strip()]
    files = scan_py_files(root, ignore, args.max_files)

    if not files:
        print("No .py files found.", file=sys.stderr)
        sys.exit(1)

    lines: List[str] = []
    lines.append(f"# Mermaid Flowcharts for: {root}")
    for i, f in enumerate(files, 1):
        try:
            charts = build_for_file(f)
        except SyntaxError as e:
            print(f"[skip] {f} syntax error: {e}", file=sys.stderr)
            continue
        lines.append(f"\n\n## {i}. {f.relative_to(root)}")
        for title, mer in charts:
            lines.append(f"\n### {title}\n")
            lines.append("```mermaid")
            lines.append(mer)
            lines.append("```")

    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {args.out} with {len(files)} file(s).")

if __name__ == "__main__":
    main()
