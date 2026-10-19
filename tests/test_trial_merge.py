from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import trial_merge

SCRIPT = Path(trial_merge.__file__)
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_COUNT": "3",
    "GIT_CONFIG_KEY_0": "protocol.file.allow",
    "GIT_CONFIG_VALUE_0": "always",
    "GIT_CONFIG_KEY_1": "uploadpack.allowFilter",
    "GIT_CONFIG_VALUE_1": "true",
    "GIT_CONFIG_KEY_2": "uploadpack.allowAnySHA1InWant",
    "GIT_CONFIG_VALUE_2": "true",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def init(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit(path, files, "base")
    return path


def add_submodule(repo: Path, url: Path, path: str) -> None:
    git(repo, "submodule", "add", "-q", url.as_uri(), path)
    git(repo, "commit", "-q", "-m", f"add {path}")


def branch(repo: Path, name: str, start: str = "main") -> None:
    git(repo, "checkout", "-q", "-B", name, start)
    git(repo, "submodule", "update", "-q", "--init", "--recursive")


def pin(repo: Path, path: str, sha: str) -> str:
    git(repo / path, "fetch", "-q", "origin")
    git(repo / path, "checkout", "-q", "--detach", sha)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", f"pin {path}")
    return git(repo, "rev-parse", "HEAD")


def chain(tmp_path: Path) -> tuple[Path, Path, Path]:
    leaf = init(tmp_path / "leaf", {"a.txt": "a\n"})
    mid = init(tmp_path / "mid", {"m.txt": "m\n"})
    add_submodule(mid, leaf, "deps/leaf")
    top = init(tmp_path / "top", {"t.txt": "t\n"})
    add_submodule(top, mid, "mid")
    git(top, "submodule", "update", "-q", "--init", "--recursive")
    return leaf, mid, top


def run(tmp_path: Path, top: Path, head: str, *args: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", top.as_uri(), str(work)], check=True, env=GIT_ENV)
    git(work, "checkout", "-q", "--detach", head)
    result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(work), *args], capture_output=True, text=True, env=GIT_ENV, check=False)
    return result, work


def test_head_that_contains_the_base_is_tested_as_is(tmp_path: Path) -> None:
    _, _, top = chain(tmp_path)
    branch(top, "feature")
    head = commit(top, {"f.txt": "f\n"}, "feature")
    result, work = run(tmp_path, top, head, "--base", "main")
    assert result.returncode == 0, result.stderr
    assert git(work, "rev-parse", "HEAD") == head


def test_changes_on_both_sides_merge_with_the_base_as_first_parent(tmp_path: Path) -> None:
    _, _, top = chain(tmp_path)
    branch(top, "feature")
    head = commit(top, {"f.txt": "f\n"}, "feature")
    branch(top, "main")
    base = commit(top, {"b.txt": "b\n"}, "base side")
    output = tmp_path / "output"
    result, work = run(tmp_path, top, head, "--base", "main")
    assert result.returncode == 0, result.stderr
    assert git(work, "rev-parse", "HEAD^1", "HEAD^2").split() == [base, head]
    assert (work / "f.txt").exists() and (work / "b.txt").exists()
    result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(work)], capture_output=True, text=True, env={**GIT_ENV, "GITHUB_OUTPUT": str(output)}, check=False)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"base=\nhead={git(work, 'rev-parse', 'HEAD')}\n"


def test_pin_that_contains_the_other_pin_wins(tmp_path: Path) -> None:
    _, mid, top = chain(tmp_path)
    older = commit(mid, {"m.txt": "m1\n"}, "older")
    newer = commit(mid, {"m.txt": "m2\n"}, "newer")
    branch(top, "feature")
    head = pin(top, "mid", newer)
    branch(top, "main")
    pin(top, "mid", older)
    result, work = run(tmp_path, top, head, "--base", "main", "--submodules", "recursive")
    assert result.returncode == 0, result.stderr
    assert git(work, "rev-parse", "HEAD:mid") == newer
    assert (work / "mid" / "m.txt").read_text() == "m2\n"


def test_diverged_pins_merge_at_every_depth(tmp_path: Path) -> None:
    leaf, mid, top = chain(tmp_path)
    leaf_base = commit(leaf, {"base.txt": "b\n"}, "leaf base side")
    branch(leaf, "feature", "main~1")
    leaf_head = commit(leaf, {"head.txt": "h\n"}, "leaf feature side")
    mid_base = pin(mid, "deps/leaf", leaf_base)
    branch(mid, "feature", "main~1")
    mid_head = pin(mid, "deps/leaf", leaf_head)
    branch(top, "feature")
    head = pin(top, "mid", mid_head)
    branch(top, "main")
    pin(top, "mid", mid_base)
    result, work = run(tmp_path, top, head, "--base", "main", "--submodules", "recursive")
    assert result.returncode == 0, result.stderr
    mid_pin = git(work, "rev-parse", "HEAD:mid")
    assert git(work / "mid", "rev-parse", f"{mid_pin}^1", f"{mid_pin}^2").split() == [mid_base, mid_head]
    leaf_pin = git(work / "mid", "rev-parse", f"{mid_pin}:deps/leaf")
    assert git(work / "mid" / "deps" / "leaf", "rev-parse", f"{leaf_pin}^1", f"{leaf_pin}^2").split() == [leaf_base, leaf_head]
    assert (work / "mid" / "deps" / "leaf" / "base.txt").exists() and (work / "mid" / "deps" / "leaf" / "head.txt").exists()
    status = subprocess.run(["git", "-C", str(work), "submodule", "status", "--recursive"], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.splitlines()
    assert [line[0] for line in status] == [" ", " "], status
    git(work, "submodule", "update", "--init", "--recursive")
    assert git(work, "status", "--porcelain", "--ignore-submodules=none") == ""


def test_conflict_inside_a_submodule_fails_naming_its_path(tmp_path: Path) -> None:
    leaf, mid, top = chain(tmp_path)
    leaf_base = commit(leaf, {"a.txt": "base\n"}, "leaf base side")
    branch(leaf, "feature", "main~1")
    leaf_head = commit(leaf, {"a.txt": "head\n"}, "leaf feature side")
    mid_base = pin(mid, "deps/leaf", leaf_base)
    branch(mid, "feature", "main~1")
    mid_head = pin(mid, "deps/leaf", leaf_head)
    branch(top, "feature")
    head = pin(top, "mid", mid_head)
    branch(top, "main")
    pin(top, "mid", mid_base)
    result, _ = run(tmp_path, top, head, "--base", "main")
    assert result.returncode == 1
    assert "::error::merge conflict in mid/deps/leaf/a.txt" in result.stdout.splitlines()


def test_conflict_in_the_superproject_fails_naming_its_path(tmp_path: Path) -> None:
    _, _, top = chain(tmp_path)
    branch(top, "feature")
    head = commit(top, {"t.txt": "head\n"}, "feature")
    branch(top, "main")
    commit(top, {"t.txt": "base\n"}, "base side")
    result, _ = run(tmp_path, top, head, "--base", "main")
    assert result.returncode == 1
    assert "::error::merge conflict in t.txt" in result.stdout.splitlines()
