import sqlite3
from contextlib import closing

import pytest

from crawlme.storage.read import connect


def test_connection_refuses_writes(tmp_path):
    db = tmp_path / "run ?# %.db"
    with closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE sample (value TEXT)")
        con.execute("INSERT INTO sample VALUES ('kept')")
        con.commit()

    with closing(connect(db)) as con:
        assert con.execute("SELECT value FROM sample").fetchone()[0] == "kept"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("DELETE FROM sample")


def test_missing_db_not_created(tmp_path):
    db = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        connect(db)
    assert not db.exists()
