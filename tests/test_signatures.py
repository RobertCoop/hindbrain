"""Spec 13.1: VAR= stripping, &&/;/| and $() splitting, multitool heads,
failure_signature classes, similar()."""
import pytest

from lib import signatures


# ---- subcommands: VAR= stripping ----

def test_leading_assignments_stripped():
    assert signatures.subcommands("FOO=1 BAR=two pytest -x") == [["pytest", "-x"]]


def test_assignment_only_segment_dropped():
    assert signatures.subcommands("FOO=1") == []


def test_assignments_stripped_per_segment():
    assert signatures.subcommands("FOO=1 pytest && BAR=2 tox") == [["pytest"], ["tox"]]


# ---- subcommands: splitting ----

def test_split_and_and():
    assert signatures.subcommands("git add . && git commit -m msg") == [
        ["git", "add", "."], ["git", "commit", "-m", "msg"]]


def test_split_semicolon_and_newline():
    assert signatures.subcommands("make build; ls\npwd") == [
        ["make", "build"], ["ls"], ["pwd"]]


def test_split_pipe():
    assert signatures.subcommands("cat f.txt | grep foo | wc -l") == [
        ["cat", "f.txt"], ["grep", "foo"], ["wc", "-l"]]


def test_command_substitution_extracted():
    subs = signatures.subcommands('echo "sha: $(git rev-parse HEAD)"')
    assert ["git", "rev-parse", "HEAD"] in subs
    assert any(s[0] == "echo" for s in subs)


def test_nested_command_substitution():
    subs = signatures.subcommands("echo $(dirname $(which python))")
    assert ["which", "python"] in subs
    assert any(s[0] == "dirname" for s in subs)


def test_single_quotes_protect_separators():
    assert signatures.subcommands("echo 'a && b; c'") == [["echo", "a && b; c"]]


def test_double_quotes_protect_separators():
    assert signatures.subcommands('echo "x && y"') == [["echo", "x && y"]]


def test_single_quotes_protect_substitution():
    subs = signatures.subcommands("echo '$(rm -rf /)'")
    assert ["rm", "-rf", "/"] not in subs


@pytest.mark.parametrize("bad", ["", None, 123, {}])
def test_subcommands_non_string(bad):
    assert signatures.subcommands(bad) == []


def test_subcommands_unbalanced_quote_falls_back():
    # shlex fails on unbalanced quotes; whitespace-split fallback still tokenizes
    subs = signatures.subcommands('git commit -m "oops')
    assert subs and subs[0][:2] == ["git", "commit"]


# ---- head / head_str ----

@pytest.mark.parametrize("toks,expected", [
    (["git", "push", "--force"], ("git", "push")),
    (["git", "-C", "/tmp/repo", "push"], ("git", "push")),
    (["git", "--no-pager", "log"], ("git", "log")),
    (["git", "-c", "user.name=x", "commit"], ("git", "commit")),
    (["/usr/bin/docker", "run", "img"], ("docker", "run")),
    (["npm", "install", "left-pad"], ("npm", "install")),
    (["make", "test"], ("make", "test")),
    (["pytest", "-x", "tests/"], ("pytest",)),
    (["ls", "-la"], ("ls",)),
    ([], ()),
])
def test_head(toks, expected):
    assert signatures.head(toks) == expected


def test_head_str():
    assert signatures.head_str(["git", "push", "origin"]) == "git.push"
    assert signatures.head_str(["pytest"]) == "pytest"


# ---- failure_signature code classes ----

@pytest.mark.parametrize("text,klass", [
    ("ImportError: cannot import name 'x' from 'y'", "importerror"),
    ("ModuleNotFoundError: No module named 'requests'", "modulenotfound"),
    ("bash: /etc/hosts: Permission denied", "permissionerror"),
    ("Command exited with 2", "exit_code"),
    ("Operation timed out", "timeout"),
    ("bash: foo: command not found", "notfound"),
    ("SyntaxError: invalid syntax", "syntax"),
    ("curl: Connection refused after retries", "connection"),
    ("fatal: Authentication failed for remote", "auth"),
    ("everything is broken somehow", "unknown"),
    ("", "unknown"),
])
def test_code_classes(text, klass):
    sig = signatures.failure_signature("Bash", {"command": "true"}, text)
    assert sig == f"Bash:true:{klass}"


def test_code_class_first_match_wins():
    text = "ModuleNotFoundError: No module named 'x'\nprocess exit code 1"
    sig = signatures.failure_signature("Bash", {"command": "pytest"}, text)
    assert sig.endswith(":modulenotfound")


def test_failure_signature_format():
    sig = signatures.failure_signature(
        "Bash", {"command": "git push origin main"}, "exit code 1")
    assert sig == "Bash:git.push:exit_code"


def test_failure_signature_non_dict_input():
    assert signatures.failure_signature("Bash", None, "weird") == "Bash::unknown"


def test_failure_signature_non_bash_tool():
    sig = signatures.failure_signature("Edit", {}, "Permission denied")
    assert sig == "Edit::permissionerror"


# ---- similar ----

def test_similar_same_head_different_class():
    assert signatures.similar("Bash:git.push:exit_code", "Bash:git.push:auth")


def test_similar_different_head():
    assert not signatures.similar("Bash:git.push:auth", "Bash:git.pull:auth")


def test_similar_different_tool():
    assert not signatures.similar("Bash:git.push:auth", "Edit:git.push:auth")


@pytest.mark.parametrize("a,b", [
    ("", ""), ("Bash", "Bash"), (None, None), ("Bash:git.push:x", ""),
])
def test_similar_malformed(a, b):
    assert not signatures.similar(a, b)
