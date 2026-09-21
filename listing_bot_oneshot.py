#!/usr/bin/env python3
"""
listing_bot_oneshot.py

One-shot version of the Binance listing checker, designed to be run on
a schedule by GitHub Actions instead of looping forever on a server.

Each run:
  1. Loads the last-known state from listing_state.json (committed in
     the repo).
  2. Fetches the current Binance symbol list.
  3. Alerts (via ntfy.sh) on anything newly TRADING.
  4. Saves the new state back to listing_state.json so the workflow
     can commit it.

On the very first run ever (no state file yet), it seeds the state
without sending any alerts -- otherwise it would fire one notification
per already-listed coin.
"""

import json
from pathlib import Path

import requests

NTFY_TOPIC = "RHBNBNEW"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

STATE_FILE = Path(__file__).parent / "listing_state.json"

# Binance's regular api.binance.com blocks requests from certain IP
# locations (incl. US-hosted servers, which is what GitHub Actions
# runners use) with an HTTP 451 error. data-api.binance.vision is
# Binance's official market-data-only mirror -- same public endpoints,
# no geo-blocking, no auth needed. See:
# https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only
BINANCE_EXCHANGE_INFO_URL = "https://data-api.binance.vision/api/v3/exchangeInfo"
BINANCE_QUOTE_ASSETS_OF_INTEREST = {"USDT", "USDC", "BTC"}
HTTP_TIMEOUT = 15


def send_ntfy(title: str, message: str, tags: str = "rotating_light", priority: str = "high") -> None:
    try:
        requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=HTTP_TIMEOUT,
        )
        print(f"[ntfy] sent: {title} - {message}")
    except requests.RequestException as e:
        print(f"[ntfy] failed to send notification: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            print("[state] state file unreadable, starting fresh")
    return {"binance_symbols": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_binance_symbols() -> dict:
    resp = requests.get(BINANCE_EXCHANGE_INFO_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    out = {}
    for s in data.get("symbols", []):
        if s.get("quoteAsset") not in BINANCE_QUOTE_ASSETS_OF_INTEREST:
            continue
        out[s["symbol"]] = {
            "status": s.get("status"),
            "base": s.get("baseAsset"),
            "quote": s.get("quoteAsset"),
        }
    return out


def main() -> None:
    first_run = not STATE_FILE.exists()

    state = load_state()
    previous = state.get("binance_symbols", {})
    current = get_binance_symbols()

    if first_run:
        print(f"[first run] seeding state with {len(current)} symbols, no alerts sent")
    else:
        for symbol, info in current.items():
            prev_info = previous.get(symbol)

            if prev_info is None:
                if info["status"] == "TRADING":
                    send_ntfy(
                        "New Binance listing",
                        f"{info['base']}/{info['quote']} ({symbol}) is now live and trading on Binance.",
                    )
            elif prev_info.get("status") != "TRADING" and info["status"] == "TRADING":
                send_ntfy(
                    "Binance listing now trading",
                    f"{info['base']}/{info['quote']} ({symbol}) just passed to TRADING status on Binance.",
                )

    state["binance_symbols"] = current
    save_state(state)
    print(f"[done] tracked {len(current)} symbols")


if __name__ == "__main__":
    main()


