"""Regression: ``Config.BENCHMARK_SYMBOL`` was dropped from the main tree.

The multi-account config rewrite (commit 6345c85) replaced ``config.py`` and
lost the ``BENCHMARK_SYMBOL`` line that the worktree copy still carries. Two
consumers still read the attribute:

* ``optimizer/self_improver.py`` -> ``SelfImprover.fetch_benchmark``
* ``dashboard/data_fetcher.py``  -> ``get_config()``

Without it both raised ``AttributeError``. The hermetic suite missed it
because its fixtures fake the market-data provider, which never touches the
missing ``Config`` attribute. These tests exercise both consumers against the
*real* ``Config`` so the failure is loud again.
"""
from config import Config


def test_config_defines_benchmark_symbol():
    """The attribute must exist on the live Config class, not just in docs."""
    assert hasattr(Config, "BENCHMARK_SYMBOL"), (
        "Config.BENCHMARK_SYMBOL is missing: SelfImprover.fetch_benchmark and "
        "dashboard get_config() will raise AttributeError. It was dropped in "
        "the multi-account config rewrite; restore it (see worktree config.py)."
    )
    # It is a concrete, upper-cased ticker, not an empty/placeholder value.
    assert Config.BENCHMARK_SYMBOL, "BENCHMARK_SYMBOL must not be empty."
    assert Config.BENCHMARK_SYMBOL == Config.BENCHMARK_SYMBOL.upper()


class _FakeProvider:
    """Stands in for MarketDataProvider; records the symbol it was asked for."""

    def __init__(self):
        self.requested = None

    def get_historical_bars(self, symbol, timeframe=None, days_back=None):
        self.requested = symbol
        import pandas as pd
        return pd.DataFrame(
            {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [0]},
        )


def test_fetch_benchmark_resolves_the_real_config_symbol():
    """fetch_benchmark must read Config.BENCHMARK_SYMBOL without crashing."""
    from optimizer.self_improver import SelfImprover

    provider = _FakeProvider()
    result = SelfImprover.fetch_benchmark(provider)

    # No crash, and it asked the data layer for the configured benchmark.
    assert provider.requested == Config.BENCHMARK_SYMBOL, (
        "fetch_benchmark requested the wrong symbol; it must use "
        f"Config.BENCHMARK_SYMBOL ({Config.BENCHMARK_SYMBOL!r})."
    )
    # The returned frame is a DataFrame (possibly empty, but never a crash).
    import pandas as pd
    assert isinstance(result, pd.DataFrame)


def test_dashboard_get_config_exposes_benchmark_symbol():
    """The dashboard config endpoint must not 500 on the missing attribute."""
    from dashboard.data_fetcher import get_config

    payload = get_config()
    assert "benchmark_symbol" in payload, (
        "dashboard get_config() no longer exposes benchmark_symbol."
    )
    assert payload["benchmark_symbol"] == Config.BENCHMARK_SYMBOL
