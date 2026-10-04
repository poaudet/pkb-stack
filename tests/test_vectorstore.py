import math

import numpy as np
import pytest

from hub import db
from hub.vectorstore import NumpyStore, SqliteVecStore, select_store


def _stores(tmp_path):
    conn = db.connect(str(tmp_path / "v.db"))
    yield conn, NumpyStore()
    try:
        conn2 = db.connect(str(tmp_path / "v2.db"))
        yield conn2, select_store(conn2, "sqlite-vec")
    except Exception:  # noqa: BLE001
        pytest.skip("sqlite-vec unavailable")


def test_stores(tmp_path):
    for conn, store in _stores(tmp_path):
        store.ensure(conn, 4)
        e = np.eye(4, dtype=np.float32)
        for i in range(4):
            store.upsert(conn, i + 1, e[i])
        hits = store.knn(conn, e[0], 4)
        assert hits[0][0] == 1 and hits[0][1] == pytest.approx(0, abs=1e-3)
        assert hits[1][1] == pytest.approx(math.sqrt(2), abs=1e-3)
        store.delete(conn, [1])
        assert 1 not in [h[0] for h in store.knn(conn, e[0], 4)]
    assert isinstance(select_store(db.connect(str(tmp_path / "v3.db")), "auto"),
                      (SqliteVecStore, NumpyStore))
