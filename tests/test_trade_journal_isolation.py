from execution.trade_journal import TradeJournal


def test_implicit_journal_uses_test_temporary_database(tmp_path, monkeypatch):
    monkeypatch.setattr(TradeJournal, "_init_db", lambda self: None)

    journal = TradeJournal()

    assert journal.db_path == tmp_path / "trades.db"
