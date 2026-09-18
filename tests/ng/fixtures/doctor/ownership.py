"""Executable local ownership examples; never imported by the static doctor.

These model recipe semantics, not a production database/cache provisioner.
The explicitly bad cleanup variants prove neighbor-sentinel sensitivity.
"""
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import sqlite3


class WorkerDatabases:
    def __init__(self, root: Path):
        self.root = root
        self.paths = {}
        self.setup_calls = Counter()

    def database(self, run, worker):
        key = (run, worker)
        if key not in self.paths:
            path = self.root / run / worker / "test.sqlite"
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path) as connection:
                connection.execute("create table if not exists records (value text)")
                self.setup_calls[key] += 1
            self.paths[key] = path
        return self.paths[key]

    @contextmanager
    def test_connection(self, run, worker):
        connection = sqlite3.connect(self.database(run, worker))
        try:
            yield connection
        finally:
            connection.rollback()
            connection.execute("delete from records")
            connection.commit()
            connection.close()

    def cleanup(self, run, worker, *, global_cleanup=False):
        owned = self.database(run, worker)
        for path in self.paths.values() if global_cleanup else (owned,):
            with sqlite3.connect(path) as connection:
                connection.execute("delete from records")


class CacheClient:
    def __init__(self, service, run, worker):
        self.service = service
        self.namespace = (run, worker)

    def put(self, key, value):
        self.service[(*self.namespace, key)] = value

    def get(self, key):
        return self.service.get((*self.namespace, key))

    def cleanup(self, *, global_cleanup=False):
        for key in tuple(self.service):
            if global_cleanup or key[:2] == self.namespace:
                del self.service[key]
