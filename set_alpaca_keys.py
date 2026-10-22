"""Securely update Alpaca API credentials in .env.

Usage:
    python set_alpaca_keys.py            # prompt for new keys, validate, then write
    python set_alpaca_keys.py --check    # validate the keys already in .env, no writes

Keys are read with getpass (never echoed, never written to shell history or logs),
validated against the Alpaca API before anything is saved, and the trading mode
(paper vs live) is detected from the key itself so ALPACA_LIVE cannot disagree.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path
from typing import Dict, Tuple

import requests
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ENV = BASE_DIR / ".env"

ENDPOINTS = {
    "paper": "https://paper-api.alpaca.markets/v2/account",
    "live": "https://api.alpaca.markets/v2/account",
}


def mask(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}***{value[-2:]}"


def clean(value: str) -> str:
    """Strip whitespace and any quotes the user may have pasted along with the key."""
    return value.strip().strip("'\"").strip()


def detect_mode(api_key: str) -> str:
    """Alpaca paper keys start with PK, live keys with AK."""
    if api_key.startswith("PK"):
        return "paper"
    if api_key.startswith("AK"):
        return "live"
    return "unknown"


def fetch_account(api_key: str, secret_key: str, mode: str) -> Tuple[bool, dict, str]:
    """Calls GET /v2/account for the given mode. Returns (ok, account, error_message)."""
    url = ENDPOINTS[mode]
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException as exc:
        return False, {}, f"Network error contacting {url}: {exc}"

    try:
        body = resp.json()
    except ValueError:
        return False, {}, f"Non-JSON response (HTTP {resp.status_code}) from {url}"

    if resp.status_code == 200:
        return True, body, ""

    message = body.get("message") or body.get("error") or resp.text[:200]
    return False, {}, f"HTTP {resp.status_code}: {message}"


def validate(api_key: str, secret_key: str) -> Tuple[bool, str, dict, str]:
    """Tries the endpoint implied by the key prefix, then the other one as a fallback.

    Returns (ok, mode, account, error).
    """
    guessed = detect_mode(api_key)
    candidates = [guessed] if guessed in ENDPOINTS else ["paper", "live"]

    errors = []
    for mode in candidates:
        ok, account, error = fetch_account(api_key, secret_key, mode)
        if ok:
            return True, mode, account, ""
        errors.append(f"{mode}: {error}")

    return False, guessed if guessed in ENDPOINTS else "unknown", {}, " | ".join(errors)


def update_env_file(path: Path, updates: Dict[str, str]) -> None:
    """Rewrites only the given keys, preserving comments, ordering and other settings."""
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    seen = set()
    output = []
    for line in lines:
        match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
        if match and match.group(1) in updates:
            key = match.group(1)
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)

    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")

    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Windows uses inherited ACLs instead of POSIX modes

    os.replace(tmp, path)


def print_account_summary(mode: str, account: dict) -> None:
    print(f"  Mode:          {'PAPER' if mode == 'paper' else 'LIVE (REAL MONEY)'}")
    print(f"  Account:       {account.get('account_number', 'unknown')}")
    print(f"  Status:        {account.get('status', 'unknown')}")
    print(f"  Equity:        ${float(account.get('equity', 0)):,.2f}")
    print(f"  Cash:          ${float(account.get('cash', 0)):,.2f}")
    blocked = account.get("trading_blocked")
    if blocked:
        print("  WARNING:       trading_blocked is True on this account")


def check_existing(env_path: Path) -> int:
    if not env_path.exists():
        print(f"No environment file at {env_path}")
        return 1

    values = dotenv_values(env_path)
    api_key = clean(values.get("ALPACA_API_KEY") or "")
    secret_key = clean(values.get("ALPACA_SECRET_KEY") or "")
    configured_live = str(values.get("ALPACA_LIVE", "")).strip().lower() in ("true", "1", "yes")

    if not api_key or not secret_key:
        print("ALPACA_API_KEY and/or ALPACA_SECRET_KEY are missing or empty.")
        return 1

    print(f"Checking credentials in {env_path.name}")
    print(f"  Key:           {mask(api_key)}  ({len(api_key)} chars)")

    ok, mode, account, error = validate(api_key, secret_key)
    if not ok:
        print(f"  Result:        FAILED\n  Detail:        {error}")
        return 1

    print("  Result:        VALID")
    print_account_summary(mode, account)

    if mode == "live" and not configured_live:
        print("  WARNING:       live keys but ALPACA_LIVE=False - orders would route to paper.")
    if mode == "paper" and configured_live:
        print("  WARNING:       paper keys but ALPACA_LIVE=True - orders would be rejected.")
    return 0


def interactive_update(env_path: Path) -> int:
    print("Paste the new Alpaca credentials (input is hidden, nothing is logged).")
    print("Generate them at: https://app.alpaca.markets -> API Keys\n")

    api_key = clean(getpass.getpass("API Key ID: "))
    secret_key = clean(getpass.getpass("Secret Key: "))

    if not api_key or not secret_key:
        print("\nBoth values are required. Nothing was changed.")
        return 1

    if detect_mode(api_key) == "unknown":
        print("\nWARNING: key does not start with PK (paper) or AK (live); will try both endpoints.")

    print("\nValidating against Alpaca...")
    ok, mode, account, error = validate(api_key, secret_key)

    if not ok:
        print(f"  Authentication FAILED. Nothing was written.\n  Detail: {error}")
        return 1

    print("  Authentication OK")
    print_account_summary(mode, account)

    updates = {
        "ALPACA_API_KEY": api_key,
        "ALPACA_SECRET_KEY": secret_key,
        "ALPACA_LIVE": "True" if mode == "live" else "False",
    }

    update_env_file(env_path, updates)

    print(f"\nSaved to {env_path}")
    print(f"  ALPACA_LIVE set to {updates['ALPACA_LIVE']} (detected {mode} keys)")

    if mode == "live":
        print("\n  *** LIVE MODE: this account trades real money. ***")
        print("  Check MAX_PORTFOLIO_RISK_PER_TRADE and DAILY_DRAWDOWN_LIMIT before running.")

    print("\nNext: python bot.py --status")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Update and verify Alpaca API credentials in .env")
    parser.add_argument("--check", action="store_true", help="Validate the keys already in .env without changing them")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV, help="Path to the .env file")
    args = parser.parse_args()

    try:
        if args.check:
            return check_existing(args.env_file)
        return interactive_update(args.env_file)
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was changed.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
