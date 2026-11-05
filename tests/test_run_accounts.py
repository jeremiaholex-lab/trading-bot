from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import subprocess


RUNNER_PATH = Path(__file__).resolve().parents[1] / "run_accounts.py"


def load_runner():
    spec = spec_from_file_location("run_accounts", RUNNER_PATH)
    assert spec and spec.loader, "The multi-account launcher should exist."
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_secondary_profile_requires_its_own_paper_credentials(tmp_path):
    runner = load_runner()
    (tmp_path / ".env.secondary").write_text(
        "ALPACA_API_KEY=secondary-key\nALPACA_SECRET_KEY=secondary-secret\nALPACA_LIVE=False\n",
        encoding="utf-8",
    )

    profiles = runner.configured_profiles(tmp_path, {"ALPACA_API_KEY": "primary-key"})

    assert profiles == ["primary", "secondary"]


def test_secondary_profile_never_inherits_primary_credentials(tmp_path):
    runner = load_runner()
    inherited = {
        "ALPACA_API_KEY": "primary-key",
        "ALPACA_SECRET_KEY": "primary-secret",
        "ALPACA_LIVE": "True",
        "OTHER_SETTING": "keep",
    }

    env = runner.profile_environment("secondary", tmp_path, inherited)

    assert env["BOT_PROFILE"] == "secondary"
    assert env["BOT_ENV_FILE"] == str(tmp_path / ".env.secondary")
    assert env["BOT_STATE_DIR"] == str(tmp_path / "runtime" / "secondary")
    assert "ALPACA_API_KEY" not in env
    assert "ALPACA_SECRET_KEY" not in env
    assert "ALPACA_LIVE" not in env
    assert env["OTHER_SETTING"] == "keep"


def test_live_secondary_profile_is_rejected(tmp_path):
    runner = load_runner()
    (tmp_path / ".env.secondary").write_text(
        "ALPACA_API_KEY=secondary-key\nALPACA_SECRET_KEY=secondary-secret\nALPACA_LIVE=True\n",
        encoding="utf-8",
    )

    try:
        runner.configured_profiles(tmp_path, {})
    except ValueError as exc:
        assert "paper mode" in str(exc)
    else:
        raise AssertionError("live secondary profile was accepted")


def test_isolated_worker_can_import_project_modules(tmp_path):
    code = (
        f"import runpy; runpy.run_path({str(RUNNER_PATH)!r}, run_name='launcher_test'); "
        "import config; print(config.Config.BOT_PROFILE)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "primary"
