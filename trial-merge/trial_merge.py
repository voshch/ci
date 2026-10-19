"""Check out the merge of HEAD into a base branch, merging conflicting submodule pins at every depth."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

GITLINK = "160000"
IDENTITY = ("-c", "user.name=trial merge", "-c", "user.email=trial-merge@localhost")


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), "-c", "safe.directory=*", *IDENTITY, *args], capture_output=True, text=True, check=False)
    if check and result.returncode:
        sys.exit(f"git {' '.join(args)} failed in {repo}:\n{result.stderr.strip()}")
    return result


def out(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def is_ancestor(repo: Path, old: str, new: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", old, new, check=False).returncode == 0


def unmerged(repo: Path) -> dict[str, dict[int, tuple[str, str]]]:
    entries: dict[str, dict[int, tuple[str, str]]] = {}
    for record in filter(None, git(repo, "ls-files", "-u", "-z").stdout.split("\0")):
        info, path = record.split("\t", 1)
        mode, sha, stage = info.split()
        entries.setdefault(path, {})[int(stage)] = (mode, sha)
    return entries


def pending(repo: Path) -> bool:
    return git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0


def merge(repo: Path, ours: str, theirs: str, prefix: str) -> list[str]:
    """Merge theirs into ours, leave the merge uncommitted and return the conflicts no pin move resolves."""
    git(repo, "checkout", "-q", "--detach", ours)
    result = git(repo, "merge", "-q", "--no-ff", "--no-commit", theirs, check=False)
    entries = unmerged(repo)
    if result.returncode and not entries:
        sys.exit(f"git merge {theirs} failed in {repo}:\n{result.stderr.strip()}")
    pins = {path: (stages[2][1], stages[3][1]) for path, stages in entries.items() if {2, 3} <= stages.keys() and all(mode == GITLINK for mode, _ in stages.values())}
    conflicts = [prefix + path for path in sorted(entries.keys() - pins.keys())]
    for path, (pin_ours, pin_theirs) in sorted(pins.items()):
        conflicts += merge_pin(repo, path, pin_ours, pin_theirs, prefix)
    return conflicts


def merge_pin(repo: Path, path: str, ours: str, theirs: str, prefix: str) -> list[str]:
    """Point the submodule at path to a commit holding both pins and stage it."""
    git(repo, "submodule", "init", "--", path)
    names = (line.split(" ", 1) for line in out(repo, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$").splitlines())
    name = next(key.removeprefix("submodule.").removesuffix(".path") for key, value in names if value == path)
    url = out(repo, "config", f"submodule.{name}.url")
    sub = repo / path
    git(repo, "clone", "-q", "--filter=blob:none", "--no-checkout", url, path)
    git(sub, "fetch", "-q", "origin", ours, theirs)
    conflicts: list[str] = []
    if is_ancestor(sub, ours, theirs):
        git(sub, "checkout", "-q", "--detach", theirs)
    elif is_ancestor(sub, theirs, ours):
        git(sub, "checkout", "-q", "--detach", ours)
    else:
        conflicts = merge(sub, ours, theirs, f"{prefix}{path}/")
        if not conflicts:
            git(sub, "commit", "-q", "-m", f"trial merge {theirs} into {ours}")
    if conflicts:
        return conflicts
    git(repo, "add", "--", path)
    git(repo, "submodule", "absorbgitdirs", "--", path)
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path())
    parser.add_argument("--base", default="", help="branch on origin to merge HEAD into, empty to keep HEAD as is")
    parser.add_argument("--submodules", choices=("false", "true", "recursive"), default="false", help="submodules to check out after the merge")
    args = parser.parse_args()

    head = out(args.repo, "rev-parse", "HEAD")
    base = ""
    if args.base:
        git(args.repo, "fetch", "-q", "origin", args.base)
        base = out(args.repo, "rev-parse", "FETCH_HEAD")
    if not base:
        print(f"testing {head} as is")
    elif is_ancestor(args.repo, base, head):
        print(f"{head} already contains {args.base} at {base}")
    else:
        conflicts = merge(args.repo, base, head, "")
        if conflicts:
            for path in conflicts:
                print(f"::error::merge conflict in {path}")
            print(f"{head} does not merge into {args.base} at {base}: {len(conflicts)} conflicts outside submodule pins")
            return 1
        if pending(args.repo):
            git(args.repo, "commit", "-q", "-m", f"trial merge {head} into {args.base}")
        print(f"testing the merge of {head} into {args.base} at {base}")
    if args.submodules != "false":
        git(args.repo, "submodule", "update", "--init", *(["--recursive"] if args.submodules == "recursive" else []))
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"base={base}\nhead={head}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
