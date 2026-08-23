"""Spec 13.1 + RT-7 + AM-3: hostile input must yield a valid FTS5 MATCH string
or None — never an exception. Validity is proven by executing every query
against a real in-memory FTS5 table."""
import sqlite3

import pytest

from lib import querybuild

HOSTILE = [
    'he said "hello there" loudly',
    "'single quoted' and unbalanced ' apostrophe",
    'NEAR(foo, 3) NEAR( bar',
    "-exclude -this -minus--dashes-",
    "col:value column: another",
    "wild* card* AND OR NOT",
    "(paren (nested) unbalanced (",
    "a\" OR \"b -- injection attempt",
    "caret^5 boost^ syntax",
    "emoji \U0001F600\U0001F4A9 mixed with téxt and 中文 tokens",
    "back\\slash \\\"escaped quote",
    "{curly} [square] <angle> braces",
    "semicolon; DROP TABLE memory; --",
    "\x00null\x01bytes\x02embedded",
    "   ",
    "",
    "\n\t\r",
    "a b c",  # all sub-length tokens
    "the and for with from",  # all stopwords
    "!!!???...,,,***",
]


@pytest.fixture(scope="module")
def fts():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE VIRTUAL TABLE t USING fts5(title, body, tags, "
              "tokenize='porter unicode61')")
    c.execute("INSERT INTO t VALUES ('pytest gotcha', "
              "'pytest here needs pythonpath src conftest', 'pytest,uuid')")
    yield c
    c.close()


def _assert_valid(fts, q):
    if q is None:
        return
    assert isinstance(q, str) and q.strip()
    fts.execute("SELECT rowid FROM t WHERE t MATCH ?", (q,)).fetchall()


@pytest.mark.parametrize("text", HOSTILE)
def test_fts_query_hostile_never_raises(fts, text):
    _assert_valid(fts, querybuild.fts_query(text))


@pytest.mark.parametrize("text", HOSTILE)
def test_error_query_hostile_never_raises(fts, text):
    _assert_valid(fts, querybuild.error_query(text))


def test_fts_query_one_megabyte_input(fts):
    text = ('run pytest with "quotes" NEAR(x,1) -flags \U0001F600 ' * 20000)[:1048576]
    q = querybuild.fts_query(text)
    _assert_valid(fts, q)
    assert q is not None  # real tokens exist in there


def test_error_query_one_megabyte_input(fts):
    text = ("Error: something failed at /path/x.py line 12 deadbeef01 " * 18000)[:1048576]
    _assert_valid(fts, querybuild.error_query(text))


def test_fts_query_non_string_inputs():
    assert querybuild.fts_query(None) is None
    assert querybuild.fts_query(12345) is None
    assert querybuild.fts_query(b"bytes") is None


def test_fts_query_all_tokens_quoted(fts):
    q = querybuild.fts_query("deploy kubernetes cluster config")
    for part in q.split(" OR "):
        assert part.startswith('"') and part.endswith('"')
    _assert_valid(fts, q)


def test_uuid4_regression(fts):
    # AM-3: identifier trailing-digit stemming — query token uuid4 must also
    # emit the stripped "uuid" so porter-unrelated tags still match.
    q = querybuild.fts_query("generate a uuid4 for the record")
    assert '"uuid4"' in q and '"uuid"' in q
    rows = fts.execute("SELECT rowid FROM t WHERE t MATCH ?", (q,)).fetchall()
    assert rows  # matches the doc tagged 'uuid'


def test_trailing_digit_strip_respects_stopwords():
    # "the4" -> stripped form "the" is a stopword and must not be emitted
    q = querybuild.fts_query("the4 zebra") or ""
    assert '"the"' not in q


def test_short_tokens_not_digit_stripped():
    # only len>=4 tokens get the stripped variant
    q = querybuild.fts_query("ab1 zebra") or ""
    assert '"ab"' not in q


def test_stopwords_removed():
    q = querybuild.fts_query("please run the deployment and check the file")
    assert '"please"' not in q and '"the"' not in q and '"run"' not in q
    assert '"deployment"' in q


def test_all_stopwords_returns_none():
    assert querybuild.fts_query("please run the file and make it work") is None


def test_code_fences_stripped():
    text = "fix the login bug ```secretfencetoken innerfence```  outside"
    q = querybuild.fts_query(text)
    assert '"secretfencetoken"' not in q and '"innerfence"' not in q
    assert '"login"' in q


def test_dedup_preserves_first_occurrence():
    q = querybuild.fts_query("zebra zebra ZEBRA giraffe")
    assert q.count('"zebra"') == 1


def test_cap_limits_base_tokens():
    words = " ".join(f"uniqueword{chr(97 + i)}x" for i in range(30))
    q = querybuild.fts_query(words, cap=12)
    assert len(q.split(" OR ")) <= 12


def test_error_query_prefers_error_lines(fts):
    text = ("compiling module alpha\n"
            "linking objects bravo\n"
            "Error: connection refused talking to registry\n")
    q = querybuild.error_query(text)
    _assert_valid(fts, q)
    assert '"connection"' in q and '"refused"' in q
    assert '"compiling"' not in q


def test_error_query_strips_paths_hashes_numbers():
    text = 'Error at /usr/lib/python3/dist.py line 4217 commit deadbeefcafe failed'
    q = querybuild.error_query(text) or ""
    assert "dist.py" not in q and "deadbeefcafe" not in q and "4217" not in q


def test_error_query_falls_back_to_tail_lines(fts):
    text = "alpha zebra\nbravo giraffe\ncharlie hippo\ndelta rhino"
    q = querybuild.error_query(text)
    _assert_valid(fts, q)
    assert '"rhino"' in q
    assert '"zebra"' not in q  # only last 3 lines on fallback
