"""Persistence. In memory on purpose, see docs/decisions/0001-store-in-memory.md."""
import itertools


class MemoryStore:
    def __init__(self):
        self._rows = {}
        self._ids = itertools.count(1)

    def insert(self, row):
        new_id = next(self._ids)
        self._rows[new_id] = dict(row, id=new_id)
        return self._rows[new_id]

    def get(self, row_id):
        return dict(self._rows[row_id])

    def update(self, row_id, row):
        self._rows[row_id] = dict(row)

    def all(self):
        return [dict(r) for r in self._rows.values()]
