#!/usr/bin/env python3
"""
Solana New-Coin Scanner
------------------------
Polls DexScreener for newly listed Solana pairs, filters by liquidity,
checks LP lock/burn status via RugCheck, and pushes alerts to ntfy.sh
when a token passes both filters.

Setup:
    pip install requests

Config:
    Edit the CONFIG block below, or set environment variables of the
    same name (env vars take priority).

Run:
    python solana_scanner.py

This is a monitoring/alerting tool only. It does not execute trades.
Always do your own research before buying anything it flags — passing
these filters is not a guarantee a token is safe.
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

# ----------------------------- CONFIG ------------------------------------

CONFIG = {
    # Your ntfy.sh topic name (pick something unique/hard-to-guess,
    # e.g. "solscan-8f3k2x9"). Subscribe to it in the ntfy app.
    "NTFY_TOPIC": os.environ.get("NTFY_TOPIC", "your-ntfy-topic-here"),
    "NTFY_SERVER": os.environ.get("NTFY_SERVER", "https://ntfy.sh"),

    # Minimum liquidity (USD) for a pair to be considered "appropriate"
    "MIN_LIQUIDITY_USD": float(os.environ.get("MIN_LIQUIDITY_USD", 5000)),

    # Minimum pool age in seconds before we bother checking lock status
    # (brand new pools often don't have lock data indexed yet)
    "MIN_POOL_AGE_SEC": int(os.environ.get("MIN_POOL_AGE_SEC", 60)),

    # How often to poll for new pairs (seconds). DexScreener's public
    # API is rate-limited; don't go below ~30s.
    "POLL_INTERVAL_SEC": int(os.environ.get("POLL_INTERVAL_SEC", 45)),

    # Minimum % of LP considered "locked or burned" to count as locked
    "MIN_LP_LOCKED_PCT": float(os.environ.get("MIN_LP_LOCKED_PCT", 90)),

    # Where we remember tokens we've already alerted on
    "SEEN_FILE": os.environ.get("SEEN_FILE", "seen_tokens.json"),

    # Where we track tokens waiting on a lock (liquidity ok, not locked yet)
    "PENDING_FILE": os.environ.get("PENDING_FILE", "pending_tokens.json"),

    # Re-check an unlocked token's LP status this often (seconds)
    "RESCAN_INTERVAL_SEC": int(os.environ.get("RESCAN_INTERVAL_SEC", 300)),

    # Stop rescanning a token after this long since it was first seen (seconds)
    "MAX_RESCAN_WINDOW_SEC": int(os.environ.get("MAX_RESCAN_WINDOW_SEC", 6 * 3600)),

    # Main loop tick (seconds) — governs how promptly pending rescans fire;
    # new-token polling still only happens every POLL_INTERVAL_SEC
    "TICK_SEC": int(os.environ.get("TICK_SEC", 30)),

    "LOG_LEVEL": os.environ.get("LOG_LEVEL", "INFO"),
}

DEXSCREENER_NEW_PAIRS_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_PAIRS_URL = "https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"

# ---------------------------------------------------------------------------

logging.basicConfig(
    level=CONFIG["LOG_LEVEL"],
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("solana_scanner")


class SeenStore:
    """Tracks token mints we've already alerted on, persisted to disk."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.seen = set()
        if self.path.exists():
            try:
                self.seen = set(json.loads(self.path.read_text()))
            except Exception:
                log.warning("Could not parse %s, starting fresh", self.path)

    def has(self, mint: str) -> bool:
        return mint in self.seen

    def add(self, mint: str):
        self.seen.add(mint)
        self._save()

    def _save(self):
        try:
            self.path.write_text(json.dumps(list(self.seen)))
        except Exception as e:
            log.warning("Failed saving seen store: %s", e)


class PendingStore:
    """
    Tracks tokens that had OK liquidity but no lock yet, so they can be
    rescanned periodically until they lock or the wait window expires.
    Persisted to disk so a restart doesn't lose the queue.

    Each entry: mint -> {
        "first_seen": epoch seconds,
        "next_check": epoch seconds,
        "symbol", "name", "url"  (cached for the eventual alert)
    }
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                log.warning("Could not parse %s, starting fresh", self.path)

    def add(self, mint: str, symbol: str, name: str, url: str, now: float):
        self.data[mint] = {
            "first_seen": now,
            "next_check": now + CONFIG["RESCAN_INTERVAL_SEC"],
            "symbol": symbol,
            "name": name,
            "url": url,
        }
        self._save()

    def remove(self, mint: str):
        self.data.pop(mint, None)
        self._save()

    def reschedule(self, mint: str, now: float):
        if mint in self.data:
            self.data[mint]["next_check"] = now + CONFIG["RESCAN_INTERVAL_SEC"]
            self._save()

    def due(self, now: float):
        """Mints whose next_check has arrived."""
        return [m for m, v in self.data.items() if v["next_check"] <= now]

    def expired(self, mint: str, now: float) -> bool:
        entry = self.data.get(mint)
        if not entry:
            return True
        return (now - entry["first_seen"]) > CONFIG["MAX_RESCAN_WINDOW_SEC"]

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.data))
        except Exception as e:
            log.warning("Failed saving pending store: %s", e)


def fetch_new_solana_tokens():
    """
    Fetch recently profiled tokens from DexScreener, then filter to Solana.
    Returns a list of dicts: {mint, pair_address, url}
    """
    try:
        resp = requests.get(DEXSCREENER_NEW_PAIRS_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Failed fetching new token profiles: %s", e)
        return []

    results = []
    for item in data if isinstance(data, list) else []:
        if item.get("chainId") != "solana":
            continue
        mint = item.get("tokenAddress")
        if not mint:
            continue
        results.append({
            "mint": mint,
            "url": item.get("url"),
        })
    return results


def fetch_pair_data(mint: str):
    """
    Look up the best/most liquid pair for a given mint via DexScreener search.
    Returns the pair dict or None.
    """
    try:
        resp = requests.get(
            DEXSCREENER_SEARCH_URL, params={"q": mint}, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Failed fetching pair data for %s: %s", mint, e)
        return None

    pairs = data.get("pairs") or []
    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    if not sol_pairs:
        return None

    # pick the pair with highest liquidity
    sol_pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd", 0), reverse=True)
    return sol_pairs[0]


def check_lp_lock(mint: str):
    """
    Query RugCheck for LP lock/burn info.
    Returns (is_locked: bool, locked_pct: float, note: str)
    """
    try:
        resp = requests.get(RUGCHECK_URL.format(mint=mint), timeout=15)
        if resp.status_code == 404:
            return False, 0.0, "not indexed by RugCheck yet"
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return False, 0.0, f"RugCheck error: {e}"

    # RugCheck summary includes markets with lp.lpLockedPct (0-100)
    markets = data.get("markets") or []
    if not markets:
        return False, 0.0, "no market data"

    best = max(markets, key=lambda m: (m.get("lp") or {}).get("lpLockedPct", 0))
    lp = best.get("lp") or {}
    locked_pct = lp.get("lpLockedPct", 0) or 0
    is_locked = locked_pct >= CONFIG["MIN_LP_LOCKED_PCT"]
    return is_locked, locked_pct, "ok"


def send_ntfy(title: str, message: str, url: str = None, priority: str = "default"):
    topic = CONFIG["NTFY_TOPIC"]
    if not topic or topic == "your-ntfy-topic-here":
        log.warning("NTFY_TOPIC not configured — skipping notification. %s", title)
        return
    server = CONFIG["NTFY_SERVER"].rstrip("/")
    headers = {
        "Title": title,
        "Priority": priority,
    }
    if url:
        headers["Click"] = url
        headers["Actions"] = f"view, Open Chart, {url}"
    try:
        requests.post(f"{server}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=10)
    except Exception as e:
        log.warning("Failed sending ntfy notification: %s", e)


def send_match_alert(mint: str, symbol: str, name: str, dex_url: str,
                      liquidity_usd: float, locked_pct: float, waited_sec: float = 0):
    title = f"🚨 {symbol} passed filters"
    waited_note = f"\nWaited {waited_sec/60:.0f} min for lock" if waited_sec else ""
    message = (
        f"{name} ({symbol})\n"
        f"Liquidity: ${liquidity_usd:,.0f}\n"
        f"LP locked/burned: {locked_pct:.1f}%\n"
        f"Mint: {mint}"
        f"{waited_note}"
    )
    log.info("MATCH: %s — liquidity $%.0f, LP locked %.1f%%", symbol, liquidity_usd, locked_pct)
    send_ntfy(title, message, url=dex_url, priority="high")


def evaluate_token(mint: str, pending: "PendingStore", now: float):
    """First-pass check on a freshly discovered token."""
    pair = fetch_pair_data(mint)
    if not pair:
        return

    liquidity_usd = (pair.get("liquidity") or {}).get("usd", 0) or 0
    pair_created_ms = pair.get("pairCreatedAt")
    age_sec = None
    if pair_created_ms:
        age_sec = (time.time() * 1000 - pair_created_ms) / 1000

    if liquidity_usd < CONFIG["MIN_LIQUIDITY_USD"]:
        log.debug("Skip %s: liquidity $%.0f below threshold", mint, liquidity_usd)
        return

    if age_sec is not None and age_sec < CONFIG["MIN_POOL_AGE_SEC"]:
        log.debug("Skip %s: pool too new (%.0fs)", mint, age_sec)
        return

    base = pair.get("baseToken") or {}
    symbol = base.get("symbol", "?")
    name = base.get("name", "?")
    dex_url = pair.get("url", f"https://dexscreener.com/solana/{pair.get('pairAddress', '')}")

    is_locked, locked_pct, note = check_lp_lock(mint)
    if is_locked:
        send_match_alert(mint, symbol, name, dex_url, liquidity_usd, locked_pct)
        return

    # Liquidity is fine but not locked yet — queue for rescanning instead
    # of dropping it.
    log.info("Token %s has liquidity ($%.0f) but LP not locked (%.1f%%, %s) — queued for rescan",
              mint, liquidity_usd, locked_pct, note)
    pending.add(mint, symbol, name, dex_url, now)


def process_pending(pending: "PendingStore", now: float):
    """Rescan tokens sitting in the pending queue whose timer is due."""
    for mint in pending.due(now):
        entry = pending.data.get(mint)
        if not entry:
            continue

        if pending.expired(mint, now):
            log.info("Token %s (%s) expired after %d min without a lock — dropping",
                      mint, entry["symbol"], CONFIG["MAX_RESCAN_WINDOW_SEC"] // 60)
            pending.remove(mint)
            continue

        is_locked, locked_pct, note = check_lp_lock(mint)
        if is_locked:
            # Re-pull liquidity in case it moved since first seen
            pair = fetch_pair_data(mint)
            liquidity_usd = ((pair.get("liquidity") or {}).get("usd", 0) if pair else 0) or 0
            waited = now - entry["first_seen"]
            send_match_alert(mint, entry["symbol"], entry["name"], entry["url"],
                              liquidity_usd, locked_pct, waited_sec=waited)
            pending.remove(mint)
        else:
            log.debug("Still unlocked: %s (%.1f%%, %s) — next check in %ds",
                       entry["symbol"], locked_pct, note, CONFIG["RESCAN_INTERVAL_SEC"])
            pending.reschedule(mint, now)


def run_once(seen: "SeenStore", pending: "PendingStore", now: float):
    """One full pass: poll for new tokens, then process any due rescans."""
    candidates = fetch_new_solana_tokens()
    log.info("Fetched %d candidate tokens", len(candidates))
    for c in candidates:
        mint = c["mint"]
        if seen.has(mint):
            continue
        seen.add(mint)
        evaluate_token(mint, pending, now)

    process_pending(pending, now)
    log.info("Pending queue size: %d", len(pending.data))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single pass and exit (for cron/GitHub Actions) "
             "instead of looping forever.",
    )
    args = parser.parse_args()

    log.info(
        "Starting Solana scanner. Liquidity >= $%.0f, LP locked >= %.0f%%, "
        "rescan every %ds for up to %dh",
        CONFIG["MIN_LIQUIDITY_USD"], CONFIG["MIN_LP_LOCKED_PCT"],
        CONFIG["RESCAN_INTERVAL_SEC"], CONFIG["MAX_RESCAN_WINDOW_SEC"]
