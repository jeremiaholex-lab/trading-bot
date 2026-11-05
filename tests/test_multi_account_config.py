import json
import os
from pathlib import Path
import subprocess
import sys


def load_profile(tmp_path, profile, **inherited):
    env_file = tmp_path / ".secondary.env"
    env_file.write_text(
        "ALPACA_API_KEY=secondary-key\nALPACA_SECRET_KEY=secondary-secret\nALPACA_LIVE=False\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_LIVE", "BOT_ENV_FILE", "BOT_STATE_DIR"):
        env.pop(name, None)
    env.update({"BOT_PROFILE": profile, "BOT_STATE_DIR": str(tmp_path / "state"), **inherited})
    if profile == "secondary" and env_file.exists():
        env["BOT_ENV_FILE"] = str(env_file)
    result = subprocess.run(
        [sys.executable, "-c", "import json; from config import Config; print(json.dumps({'profile': Config.BOT_PROFILE, 'key': Config.ALPACA_API_KEY, 'state': str(Config.STATE_DIR), 'logs': str(Config.LOGS_DIR), 'strategy': str(Config.STRATEGY_STATE_FILE), 'journal': str(Config.TRADES_DB_PATH), 'simulator': str(Config.SIMULATOR_STATE_FILE), 'heartbeat': str(Config.HEARTBEAT_PATH), 'errors': Config.validate()}))"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_secondary_profile_loads_own_paper_credentials_and_separate_state(tmp_path):
    loaded = load_profile(tmp_path, "secondary", ALPACA_API_KEY="primary-key", ALPACA_SECRET_KEY="primary-secret")

    assert loaded == {
        "profile": "secondary",
        "key": "secondary-key",
        "state": str(tmp_path / "state"),
        "logs": str(tmp_path / "state" / "logs"),
        "strategy": str(tmp_path / "state" / "strategy_state.json"),
        "journal": str(tmp_path / "state" / "trades.db"),
        "simulator": str(tmp_path / "state" / "simulator_state.json"),
        "heartbeat": str(tmp_path / "state" / "bot_heartbeat.json"),
        "errors": [],
    }


def test_secondary_profile_without_credentials_does_not_use_primary_credentials(tmp_path):
    env_file = tmp_path / ".secondary.env"
    env_file.write_text("", encoding="utf-8")
    env = os.environ.copy()
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_LIVE", "BOT_ENV_FILE", "BOT_STATE_DIR"):
        env.pop(name, None)
    env.update({
        "BOT_PROFILE": "secondary",
        "BOT_ENV_FILE": str(env_file),
        "BOT_STATE_DIR": str(tmp_path / "state"),
        "ALPACA_API_KEY": "primary-key",
        "ALPACA_SECRET_KEY": "primary-secret",
    })
    result = subprocess.run(
        [sys.executable, "-c", "from config import Config; print(Config.ALPACA_API_KEY); print(Config.ALPACA_SECRET_KEY); print(';'.join(Config.validate()))"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[:2] == ["", ""]
    assert "secondary profile requires Alpaca credentials" in result.stdout
