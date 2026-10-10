from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import comment_density as cd

SCRIPT = Path(cd.__file__)
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

TERSE = "".join(("# group\n" if i % 10 == 0 else "") + f"def f{i}(x: int) -> int:\n    return x + {i}\n\n\n" for i in range(300))
CHATTY = "".join(f"def g{i}(x: int) -> int:\n    # add {i} to x\n    # and hand it back\n    return x + {i}\n\n\n" for i in range(20))
QUIET = "".join(f"def h{i}(x: int) -> int:\n    return x * {i}\n\n\n" for i in range(30))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "protocol.file.allow=always", *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def init(path: Path, files: dict[str, str]) -> str:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return commit(path, files, "base")


def run(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args], capture_output=True, text=True, env={**GIT_ENV, **(env or {})}, check=False)


def test_python_counts_comments_and_docstring_lines_past_the_first_but_not_directives() -> None:
    source = '"""Module."""\n\nimport os  # noqa: F401\n\n\ndef f(x):\n    """Doc line one.\n\n    Doc line two.\n    """\n    # a real comment\n    return x  # trailing\n\n\nTEXT = """not a docstring"""\n'
    nonblank, commented = cd.python_lines(source)
    assert commented == {9, 10, 11, 12}
    assert 3 in nonblank and 15 in nonblank and 8 not in nonblank


def test_python_with_a_syntax_error_is_not_classified() -> None:
    assert cd.python_lines("def broken(:\n") is None


def test_hash_languages_count_full_line_comments_only() -> None:
    nonblank, commented = cd.hash_lines("#!/bin/bash\n# note\nkey: value  # trailing\n\n  # indented\n")
    assert nonblank == {1, 2, 3, 5}
    assert commented == {2, 5}


def test_cpp_counts_line_and_block_comments_outside_strings() -> None:
    source = 'int a = 1; // trailing\n/* block\n\n   more */\nconst char* s = "// not a comment";\nchar c = \'"\'; /* after a quote char */\nint b;\n'
    nonblank, commented = cd.c_lines(source)
    assert commented == {1, 2, 4, 6}
    assert nonblank == {1, 2, 4, 5, 6, 7}


def test_cpp_doxygen_brief_and_delimiters_are_free_and_further_lines_count() -> None:
    source = "/**\n * @brief Adds.\n * @param a first\n * @return sum\n */\nint add(int a);\n/** One line. */\nint one();\n/*! Qt style.\n    More. */\nint qt();\n"
    nonblank, commented = cd.c_lines(source)
    assert commented == {3, 4, 10}
    assert nonblank == set(range(1, 12))


def test_cpp_doc_line_runs_free_their_first_line_and_trailing_member_docs() -> None:
    source = "/// Brief.\n/// Detail.\n//! Detail two.\nint a;\n\n/// Other brief.\nint b; ///< member\nint c; ///< member\n//// banner\n// plain\n"
    _, commented = cd.c_lines(source)
    assert commented == {2, 3, 9, 10}


def test_every_registered_language_resolves_by_its_suffixes_and_names() -> None:
    for lang in cd.LANGUAGES:
        assert {cd.language(f"dir/file{suffix}") for suffix in lang.suffixes} <= {lang.name}
        assert {cd.language(f"dir/{name}") for name in lang.names} <= {lang.name}
        assert lang.family in ("code", "config")


def test_language_by_suffix_and_file_name() -> None:
    assert cd.language("a/b/c.py") == "python"
    assert cd.language("pkg/CMakeLists.txt") == "cmake"
    assert cd.language("docker/Dockerfile") == "docker"
    assert cd.language("include/x.hpp") == "c++"
    assert cd.language("README.md") is None


def test_only_lines_the_change_adds_are_counted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init(repo, {"old.py": "# old comment\nA = 1\nB = 2\n", "gone.py": "X = 1\n"})
    (repo / "gone.py").unlink()
    head = commit(repo, {"old.py": "# old comment\nA = 1\n# new comment\nC = 3\nB = 2\n", "new.py": "D = 4\n"}, "change")
    assert cd.added_lines(cd.Repo(repo), base, head) == {"old.py": {3, 4}, "new.py": {1}}


def test_chatty_file_is_flagged_and_quiet_file_is_not(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init(repo, {"lib.py": TERSE})
    head = commit(repo, {"chatty.py": CHATTY, "quiet.py": QUIET}, "change")
    scopes = list(cd.scan(cd.Repo(repo), base, head))
    totals, files = cd.verdicts(scopes, cd.Thresholds())
    assert [scope.name for scope in scopes] == ["."]
    assert scopes[0].baseline["python"] == cd.Count(630, 30)
    assert [(row.label, row.count) for row in files] == [("chatty.py", cd.Count(80, 40))]
    assert [(row.label, row.count, row.over) for row in totals] == [("python", cd.Count(140, 40), True)]


def test_file_below_min_lines_is_not_judged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init(repo, {"lib.py": TERSE})
    head = commit(repo, {"small.py": CHATTY[: CHATTY.index("def g5")]}, "change")
    _, files = cd.verdicts(list(cd.scan(cd.Repo(repo), base, head)), cd.Thresholds())
    assert files == []


def test_density_over_the_ratio_but_under_the_floor_is_not_flagged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sparse = "# only note\n" + "".join(f"def f{i}(x: int) -> int:\n    return x + {i}\n\n\n" for i in range(300))
    base = init(repo, {"lib.py": sparse})
    light = "".join(("# note\n" if i % 12 == 0 else "") + f"def g{i}(x: int) -> int:\n    return x - {i}\n\n\n" for i in range(72))
    head = commit(repo, {"light.py": light}, "change")
    scopes = list(cd.scan(cd.Repo(repo), base, head))
    assert [row.label for row in cd.verdicts(scopes, cd.Thresholds(floor=0.0))[1]] == ["light.py"]
    assert cd.verdicts(scopes, cd.Thresholds())[1] == []


def test_language_without_a_baseline_gets_no_verdict(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init(repo, {"lib.py": TERSE})
    head = commit(repo, {"conf.yaml": "".join(f"# why {i}\nkey{i}: {i}\n" for i in range(40))}, "change")
    totals, files = cd.verdicts(list(cd.scan(cd.Repo(repo), base, head)), cd.Thresholds())
    assert files == []
    assert [(row.label, row.over, row.ratio) for row in totals] == [("yaml", False, "-")]


def test_language_new_to_the_repo_is_judged_against_its_family(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init(repo, {"lib.py": TERSE})
    head = commit(repo, {"layer.cpp": "".join(f"// explain step {i}\nint step{i}() {{ return {i}; }}\n" for i in range(30))}, "change")
    totals, files = cd.verdicts(list(cd.scan(cd.Repo(repo), base, head)), cd.Thresholds())
    assert [(row.label, row.against, row.over) for row in totals] == [("c++", "code", True)]
    assert [(row.label, row.against, row.count) for row in files] == [("layer.cpp", "code", cd.Count(60, 30))]


def test_language_with_enough_lines_keeps_its_own_baseline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    noted = "".join(f"// part {i}\nint part{i}() {{ return {i}; }}\n" for i in range(300))
    base = init(repo, {"lib.py": TERSE, "old.cpp": noted})
    head = commit(repo, {"layer.cpp": "".join(f"// explain step {i}\nint step{i}() {{ return {i}; }}\n" for i in range(30))}, "change")
    totals, files = cd.verdicts(list(cd.scan(cd.Repo(repo), base, head)), cd.Thresholds())
    assert [(row.label, row.against, row.over) for row in totals] == [("c++", "c++", False)]
    assert files == []


def test_moved_submodule_pin_is_measured_against_its_own_baseline(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    init(upstream, {"lib.py": TERSE})
    repo = tmp_path / "repo"
    init(repo, {"root.py": "A = 1\n"})
    git(repo, "submodule", "add", "-q", str(upstream), "deps/sub")
    base = commit(repo, {}, "add submodule")
    commit(repo / "deps/sub", {"chatty.py": CHATTY}, "change")
    head = commit(repo, {}, "bump")

    scopes = list(cd.scan(cd.Repo(repo), base, head))
    _, files = cd.verdicts(scopes, cd.Thresholds())
    assert [scope.name for scope in scopes] == [".", "deps/sub"]
    assert [(row.scope, row.label, row.first) for row in files] == [("deps/sub", "deps/sub/chatty.py", 2)]


def test_submodule_with_a_missing_commit_is_skipped_with_a_reason(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    init(upstream, {"lib.py": TERSE})
    repo = tmp_path / "repo"
    init(repo, {"root.py": "A = 1\n"})
    git(repo, "submodule", "add", "-q", str(upstream), "sub")
    base = commit(repo, {}, "add submodule")
    absent = commit(upstream, {"chatty.py": CHATTY}, "change")
    git(repo, "update-index", "--cacheinfo", f"160000,{absent},sub")
    git(repo, "commit", "-q", "-m", "bump")
    head = git(repo, "rev-parse", "HEAD")

    scopes = list(cd.scan(cd.Repo(repo), base, head))
    assert [(scope.name, scope.skipped, scope.lost) for scope in scopes] == [(".", "", False), ("sub", f"commit {absent[:12]} not fetched", True)]


def test_submodule_that_is_not_checked_out_is_listed_without_an_annotation(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    init(upstream, {"lib.py": TERSE})
    repo = tmp_path / "repo"
    init(repo, {"root.py": "A = 1\n"})
    git(repo, "submodule", "add", "-q", str(upstream), "sub")
    base = commit(repo, {}, "add submodule")
    moved = commit(upstream, {"chatty.py": CHATTY}, "change")
    git(repo, "submodule", "deinit", "-q", "-f", "sub")
    git(repo, "update-index", "--cacheinfo", f"160000,{moved},sub")
    git(repo, "commit", "-q", "-m", "bump")

    result = run(repo, "--base", base, env={"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")})
    assert "sub: skipped, not checked out" in result.stdout
    assert "::warning" not in result.stdout


def test_cli_prints_the_table_and_exits_zero_on_a_flagged_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init(repo, {"lib.py": TERSE})
    commit(repo, {"chatty.py": CHATTY}, "change")
    result = run(repo, "--base", base, env={"GITHUB_ACTIONS": ""})
    assert result.returncode == 0
    assert "Files over the threshold:" in result.stdout
    assert "40 of   80 lines  chatty.py" in result.stdout
    assert "::warning" not in result.stdout


def test_cli_under_github_actions_emits_annotations_and_a_summary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init(repo, {"lib.py": TERSE})
    commit(repo, {"chatty.py": CHATTY}, "change")
    summary = tmp_path / "summary.md"
    result = run(repo, "--base", base, env={"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)})
    assert result.returncode == 0
    assert "::warning file=chatty.py,line=2,title=comment density::50%25 of the 80 added lines are comments or docstrings, 10.5x the 4.8%25 python baseline" in result.stdout
    assert "| `chatty.py` | 80 | 40 | 50.0% | 4.8% | python | 10.5x |" in summary.read_text()


def test_cli_rejects_an_unknown_base_with_a_hint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init(repo, {"lib.py": TERSE})
    result = run(repo, "--base", "nope")
    assert result.returncode == 2
    assert "--base 'nope' is not a commit" in result.stderr


def test_cli_without_arguments_prints_help() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "--base REV" in result.stdout and "example:" in result.stdout
