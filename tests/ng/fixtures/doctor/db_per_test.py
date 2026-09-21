import sqlite3


def test_db():
    sqlite3.connect("test.db")
