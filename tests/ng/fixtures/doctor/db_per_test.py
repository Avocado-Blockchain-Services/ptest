import sqlite3


def test_db(tmp_path):
    """Exercise a per-test database without mutating the checkout."""
    connection = sqlite3.connect(tmp_path / "test.db")
    connection.close()
