import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib import paths


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HINDBRAIN_DATA", str(d))
    monkeypatch.delenv("HINDBRAIN_DB", raising=False)
    monkeypatch.delenv("HINDBRAIN_DISABLE", raising=False)
    paths._reset_cache_for_tests()
    yield str(d)
    paths._reset_cache_for_tests()


@pytest.fixture
def conn(tmp_data):
    from lib import db
    c = db.connect()
    db.ensure_schema(c)
    yield c
    c.close()
