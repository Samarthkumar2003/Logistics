"""
`agent_repo.email_for_name` resolves a vendor's address from the `agents` table
alone. It used to fall back to `data/agents_database.csv`, which let a vendor
deleted from the table stay mailable and let an empty table resolve names
against the original operator's private vendor list.
"""

import pytest

from backend.repositories import agent_repo


class _Table:
    def __init__(self, rows: list[dict], fail: bool = False):
        self._rows, self._fail, self._eq = rows, fail, None

    def select(self, *_a, **_k):
        return self

    def eq(self, column: str, value):
        self._eq = (column, value)
        return self

    def execute(self):
        if self._fail:
            raise ConnectionError("database unreachable")
        column, value = self._eq
        return type("R", (), {"data": [r for r in self._rows if r.get(column) == value]})()


def _use(monkeypatch, rows: list[dict], fail: bool = False) -> None:
    monkeypatch.setattr(agent_repo, "get_db",
                        lambda: type("DB", (), {"table": lambda _s, _n: _Table(rows, fail)})())


def test_one_row_resolves(monkeypatch):
    _use(monkeypatch, [{"agent_name": "Pipil Freight", "email": " vasu@pipil.example "}])
    assert agent_repo.email_for_name("Pipil Freight") == "vasu@pipil.example"


def test_name_missing_from_table_resolves_to_nothing(monkeypatch):
    # Pipil Freight is in the CSV. An empty table must not find it there.
    _use(monkeypatch, [])
    assert agent_repo.email_for_name("Pipil Freight") == ""


def test_multi_office_name_refuses_to_guess(monkeypatch):
    _use(monkeypatch, [
        {"agent_name": "DP World", "email": "a@dpw.example"},
        {"agent_name": "DP World", "email": "b@dpw.example"},
    ])
    assert agent_repo.email_for_name("DP World") == ""


def test_database_failure_raises_instead_of_reading_as_missing(monkeypatch):
    _use(monkeypatch, [], fail=True)
    with pytest.raises(ConnectionError):
        agent_repo.email_for_name("Pipil Freight")


def test_csv_loader_is_gone():
    assert not hasattr(agent_repo, "load_csv")
