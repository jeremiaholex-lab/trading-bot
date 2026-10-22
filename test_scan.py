#!/usr/bin/env python
"""Ad-hoc manual market scan.

This is a *script*, not a test. Running `pytest` must never import it and
execute the trading loop, so all of the work is guarded by the
`__main__` check. Use it with:

    python test_scan.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from bot import TradingBotOrchestrator


def main():
    print("Creating bot...")
    bot = TradingBotOrchestrator()
    print("Bot created")

    print("Calling scan_and_trade...")
    signals = bot.scan_and_trade()
    print(f"scan_and_trade returned {len(signals)} signals")

    for s in signals:
        print(f"  {s.symbol}: {s.action} (confidence: {s.confidence:.2f})")


if __name__ == "__main__":
    main()
