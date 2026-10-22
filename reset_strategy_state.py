#!/usr/bin/env python
"""Reset the bot's learned strategy parameters.

The parameters stored in strategy_state.json were selected by an older
version of the walk-forward optimizer that ranked candidates by their
held-out score (so the holdout was not held out) and computed Sharpe from
a handful of trade returns annualised with sqrt(252) -- which produced
values in the hundreds and dominated the fitness ranking.

Those parameters were never validated, and several were adopted with a
NEGATIVE fitness score while being marked "improved": true. This script
clears them so the bot starts from the documented defaults and re-calibrates
with the corrected optimizer on its next run.

Usage:
    python reset_strategy_state.py            # reset, keeping a backup
    python reset_strategy_state.py --dry-run  # show what would be reset
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import Config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset learned strategy parameters")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be reset and exit")
    args = parser.parse_args()

    state_file = Config.STRATEGY_STATE_FILE

    if not state_file.exists():
        print(f"No strategy state at {state_file}. Nothing to reset.")
        return 0

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Could not parse {state_file}: {e}")
        return 1

    symbols = data.get("symbols", {})
    if not symbols:
        print("Strategy state holds no tuned parameters. Nothing to reset.")
        return 0

    print(f"Strategy state: {state_file}")
    print(f"{'Symbol':<8}{'improved':<12}{'fitness':>10}{'sharpe':>10}{'trades':>8}")
    for sym, rec in symbols.items():
        perf = rec.get("performance", {})
        print(
            f"{sym:<8}{str(rec.get('improved')):<12}"
            f"{str(perf.get('fitness_score')):>10}"
            f"{str(perf.get('sharpe_ratio')):>10}"
            f"{str(perf.get('total_trades')):>8}"
        )

    suspect = [
        sym for sym, rec in symbols.items()
        if (rec.get("performance", {}).get("fitness_score") or 0) < 0
    ]
    if suspect:
        print(f"\n  {len(suspect)} symbol(s) were adopted with a NEGATIVE fitness score: {suspect}")

    if args.dry_run:
        print("\nDry run -- nothing was changed.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = state_file.with_name(f"{state_file.stem}_backup_{stamp}.json")
    shutil.copy2(state_file, backup)
    print(f"\nBacked up to {backup}")

    fresh = {
        "version": 1,
        "symbols": {},
        "last_updated": None,
    }
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(fresh, indent=2), encoding="utf-8")
    tmp.replace(state_file)

    print(f"Reset {state_file}. The bot will start from the default parameters")
    print(f"and re-calibrate on its next scheduled optimization cycle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
