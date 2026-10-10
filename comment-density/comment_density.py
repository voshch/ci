#!/usr/bin/env python3
"""Compare the comment density of the lines a change adds with the repo baseline."""

from __future__ import annotations

import argparse
import ast
import io
import os
import re
import subprocess
import sys
import tokenize
import warnings
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MIN_BASELINE_LINES = 500
MIN_COMMENTS = 5
MAX_ANNOTATIONS = 10

DIRECTIVE = re.compile(r"#\s*(noqa|type:|pragma|fmt:|ruff:|pylint:|mypy:|isort:|!|-\*-)")
HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

Lines = tuple[set[int], set[int]]


def _nonblank(text: str) -> set[int]:
    return {number for number, line in enumerate(text.split("\n"), 1) if line.strip()}


def python_lines(text: str) -> Lines | None:
    """Non-blank lines and those with a comment or a docstring line past its first, None for unparsable source."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, ValueError, tokenize.TokenError):
        return None
    commented = {token.start[0] for token in tokens if token.type == tokenize.COMMENT and not DIRECTIVE.match(token.string)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            commented.update(range(first.lineno + 1, (first.end_lineno or first.lineno) + 1))
    nonblank = _nonblank(text)
    return nonblank, commented & nonblank


def hash_lines(text: str) -> Lines:
    """Non-blank lines and the full-line # comments among them."""
    nonblank, commented = set(), set()
    for number, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if not stripped:
            continue
        nonblank.add(number)
        if stripped.startswith("#") and not stripped.startswith("#!"):
            commented.add(number)
    return nonblank, commented


DOC_LINE = ("///", "//!")
DOC_BLOCK = ("/**", "/*!")


def _c_comments(text: str) -> Iterator[tuple[int, str]]:
    """Every // and /* */ comment outside string literals, as (first line, text)."""
    line, i, quote = 1, 0, ""
    while i < len(text):
        char, pair = text[i], text[i : i + 2]
        if quote:
            if char == "\\":
                if text[i + 1 : i + 2] == "\n":
                    line += 1
                i += 1
            elif char in (quote, "\n"):
                quote = ""
                if char == "\n":
                    line += 1
        elif char == "\n":
            line += 1
        elif pair in ("//", "/*"):
            end = text.find("\n", i) if pair == "//" else text.find("*/", i + 2) + 2
            if end < (i if pair == "//" else i + 4):
                end = len(text)
            yield line, text[i:end]
            line += text.count("\n", i, end)
            i = end
            continue
        elif char in "\"'":
            quote = char
        i += 1


def c_lines(text: str) -> Lines:
    """Non-blank lines and those with a // or /* */ comment, a doc comment's first line and bare delimiters excepted."""
    source = text.split("\n")
    commented: set[int] = set()
    run = -1
    for start, body in _c_comments(text):
        if body.startswith("//"):
            if not body.startswith(DOC_LINE) or body.startswith("////"):
                commented.add(start)
            elif source[start - 1].lstrip().startswith("//"):
                if start == run + 1:
                    commented.add(start)
                run = start
        elif not body.startswith(DOC_BLOCK) or body.startswith("/**/"):
            commented.update(range(start, start + body.count("\n") + 1))
        else:
            inner = body[3:-2] if body.endswith("*/") else body[3:]
            content = [start + offset for offset, segment in enumerate(inner.split("\n")) if segment.strip().lstrip("*<").strip()]
            commented.update(content[1:])
    nonblank = _nonblank(text)
    return nonblank, commented & nonblank


@dataclass(frozen=True)
class Language:
    """One supported language: how its files are recognized, which baseline family it falls back to, how its lines are classified."""

    name: str
    family: str
    classify: Callable[[str], Lines | None]
    suffixes: tuple[str, ...] = ()
    names: tuple[str, ...] = ()


LANGUAGES = (
    Language("python", "code", python_lines, suffixes=(".py",)),
    Language("c++", "code", c_lines, suffixes=(".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp")),
    Language("shell", "code", hash_lines, suffixes=(".sh", ".bash")),
    Language("cmake", "code", hash_lines, suffixes=(".cmake",), names=("CMakeLists.txt",)),
    Language("docker", "code", hash_lines, names=("Dockerfile",)),
    Language("yaml", "config", hash_lines, suffixes=(".yaml", ".yml")),
    Language("toml", "config", hash_lines, suffixes=(".toml",)),
)
BY_NAME = {lang.name: lang for lang in LANGUAGES}
BY_SUFFIX = {suffix: lang.name for lang in LANGUAGES for suffix in lang.suffixes}
BY_FILE = {name: lang.name for lang in LANGUAGES for name in lang.names}


def language(path: str) -> str | None:
    name = PurePosixPath(path)
    return BY_FILE.get(name.name) or BY_SUFFIX.get(name.suffix)


def classify(lang: str, text: str) -> Lines | None:
    return BY_NAME[lang].classify(text)


class GitError(RuntimeError):
    pass


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _run(self, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", "-C", str(self.path), "-c", "core.quotepath=off", *args], input=data, capture_output=True, check=False)

    def git(self, *args: str) -> str:
        result = self._run(*args)
        if result.returncode:
            raise GitError(result.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
        return result.stdout.decode(errors="replace")

    def has(self, commit: str) -> bool:
        return self._run("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0

    def blobs(self, specs: list[str]) -> list[str | None]:
        """File contents for object names, None where the object is missing."""
        if not specs:
            return []
        out = self._run("cat-file", "--batch", data="\n".join(specs).encode() + b"\n").stdout
        texts: list[str | None] = []
        pos = 0
        for _ in specs:
            end = out.index(b"\n", pos)
            header = out[pos:end].split()
            pos = end + 1
            if header[-1] == b"missing":
                texts.append(None)
                continue
            size = int(header[2])
            texts.append(out[pos : pos + size].decode(errors="replace"))
            pos += size + 1
        return texts


@dataclass
class Count:
    lines: int = 0
    commented: int = 0

    @property
    def density(self) -> float:
        return self.commented / self.lines if self.lines else 0.0


@dataclass
class FileStat:
    path: str
    lang: str
    lines: int
    commented: int
    first: int

    @property
    def density(self) -> float:
        return self.commented / self.lines


@dataclass
class Scope:
    """One repository of the change: the root, or a submodule whose pin moved."""

    name: str
    baseline: dict[str, Count] = field(default_factory=dict)
    files: list[FileStat] = field(default_factory=list)
    skipped: str = ""
    lost: bool = False

    def totals(self) -> dict[str, Count]:
        totals: dict[str, Count] = defaultdict(Count)
        for stat in self.files:
            totals[stat.lang].lines += stat.lines
            totals[stat.lang].commented += stat.commented
        return dict(sorted(totals.items()))


def baseline(repo: Repo, tree: str) -> dict[str, Count]:
    entries = []
    for entry in repo.git("ls-tree", "-r", "-z", tree).split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, _, sha = meta.split()
        lang = language(path)
        if lang and mode.startswith("100"):
            entries.append((lang, sha))
    counts: dict[str, Count] = defaultdict(Count)
    for (lang, _), text in zip(entries, repo.blobs([sha for _, sha in entries]), strict=True):
        lines = None if text is None else classify(lang, text)
        if lines is not None:
            counts[lang].lines += len(lines[0])
            counts[lang].commented += len(lines[1])
    return dict(counts)


def added_lines(repo: Repo, base: str, head: str) -> dict[str, set[int]]:
    """Line numbers at head that the change adds, by path."""
    added: dict[str, set[int]] = {}
    path: str | None = None
    pending = 0
    for line in repo.git("diff", "-U0", "-M", "--no-color", "--no-ext-diff", "--submodule=short", base, head).split("\n"):
        if pending:
            if not line.startswith("\\"):
                pending -= 1
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif match := HUNK.match(line):
            old = 1 if match[1] is None else int(match[1])
            new = 1 if match[3] is None else int(match[3])
            pending = old + new
            if path is not None and new:
                start = int(match[2])
                added.setdefault(path, set()).update(range(start, start + new))
    return added


def gitlinks(repo: Repo, base: str, head: str) -> list[tuple[str, str, str]]:
    """Submodule pins the change moves, as (path, old commit, new commit)."""
    links = []
    for line in repo.git("diff", "--raw", "--no-abbrev", "--no-renames", "--ignore-submodules=none", base, head).split("\n"):
        if not line.startswith(":"):
            continue
        meta, path = line.split("\t", 1)
        old_mode, new_mode, old, new, _ = meta[1:].split()
        if "160000" in (old_mode, new_mode):
            links.append((path, old, new))
    return links


def scan(repo: Repo, base: str, head: str, prefix: str = "") -> Iterator[Scope]:
    """The change from base to head in repo, then in every submodule whose pin it moves."""
    name = prefix.rstrip("/") or "."
    try:
        fork = repo.git("merge-base", base, head).strip()
    except GitError:
        yield Scope(name, skipped="no merge base, history too shallow", lost=True)
        return
    added = added_lines(repo, fork, head)
    paths = [path for path in added if language(path)]
    scope = Scope(name, baseline(repo, fork))
    for path, text in zip(paths, repo.blobs([f"{head}:{path}" for path in paths]), strict=True):
        lang = language(path)
        lines = None if text is None or lang is None else classify(lang, text)
        if lang is None or lines is None:
            continue
        mine = added[path] & lines[0]
        hits = mine & lines[1]
        if mine:
            scope.files.append(FileStat(prefix + path, lang, len(mine), len(hits), min(hits or mine)))
    yield scope
    for path, old, new in gitlinks(repo, fork, head):
        sub = Repo(repo.path / path)
        if not new.strip("0"):
            continue
        if not (sub.path / ".git").exists():
            yield Scope(prefix + path, skipped="not checked out")
        elif not old.strip("0"):
            yield Scope(prefix + path, skipped="new submodule, no baseline")
        elif missing := [commit for commit in (old, new) if not sub.has(commit)]:
            yield Scope(prefix + path, skipped=f"commit {missing[0][:12]} not fetched", lost=True)
        else:
            yield from scan(sub, old, new, prefix + path + "/")


@dataclass
class Row:
    scope: str
    label: str
    count: Count
    base: float
    against: str
    over: bool
    first: int = 0

    @property
    def ratio(self) -> str:
        return f"{self.count.density / self.base:.1f}x" if self.base else "-"


@dataclass(frozen=True)
class Thresholds:
    ratio: float = 1.5
    floor: float = 0.05
    min_lines: int = 40

    def limit(self, base: float) -> float:
        return max(base * self.ratio, self.floor)

    def over(self, count: Count, base: float) -> bool:
        return count.lines >= self.min_lines and count.commented >= MIN_COMMENTS and count.density > self.limit(base)

    def __str__(self) -> str:
        return f"Warns above {self.ratio:g}x the repo baseline and above {self.floor:.0%}, on {self.min_lines}+ added lines."


def reference(baseline: dict[str, Count], lang: str) -> tuple[str, Count] | None:
    """The baseline a language is judged against: its own when the repo has enough of it, else its family's."""
    own = baseline.get(lang, Count())
    if own.lines >= MIN_BASELINE_LINES:
        return lang, own
    family = BY_NAME[lang].family
    members = [count for member, count in baseline.items() if BY_NAME[member].family == family]
    pooled = Count(sum(count.lines for count in members), sum(count.commented for count in members))
    return (family, pooled) if pooled.lines >= MIN_BASELINE_LINES else None


def verdicts(scopes: list[Scope], thresholds: Thresholds) -> tuple[list[Row], list[Row]]:
    """Per-language totals for every scope and the files over the threshold, most excess comment lines first."""
    totals, files = [], []
    for scope in scopes:
        for lang, count in scope.totals().items():
            found = reference(scope.baseline, lang)
            if found is None:
                totals.append(Row(scope.name, lang, count, 0.0, "-", False))
                continue
            against, base = found
            totals.append(Row(scope.name, lang, count, base.density, against, thresholds.over(count, base.density)))
            for stat in scope.files:
                mine = Count(stat.lines, stat.commented)
                if stat.lang == lang and thresholds.over(mine, base.density):
                    files.append(Row(scope.name, stat.path, mine, base.density, against, True, stat.first))
    files.sort(key=lambda row: row.count.commented - thresholds.limit(row.base) * row.count.lines, reverse=True)
    return totals, files


def _cells(row: Row) -> list[str]:
    return [str(row.count.lines), str(row.count.commented), f"{row.count.density:.1%}", f"{row.base:.1%}" if row.against != "-" else "-", row.against, row.ratio]


def render_text(scopes: list[Scope], totals: list[Row], files: list[Row], thresholds: Thresholds) -> str:
    out = [f"Comment and docstring lines as a share of the non-blank lines a change adds. {thresholds}", ""]
    head = ["scope", "language", "added", "comments", "density", "baseline", "against", "ratio", ""]
    table = [head] + [[row.scope, row.label, *_cells(row), "WARN" if row.over else ""] for row in totals]
    widths = [max(len(line[i]) for line in table) for i in range(len(head))]
    out += ["  ".join(cell.ljust(width) if i in (0, 1, 6) else cell.rjust(width) for i, (cell, width) in enumerate(zip(line, widths, strict=True))).rstrip() for line in table]
    for scope in scopes:
        if scope.skipped:
            out.append(f"{scope.name}: skipped, {scope.skipped}")
    if files:
        out += ["", "Files over the threshold:"]
        out += [f"  {row.count.density:6.1%}  {row.ratio:>6}  {row.count.commented:4d} of {row.count.lines:4d} lines  {row.label}" for row in files]
    else:
        out += ["", "No file over the threshold."]
    return "\n".join(out)


def render_markdown(scopes: list[Scope], totals: list[Row], files: list[Row], thresholds: Thresholds) -> str:
    out = ["## Comment density", "", f"Comment and docstring lines as a share of the non-blank lines this change adds. {thresholds}", ""]
    out += ["| Scope | Language | Added | Comments | Density | Baseline | Against | Ratio | |", "|---|---|---:|---:|---:|---:|---|---:|---|"]
    out += [f"| {row.scope} | {row.label} | {' | '.join(_cells(row))} | {'warn' if row.over else ''} |" for row in totals]
    out += [f"| {scope.name} | skipped: {scope.skipped} | | | | | | | |" for scope in scopes if scope.skipped]
    if files:
        out += ["", "### Files over the threshold", "", "| File | Added | Comments | Density | Baseline | Against | Ratio |", "|---|---:|---:|---:|---:|---|---:|"]
        out += [f"| `{row.label}` | {' | '.join(_cells(row))} |" for row in files]
    return "\n".join(out) + "\n"


def _escape(text: str, prop: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def annotations(scopes: list[Scope], files: list[Row]) -> list[str]:
    out = [f"::warning file={_escape(row.label, True)},line={row.first},title=comment density::{_escape(f'{row.count.density:.0%} of the {row.count.lines} added lines are comments or docstrings, {row.ratio} the {row.base:.1%} {row.against} baseline')}" for row in files[:MAX_ANNOTATIONS]]
    out += [f"::warning title=comment density::{_escape(f'{scope.name} was not checked: {scope.skipped}')}" for scope in scopes if scope.lost]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Counts comment and docstring lines among the non-blank lines the change adds, per language\n"
            f"({', '.join(BY_NAME)}). The first line of a python docstring or a\n"
            "Doxygen comment is free, so one-line summaries never count. Compares that share with the\n"
            "same share over the whole repo at the merge base, per language, or per family (code, config)\n"
            "for a language the repo has under 500 lines of. A submodule whose pin the change moves is measured\n"
            "the same way against its own baseline, when it is checked out with both commits present.\n\n"
            "A warning needs --min-lines added lines, a density above --ratio times the baseline and above\n"
            "--floor, which keeps a near-zero baseline from flagging every commented file.\n\n"
            "Prints a table and the files over the threshold. Under GitHub Actions it also emits warning\n"
            "annotations and a job summary. Exits 0 whatever it finds, 2 on a bad argument.\n\n"
            "example: comment_density.py --base origin/main"
        ),
    )
    parser.add_argument("--base", required=True, metavar="REV", help="revision the change is measured against, e.g. origin/main or a commit")
    parser.add_argument("--head", default="HEAD", metavar="REV", help="revision that contains the change (default: HEAD)")
    parser.add_argument("--repo", default=".", metavar="DIR", type=Path, help="repository to read (default: the current directory)")
    parser.add_argument("--ratio", default=1.5, type=float, metavar="X", help="warn when the added density exceeds X times the baseline (default: 1.5)")
    parser.add_argument("--floor", default=5.0, type=float, metavar="PCT", help="never warn at or below PCT percent density, whatever the ratio (default: 5)")
    parser.add_argument("--min-lines", default=40, type=int, metavar="N", help="judge only files and totals with at least N added non-blank lines (default: 40)")
    if argv is None and len(sys.argv) == 1:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)

    repo = Repo(args.repo)
    for name in ("base", "head"):
        if not repo.has(vars(args)[name]):
            parser.error(f"--{name} {vars(args)[name]!r} is not a commit in {args.repo.resolve()}. Pass a branch, tag or commit that exists there, fetching it first if needed.")
    if args.ratio <= 0 or args.min_lines < 1 or not 0 <= args.floor < 100:
        parser.error("--ratio must be above 0, --floor from 0 to under 100 and --min-lines at least 1")
    thresholds = Thresholds(args.ratio, args.floor / 100, args.min_lines)

    scopes = list(scan(repo, args.base, args.head))
    totals, files = verdicts(scopes, thresholds)
    print(render_text(scopes, totals, files, thresholds))
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("\n".join(annotations(scopes, files)))
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(render_markdown(scopes, totals, files, thresholds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
