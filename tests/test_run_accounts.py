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


def test_launcher_lock_rejects_concurrent_launcher_and_releases_for_next_run(tmp_path):
    runner = load_runner()
    first = runner._acquire_launcher_lock(tmp_path)
    try:
        try:
            runner._acquire_launcher_lock(tmp_path)
        except RuntimeError as exc:
            assert "already running" in str(exc)
        else:
            raise AssertionError("A second launcher acquired the lock")
    finally:
        runner._release_launcher_lock(first)

    second = runner._acquire_launcher_lock(tmp_path)
    runner._release_launcher_lock(second)


def test_launcher_refuses_an_existing_worker_before_starting_children(tmp_path, monkeypatch):
    runner = load_runner()
    lock_path = tmp_path / ".run_accounts.lock"
    monkeypatch.setattr(runner, "_acquire_launcher_lock", lambda base: lock_path.open("a+b"))
    monkeypatch.setattr(runner, "_release_launcher_lock", lambda handle: handle.close())
    monkeypatch.setattr(runner, "configured_profiles", lambda base: ["primary", "secondary"])
    monkeypatch.setattr(runner, "_worker_is_running", lambda profile, base: profile == "primary")

    def should_not_start(*args, **kwargs):
        raise AssertionError("launcher started children despite an existing worker")

    monkeypatch.setattr(runner.subprocess, "Popen", should_not_start)

    try:
        runner.start_accounts(tmp_path)
    except RuntimeError as exc:
        assert "primary" in str(exc)
    else:
        raise AssertionError("Existing primary worker was ignored")


def test_launcher_waits_for_service_health_not_venv_stub_exit(monkeypatch, tmp_path):
    runner = load_runner()
    calls = []
    dashboard_state = {"started": False}
    monkeypatch.setattr(runner, "_acquire_launcher_lock", lambda base: object())
    monkeypatch.setattr(runner, "_release_launcher_lock", lambda lock: None)
    monkeypatch.setattr(runner, "configured_profiles", lambda base: ["primary"])
    monkeypatch.setattr(runner, "_worker_is_running", lambda profile, base: dashboard_state["started"])
    monkeypatch.setattr(runner, "_dashboard_is_running", lambda port: dashboard_state["started"])
    monkeypatch.setattr(runner, "_owned_dashboard_pids", lambda profiles: [])
    monkeypatch.setattr(runner, "_pid_is_running", lambda pid: False)
    monkeypatch.setattr(runner, "_wait_for_owned_services", lambda profiles, base, timeout=45: calls.append((profiles, timeout)))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    class StubProcess:
        pid = 12345
        return_code = 0

        def __init__(self):
            self.terminated = False
            self.killed = False

        def poll(self):
            # Model venv's short-lived launcher returning while its child is healthy.
            return self.return_code

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return self.return_code

        def kill(self):
            self.killed = True

    stub = StubProcess()
    def start_process(*args, **kwargs):
        dashboard_state["started"] = True
        return stub
    monkeypatch.setattr(runner.subprocess, "Popen", start_process)
    runner.start_accounts(tmp_path)

    assert calls == [(["primary"], 45.0)]
    assert not stub.terminated


def test_launcher_refuses_existing_dashboard_port(tmp_path, monkeypatch):
    runner = load_runner()
    (tmp_path / ".env").write_text("ALPACA_LIVE=False\n", encoding="utf-8")
    (tmp_path / ".env.secondary").unlink(missing_ok=True)
    lock = (tmp_path / ".lock").open("a+b")
    monkeypatch.setattr(runner, "_acquire_launcher_lock", lambda base: lock)
    monkeypatch.setattr(runner, "_release_launcher_lock", lambda handle: handle.close())
    monkeypatch.setattr(runner, "_worker_is_running", lambda profile, base: False)
    monkeypatch.setattr(runner, "_dashboard_is_running", lambda port: port == runner.DASHBOARD_PORTS["primary"])
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not start")))

    try:
        runner.start_accounts(tmp_path)
    except RuntimeError as exc:
        assert "primary :8765" in str(exc)
    else:
        raise AssertionError("Launcher ignored a dashboard port collision")


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
