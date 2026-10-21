import pytest
from config import Config

def test_default_config_valid():
    """Default configuration should pass validation with 0 errors."""
    errors = Config.validate()
    assert len(errors) == 0, f"Expected valid config, got errors: {errors}"
    assert Config.assert_valid() is None

def test_config_invalid_risk_percentage(monkeypatch):
    """Risk per trade > 5% or <= 0 should fail validation."""
    monkeypatch.setattr(Config, "MAX_PORTFOLIO_RISK_PER_TRADE", 0.08)
    errors = Config.validate()
    assert any("MAX_PORTFOLIO_RISK_PER_TRADE" in e for e in errors)

    monkeypatch.setattr(Config, "MAX_PORTFOLIO_RISK_PER_TRADE", 0.0)
    errors = Config.validate()
    assert any("MAX_PORTFOLIO_RISK_PER_TRADE" in e for e in errors)

def test_config_invalid_max_positions(monkeypatch):
    """Max open positions must be between 1 and 20."""
    monkeypatch.setattr(Config, "MAX_OPEN_POSITIONS", 0)
    errors = Config.validate()
    assert any("MAX_OPEN_POSITIONS" in e for e in errors)

    monkeypatch.setattr(Config, "MAX_OPEN_POSITIONS", 50)
    errors = Config.validate()
    assert any("MAX_OPEN_POSITIONS" in e for e in errors)

def test_config_invalid_drawdown_limit(monkeypatch):
    """Daily drawdown limit > 10% or <= 0 should fail validation."""
    monkeypatch.setattr(Config, "DAILY_DRAWDOWN_LIMIT", 0.15)
    errors = Config.validate()
    assert any("DAILY_DRAWDOWN_LIMIT" in e for e in errors)

def test_config_invalid_profit_target_rr(monkeypatch):
    """Profit target RR must be at least 1.0."""
    monkeypatch.setattr(Config, "PROFIT_TARGET_RR", 0.5)
    errors = Config.validate()
    assert any("PROFIT_TARGET_RR" in e for e in errors)

def test_config_live_without_credentials(monkeypatch):
    """Enabling live mode without valid keys must fail validation."""
    monkeypatch.setattr(Config, "ALPACA_LIVE", True)
    monkeypatch.setattr(Config, "ALPACA_API_KEY", "")
    monkeypatch.setattr(Config, "ALPACA_SECRET_KEY", "")
    errors = Config.validate()
    assert any("required when ALPACA_LIVE is True" in e for e in errors)

    with pytest.raises(ValueError):
        Config.assert_valid()
