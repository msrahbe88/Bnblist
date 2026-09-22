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
  creation. No API key needed.

- LP lock/burn and risk data comes from RugCheck.xyz's free public API
  (api.rugcheck.xyz), a well-known third-party Solana safety-checking
  service. No API key needed for their read endpoints.

- "LP locked or burned" here means RugCheck reports the pool's LP
  token supply as majority (>= MIN_LP_LOCKED_PCT) locked in a known
  locker contract OR burned (sent to an unspendable address).

IMPORTANT -- what this does NOT do: passing both filters is not a
safety guarantee. It filters out the most common, laziest rug
patterns (zero liquidity, immediately-pullable LP), not sophisticated
ones. Treat an alert as "cleared two basic checks," not as investment
advice or a safety certification.

WHY THIS VERSION IS DIFFERENT FROM THE FIRST ONE
--------------------------------------------------
A brand-new pool very often has no usable data yet at the exact moment
it's created: GeckoTerminal frequently reports "reserve_in_usd": null
for the first while, and RugCheck often returns 404 ("not indexed
yet") for a token that's only seconds old. The original version
checked each pool exactly once, the moment it was first seen -- so
almost everything got judged during that empty window and never
looked at again.

This version instead tracks "pending" candidates in state and keeps
re-checking each one on every run (every 5 minutes) until it either:
  - passes both checks -> alert, then stop tracking it, or
  - hits PENDING_MAX_AGE_HOURS without passing -> give up, stop
    tracking it (no alert).

Each run:
  1. Loads state: last-processed pool-creation timestamp, and the
     list of pending candidates from previous runs.
  2. Fetches current new pools from GeckoTerminal; anything newer than
     last-processed gets added to pending (if not a known base token).
  3. For every pending candidate (new ones and carried-over ones):
     re-checks its current liquidity (via GeckoTerminal's per-pool
     endpoint, which tends to be fresher than the new_pools listing)
     and, if that clears the bar, checks RugCheck for LP lock/burn.
  4. Alerts on any candidate that clears both bars this run. Expires
     (drops, no alert) any candidate older than PENDING_MAX_AGE_HOURS.
  5. Saves the updated pending list and newest timestamp back to
     solana_state.json.

On the very first run ever, it just records the current newest
timestamp and does not add anything to pending -- there's no
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
GECKOTERMINAL_POOL_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_address}"
RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

MIN_LIQUIDITY_USD = 5000
MIN_LP_LOCKED_PCT = 80

# How long to keep re-checking a candidate before giving up on it.
PENDING_MAX_AGE_HOURS = 6

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


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            print("[state] state file unreadable, starting fresh")
    return {"last_created_at": None, "pending": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_new_pools() -> list:
    """Returns a list of pool dicts with keys: pool_created_at (str,
    ISO8601), reserve_in_usd (float or None), base_mint (str),
    base_symbol (str), pool_address (str). reserve_in_usd may be None
    for very fresh pools -- that's expected, not an error."""
    resp = requests.get(
        GECKOTERMINAL_NEW_POOLS_URL,
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()

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

        created_at = attrs.get("pool_created_at")
        pool_address = attrs.get("address")
        reserve_usd = attrs.get("reserve_in_usd")
        if reserve_usd is not None:
            try:
                reserve_usd = float(reserve_usd)
            except (TypeError, ValueError):
                reserve_usd = None

        if not base_mint or not created_at or not pool_address:
            continue

        pools.append({
            "pool_created_at": created_at,
            "reserve_in_usd": reserve_usd,
            "base_mint": base_mint,
            "base_symbol": base_symbol,
            "pool_address": pool_address,
        })

    return pools


def get_pool_liquidity(pool_address: str):
    """Re-checks a single pool's current liquidity directly, which
    tends to be fresher than the new_pools listing. Returns a float,
    or None if unavailable/unreadable."""
    try:
        resp = requests.get(
            GECKOTERMINAL_POOL_URL.format(pool_address=pool_address),
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        reserve_usd = data.get("data", {}).get("attributes", {}).get("reserve_in_usd")
        if reserve_usd is None:
            return None
        return float(reserve_usd)
    except (requests.RequestException, TypeError, ValueError) as e:
        print(f"[warn] pool liquidity re-check failed for {pool_address}: {e}")
        return None


def check_lp_locked(mint: str):
    """Returns (is_locked_enough, locked_pct). locked_pct is None if
    RugCheck has no data for this token yet (too new) or the call
    failed -- treated the same as "not ready," not "failed forever."""
    try:
        resp = requests.get(
            RUGCHECK_REPORT_URL.format(mint=mint),
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 404:
            return False, None
        resp.raise_for_status()
        data = resp.json()

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


def hours_since(iso_timestamp: str) -> float:
    try:
        first_seen = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - first_seen).total_seconds() / 3600
    except (ValueError, TypeError):
        return 0.0


def main() -> None:
    first_run = not STATE_FILE.exists()

    state = load_state()
    last_created_at = state.get("last_created_at")
    pending = state.get("pending", {})

    pools = get_new_pools()
    print(f"[scan] fetched {len(pools)} pools from GeckoTerminal")

    if not pools:
        save_state(state)
        print("[done] no pools returned")
        return

    pools.sort(key=lambda p: p["pool_created_at"], reverse=True)
    newest_seen = pools[0]["pool_created_at"]

    if first_run or last_created_at is None:
        state["last_created_at"] = newest_seen
        state["pending"] = {}
        save_state(state)
        print(f"[first run] seeding at {newest_seen}, no scan performed")
        return

    # Add genuinely new pools (since last run) to the pending set.
    new_count = 0
    for pool in pools:
        if pool["pool_created_at"] <= last_created_at:
            continue
        if pool["base_mint"] in KNOWN_BASE_TOKENS:
            continue
        if pool["base_mint"] not in pending:
            pending[pool["base_mint"]] = {
                "pool_address": pool["pool_address"],
                "base_symbol": pool["base_symbol"],
                "first_seen": datetime.now(timezone.utc).isoformat(),
            }
            new_count += 1
    print(f"[scan] {new_count} new candidate(s) added, {len(pending)} total pending")

    # Re-check every pending candidate, oldest logic: liquidity first
    # (cheap, no rate-limit concern), then RugCheck only if liquidity
    # clears the bar (RugCheck is the more precious rate-limited call).
    alerts_sent = 0
    still_pending = {}
    for mint, info in pending.items():
        age_hours = hours_since(info["first_seen"])
        if age_hours > PENDING_MAX_AGE_HOURS:
            print(f"[expire] {info['base_symbol']} ({mint}) gave up after {age_hours:.1f}h")
            continue

        liquidity = get_pool_liquidity(info["pool_address"])
        if liquidity is None or liquidity < MIN_LIQUIDITY_USD:
            still_pending[mint] = info
            continue

        locked_enough, locked_pct = check_lp_locked(mint)
        time.sleep(1)  # be polite to RugCheck's free API

        if not locked_enough:
            still_pending[mint] = info
            continue

        pct_str = f"{locked_pct:.0f}%" if locked_pct is not None else "?"
        send_ntfy(
            "New Solana coin cleared filters",
            f"{info['base_symbol']} ({mint}) -- "
            f"${liquidity:,.0f} liquidity, {pct_str} LP locked/burned. "
            f"Pool: {info['pool_address']}",
        )
        alerts_sent += 1

    state["pending"] = still_pending
    state["last_created_at"] = newest_seen
    save_state(state)
    print(f"[done] {len(still_pending)} still pending, sent {alerts_sent} alert(s)")


if __name__ == "__main__":
    main()
