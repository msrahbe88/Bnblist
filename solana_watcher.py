#!/usr/bin/env python3
"""
solana_watcher.py

Watches for newly created Solana DEX pools (Raydium, Orca, etc. via
GeckoTerminal's aggregated feed) and alerts (via ntfy.sh) only on ones
that pass two filters:
  1. Reasonable starting liquidity (MIN_LIQUIDITY_USD).
  2. LP tokens majority locked or burned, per RugCheck.xyz
     (MIN_LP_LOCKED_PCT).

WHAT THIS DOES AND DOES NOT DO
-------------------------------
- New pool data comes from GeckoTerminal's free public API
  (api.geckoterminal.com), which aggregates Raydium/Orca/etc. pool
  creation. No API key needed. This is a real-time-ish aggregated feed,
  not a direct on-chain subscription -- there can be a short delay
  (GeckoTerminal's public tier caches this endpoint for ~60 seconds).

- LP lock/burn and risk data comes from RugCheck.xyz's free public API
  (api.rugcheck.xyz), a well-known third-party Solana safety-checking
  service. No API key needed for their read endpoints.

- "LP locked or burned" here means RugCheck reports the pool's LP
  token supply as majority (>= MIN_LP_LOCKED_PCT) locked in a known
  locker contract OR burned (sent to an unspendable address). Both
  count the same way: neither can be pulled by the creator.

IMPORTANT -- what this does NOT do: passing both filters is not a
safety guarantee. It filters out the most common, laziest rug
patterns (zero liquidity, immediately-pullable LP), not sophisticated
ones (e.g. a malicious mint authority doing something unexpected,
fake locker contracts, insider wallets holding most of supply, social
engineering after launch). Treat an alert as "cleared two basic
checks," not as investment advice or a safety certification.

Both GeckoTerminal and RugCheck are third-party services outside our
control -- their APIs, field names, or availability could change
without notice. If this bot starts failing entirely, that's the first
thing to check.

Each run:
  1. Loads the last-processed pool-creation timestamp from
     solana_state.json.
  2. Fetches current new pools from GeckoTerminal.
  3. For any pool created after the last-processed timestamp, checks
     its liquidity. If it clears MIN_LIQUIDITY_USD, checks RugCheck
     for LP lock/burn status.
  4. Alerts on pools that clear both bars.
  5. Saves the newest pool-creation timestamp seen back to
     solana_state.json.

On the very first run ever, it just records the newest timestamp
currently on the board and does not alert on anything -- there's no
reasonable "previous" pool history to compare against.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

NTFY_TOPIC = "sol-dex333"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

STATE_FILE = Path(__file__).parent / "solana_state.json"
HTTP_TIMEOUT = 20

GECKOTERMINAL_NEW_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

MIN_LIQUIDITY_USD = 5000
MIN_LP_LOCKED_PCT = 80

# Common Solana base/quote tokens -- if the "new" token GeckoTerminal
# reports is actually one of these well-known ones, skip it (it means
# we picked the wrong side of the pair, not a genuinely new token).
KNOWN_BASE_TOKENS = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def send_ntfy(title: str, message: str, tags: str = "seedling", priority: str = "high") -> None:
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


def load_state() -> dict:    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            print("[state] state file unreadable, starting fresh")
    return {"last_created_at": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_new_pools() -> list:
    """Returns a list of pool dicts with keys: pool_created_at (str,
    ISO8601), reserve_in_usd (float), base_mint (str), base_symbol
    (str), pool_address (str)."""
    resp = requests.get(
        GECKOTERMINAL_NEW_POOLS_URL,
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()

    # Build a lookup of included token objects (JSON:API sideloading)
    # so we can resolve each pool's base token to its mint address.
    tokens_by_id = {}
    for item in payload.get("included", []):
        if item.get("type") == "token":
            tokens_by_id[item["id"]] = item.get("attributes", {})

    pools = []
    for item in payload.get("data", []):
        attrs = item.get("attributes", {})
        rels = item.get("relationships", {})

        base_ref = rels.get("base_token", {}).get("data", {})
        base_id = base_ref.get("id", "")
        base_attrs = tokens_by_id.get(base_id, {})
        base_mint = base_attrs.get("address")
        base_symbol = base_attrs.get("symbol", "?")

        reserve_usd = attrs.get("reserve_in_usd")
        created_at = attrs.get("pool_created_at")
        pool_address = attrs.get("address")

        if not base_mint or reserve_usd is None or not created_at:
            continue

        try:
            reserve_usd = float(reserve_usd)
        except (TypeError, ValueError):
            continue

        pools.append({
            "pool_created_at": created_at,
            "reserve_in_usd": reserve_usd,
            "base_mint": base_mint,
            "base_symbol": base_symbol,
            "pool_address": pool_address,
        })

    return pools


def check_lp_locked(mint: str):
    """Returns (is_locked_enough, locked_pct) using RugCheck's report.
    Returns (False, None) if the check fails or data is unavailable --
    we do NOT alert on tokens we couldn't verify."""
    try:
        resp = requests.get(
            RUGCHECK_REPORT_URL.format(mint=mint),
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 404:
            print(f"[rugcheck] no report yet for {mint} (too new)")
            return False, None
        resp.raise_for_status()
        data = resp.json()

        # RugCheck reports LP status per market (per DEX pool). Take
                # RugCheck reports LP status per market (per DEX pool). Take
        # the highest locked percentage across markets -- if any pool
        # has the LP safely locked/burned, that's the relevant one.
        best_locked_pct = 0.0
        for market in data.get("markets", []):
            lp = market.get("lp", {})
            pct = lp.get("lpLockedPct")
            if pct is not None:
                try:
                    best_locked_pct = max(best_locked_pct, float(pct))
                except (TypeError, ValueError):
                    pass

        return best_locked_pct >= MIN_LP_LOCKED_PCT, best_locked_pct
    except requests.RequestException as e:
        print(f"[rugcheck] failed for {mint}: {e}")
        return False, None


def main() -> None:
    first_run = not STATE_FILE.exists()

    state = load_state()
    last_created_at = state.get("last_created_at")

    pools = get_new_pools()
    print(f"[scan] fetched {len(pools)} pools from GeckoTerminal")

    if not pools:
        save_state(state)
        print("[done] no pools returned")
        return

    # Newest first, by GeckoTerminal's own ordering -- but sort
    # defensively in case that ever changes.
    pools.sort(key=lambda p: p["pool_created_at"], reverse=True)
    newest_seen = pools[0]["pool_created_at"]

    if first_run or last_created_at is None:
        state["last_created_at"] = newest_seen
        save_state(state)
        print(f"[first run] seeding at {newest_seen}, no scan performed")
        return

    candidates = [p for p in pools if p["pool_created_at"] > last_created_at]
    print(f"[scan] {len(candidates)} pool(s) newer than last run ({last_created_at})")

    alerts_sent = 0
    for pool in candidates:
        if pool["base_mint"] in KNOWN_BASE_TOKENS:
            continue

        if pool["reserve_in_usd"] < MIN_LIQUIDITY_USD:
            continue

        locked_enough, locked_pct = check_lp_locked(pool["base_mint"])
        time.sleep(1)  # be polite to RugCheck's free API

        if not locked_enough:
            continue

        pct_str = f"{locked_pct:.0f}%" if locked_pct is not None else "?"
        send_ntfy(
            "New Solana coin cleared filters",
            f"{pool['base_symbol']} ({pool['base_mint']}) -- "
            f"${pool['reserve_in_usd']:,.0f} liquidity, {pct_str} LP locked/burned. "
            f"Pool: {pool['pool_address']}",
        )
        alerts_sent += 1

    state["last_created_at"] = newest_seen
    save_state(state)
    print(f"[done] checked {len(candidates)} candidate(s), sent {alerts_sent} alert(s)")


if __name__ == "__main__":
    main()


