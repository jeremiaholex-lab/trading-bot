from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Mapping
from urllib.request import urlopen

from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
PROFILES = ("primary", "secondary")
DASHBOARD_PORTS = {"primary": 8765, "secondary": 8766}
CREDENTIAL_NAMES = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")


def _acquire_launcher_lock(base_dir: Path):
    lock_file = (base_dir / ".run_accounts.lock").open("a+b")
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock_file.close()
        raise RuntimeError("account launcher is already running") from exc
    return lock_file


def _release_launcher_lock(lock_file) -> None:
    try:
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def profile_environment(
    profile: str,
    base_dir: Path = BASE_DIR,
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if profile not in PROFILES:
        raise ValueError(f"Unknown account profile: {profile}")

    env = dict(os.environ if inherited is None else inherited)
    if profile == "secondary":
        for name in (*CREDENTIAL_NAMES, "ALPACA_LIVE"):
            env.pop(name, None)

    env["BOT_PROFILE"] = profile
    env["BOT_ENV_FILE"] = str(base_dir / (".env" if profile == "primary" else ".env.secondary"))
    env["BOT_STATE_DIR"] = str(base_dir if profile == "primary" else base_dir / "runtime" / profile)
    return env


def _profile_values(profile: str, base_dir: Path, inherited: Mapping[str, str]) -> dict[str, str]:
    env_path = base_dir / (".env" if profile == "primary" else ".env.secondary")
    values = {key: str(value or "") for key, value in dotenv_values(env_path, interpolate=False).items()}
    if profile == "primary":
        values.update(inherited)
    return values


def _has_credentials(values: Mapping[str, str]) -> bool:
    placeholders = {"", "your_alpaca_api_key_here", "your_alpaca_secret_key_here"}
    credentials = [values.get(name, "").strip() for name in CREDENTIAL_NAMES]
    return all(value not in placeholders and not (value.startswith("${") and value.endswith("}")) for value in credentials)


def _is_live(values: Mapping[str, str]) -> bool:
    return values.get("ALPACA_LIVE", "False").strip().lower() in {"true", "1", "yes"}


def configured_profiles(base_dir: Path = BASE_DIR, inherited: Mapping[str, str] | None = None) -> list[str]:
    inherited = os.environ if inherited is None else inherited
    primary = _profile_values("primary", base_dir, inherited)
    if _is_live(primary):
        raise ValueError("primary profile must use Alpaca paper mode")
    profiles = ["primary"]

    secondary_path = base_dir / ".env.secondary"
    if not secondary_path.is_file():
        return profiles
    secondary = _profile_values("secondary", base_dir, {})
    if _is_live(secondary):
        raise ValueError("secondary profile must use Alpaca paper mode")
    if _has_credentials(secondary):
        profiles.append("secondary")
    return profiles


def _heartbeat_path(profile: str, base_dir: Path) -> Path:
    root = base_dir if profile == "primary" else base_dir / "runtime" / profile
    return root / "bot_heartbeat.json"


def _pid_is_running(pid: int) -> bool:
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # access denied means it exists
            try:
                exit_code = wintypes.DWORD()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, SystemError):
        return False


def _worker_is_running(profile: str, base_dir: Path) -> bool:
    try:
        heartbeat = json.loads(_heartbeat_path(profile, base_dir).read_text(encoding="utf-8"))
        if heartbeat.get("profile", profile) != profile:
            return False
        pid = int(heartbeat["pid"])
        updated = datetime.fromisoformat(heartbeat["updated_at"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
        return age <= 600 and _pid_is_running(pid)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _dashboard_is_running(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _owned_dashboard_pids(profiles: list[str]) -> list[int]:
    if os.name != "nt":
        return []
    ports = [str(DASHBOARD_PORTS[p]) for p in profiles]
    try:
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" "
            "| Where-Object { $_.CommandLine -match 'dashboard_server\\.py' -and "
            + " -or ".join(f"$_.CommandLine -match '--port {port}(?:\\s|$)'" for port in ports)
            + " } | Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=8,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = json.loads(result.stdout)
        return [int(data)] if isinstance(data, int) else [int(pid) for pid in data]
    except Exception:
        return []


def _stop_owned_processes(processes: list[subprocess.Popen], profiles: list[str], base_dir: Path) -> None:
    pids = {process.pid for process in processes if process.poll() is None}
    for profile in profiles:
        try:
            heartbeat = json.loads(_heartbeat_path(profile, base_dir).read_text(encoding="utf-8"))
            if heartbeat.get("profile") == profile and heartbeat.get("pid"):
                pids.add(int(heartbeat["pid"]))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
    pids.update(_owned_dashboard_pids(profiles))

    # First ask the Popen-owned process to stop normally, then ensure the
    # Windows venv interpreter child it spawned is also terminated.
    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    deadline = time.monotonic() + 5
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
    for pid in pids:
        if not _pid_is_running(pid):
            continue
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=8)
            else:
                os.kill(pid, signal.SIGTERM)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _wait_for_owned_services(profiles: list[str], base_dir: Path, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        missing = []
        for profile in profiles:
            if not _worker_is_running(profile, base_dir):
                missing.append(f"{profile} worker heartbeat")
            if not _dashboard_is_running(DASHBOARD_PORTS[profile]):
                missing.append(f"{profile} dashboard :{DASHBOARD_PORTS[profile]}")
        if not missing:
            return
        time.sleep(1)
    raise RuntimeError("Startup timed out waiting for: " + ", ".join(missing))


def start_accounts(base_dir: Path = BASE_DIR) -> None:
    launcher_lock = _acquire_launcher_lock(base_dir)
    processes: list[subprocess.Popen] = []
    profiles: list[str] = []
    try:
        profiles = configured_profiles(base_dir)
        running = [profile for profile in profiles if _worker_is_running(profile, base_dir)]
        dashboards = [profile for profile in profiles if _dashboard_is_running(DASHBOARD_PORTS[profile])]
        if dashboards:
            raise RuntimeError(
                "Dashboard port(s) already in use: "
                + ", ".join(f"{profile} :{DASHBOARD_PORTS[profile]}" for profile in dashboards)
            )
        if running:
            raise RuntimeError("Already-running bot worker(s): " + ", ".join(running))

        for profile in profiles:
            env = profile_environment(profile, base_dir)
            processes.append(subprocess.Popen(
                [sys.executable, "-I", str(base_dir / "run_accounts.py"), "--worker-profile", profile,
                 "--run", "--interval", "60"],
                cwd=base_dir,
                env=env,
            ))
            dashboard_env = dict(env)
            dashboard_env["BOT_SECONDARY_AVAILABLE"] = str("secondary" in profiles)
            processes.append(subprocess.Popen(
                [sys.executable, "-I", str(base_dir / "dashboard_server.py"), "--host", "127.0.0.1",
                 "--port", str(DASHBOARD_PORTS[profile])],
                cwd=base_dir,
                env=dashboard_env,
            ))
            print(f"Started {profile} paper worker and read-only dashboard on port {DASHBOARD_PORTS[profile]}.")

        # The Windows venv python.exe is a launcher stub: it spawns the base
        # interpreter and exits. Popen.poll() therefore does not represent the
        # actual worker/server lifetime. Supervise the worker heartbeats and
        # dashboard HTTP endpoints, which correspond to the real child processes.
        _wait_for_owned_services(profiles, base_dir)
        while True:
            time.sleep(5)
            _wait_for_owned_services(profiles, base_dir, timeout=30.0)
    except KeyboardInterrupt:
        print("Stopping account processes started by this launcher.")
    finally:
        try:
            _stop_owned_processes(processes, profiles, base_dir)
        finally:
            _release_launcher_lock(launcher_lock)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Start configured paper-account workers and dashboards")
    parser.add_argument("--worker-profile", choices=PROFILES)
    args, bot_args = parser.parse_known_args(argv)
    if args.worker_profile:
        worker_env = profile_environment(args.worker_profile)
        for name in (*CREDENTIAL_NAMES, "ALPACA_LIVE"):
            if name not in worker_env:
                os.environ.pop(name, None)
        os.environ.update(worker_env)
        sys.argv = [str(BASE_DIR / "bot.py"), *bot_args]
        runpy.run_path(str(BASE_DIR / "bot.py"), run_name="__main__")
        return

    if bot_args:
        parser.error("unrecognized arguments: " + " ".join(bot_args))
    start_accounts()


if __name__ == "__main__":
    main()
