from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
from typing import Mapping

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


def _worker_is_running(profile: str, base_dir: Path) -> bool:
    try:
        heartbeat = json.loads(_heartbeat_path(profile, base_dir).read_text(encoding="utf-8"))
        pid = int(heartbeat["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _stop_owned_processes(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def start_accounts(base_dir: Path = BASE_DIR) -> None:
    launcher_lock = _acquire_launcher_lock(base_dir)
    processes: list[subprocess.Popen] = []
    try:
        profiles = configured_profiles(base_dir)
        running = [profile for profile in profiles if _worker_is_running(profile, base_dir)]
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

        while all(process.poll() is None for process in processes):
            time.sleep(1)
        exited = [str(process.pid) for process in processes if process.poll() is not None]
        if exited:
            raise RuntimeError("Owned account process exited unexpectedly: " + ", ".join(exited))
    except KeyboardInterrupt:
        print("Stopping account processes started by this launcher.")
    finally:
        try:
            _stop_owned_processes(processes)
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
