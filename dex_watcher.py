#!/usr/bin/env python3
"""
dex_watcher.py

Watches PancakeSwap V2 (the main DEX on BNB Chain) for newly created
trading pairs, and alerts (via ntfy.sh) on ones that launch with
meaningful starting liquidity.

WHAT THIS DOES AND DOES NOT DO
-------------------------------
Reads the PancakeSwap V2 Factory contract's PairCreated events
directly from BNB Chain via public JSON-RPC endpoints -- no API key,
no signing, no account needed. This is fully public on-chain data.

"Meaningful starting liquidity" here means: at the moment this script
checks, the new pair's reserve of a known base asset (WBNB, BUSD, or
USDT) is above a threshold (see MIN_WBNB_LIQUIDITY / MIN_STABLE_LIQUIDITY
below). This filters out the flood of pairs created with zero or
near-zero liquidity, which are almost never real launches.

IMPORTANT -- what this filter does NOT do: it does not vet whether a
token is legitimate, audited, or safe to buy. A token can still have
real starting liquidity and be a scam -- e.g. a "rug pull" where the
creator seeds real liquidity to pass filters like this one, then
drains it minutes or hours later once people have bought in. Anyone
can create a PancakeSwap pair permissionlessly; passing this liquidity
filter is not an endorsement or a safety signal, just a noise filter.

This uses public BNB Chain RPC endpoints, which can occasionally be
slow, rate-limited, or briefly unavailable -- the script tries several
in order and moves on if one fails.

Each run:
  1. Loads the last-checked block number from dex_state.json.           2. Fetches new PairCreated events from that block to the current
     latest block.
  3. For each new pair, checks its current reserves. If the WBNB /
     BUSD / USDT side is above the liquidity threshold, alerts.
  4. Saves the latest block number back to dex_state.json.

On the very first run ever, it just records the current latest block
as a starting point and does not scan or alert on anything -- there's
no reasonable "previous" pair history to compare against.
"""

import json
from pathlib import Path

import requests

NTFY_TOPIC = "bnb-dex333"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

STATE_FILE = Path(__file__).parent / "dex_state.json"
HTTP_TIMEOUT = 20

# Try these in order; BSC public RPC nodes are sometimes flaky/rate-limited.
RPC_ENDPOINTS = [
    "https://bsc-dataseed.binance.org/",
    "https://bsc-dataseed1.defibit.io/",
    "https://bsc-dataseed1.ninicoin.io/",
    "https://bsc.publicnode.com",
]

PANCAKE_V2_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

# Known base assets on BNB Chain, all 18 decimals.
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095"
BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d0"
USDT = "0x55d398326f99059ff775485246999027b3197955"

MIN_WBNB_LIQUIDITY = 5      # in WBNB (~roughly a few thousand USD depending on price)
MIN_STABLE_LIQUIDITY = 3000  # in BUSD or USDT (both ~1 USD each)

# How many blocks to scan per run, max, as a safety cap (BSC does ~3s
# blocks, so 5 minutes is roughly 100 blocks -- this is generous headroom).
MAX_BLOCK_RANGE = 2000


def rpc_call(method: str, params: list):
    """Tries each RPC endpoint in turn until one responds successfully."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_error = None
    for url in RPC_ENDPOINTS:
        try:
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if "result" in data:
                return data["result"]
            last_error = data.get("error")
        except requests.RequestException as e:
            last_error = str(e)
    raise RuntimeError(f"All RPC endpoints failed for {method}: {last_error}")


def send_ntfy(title: str, message: str, tags: str = "moneybag", priority: str = "high") -> None:
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
    return {"last_block": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_latest_block() -> int:
    return int(rpc_call("eth_blockNumber", []), 16)
def get_new_pairs(from_block: int, to_block: int) -> list:
    """Returns a list of {"token0":..., "token1":..., "pair":...} for
    every PairCreated event in the given block range."""
    logs = rpc_call(
        "eth_getLogs",
        [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": PANCAKE_V2_FACTORY,
            "topics": [PAIR_CREATED_TOPIC],
        }],
    )

    pairs = []
    for log in logs:
        topics = log["topics"]
        data = log["data"][2:]  # strip 0x

        token0 = "0x" + topics[1][-40:]
        token1 = "0x" + topics[2][-40:]
        pair_address = "0x" + data[:64][-40:]

        pairs.append({"token0": token0, "token1": token1, "pair": pair_address})
    return pairs


def get_reserves(pair_address: str):
    """Returns (reserve0, reserve1) as ints, or None if the call fails."""
    try:
        result = rpc_call("eth_call", [{"to": pair_address, "data": "0x0902f1ac"}, "latest"])
        raw = result[2:]
        reserve0 = int(raw[0:64], 16)
        reserve1 = int(raw[64:128], 16)
        return reserve0, reserve1
    except Exception as e:
        print(f"[warn] getReserves failed for {pair_address}: {e}")
        return None


def get_token_symbol(token_address: str) -> str:
    """Best-effort ERC20 symbol() lookup. Falls back to a shortened
    address if the call or decode fails (some tokens use nonstandard
    return encodings)."""
    try:
        result = rpc_call("eth_call", [{"to": token_address, "data": "0x95d89b41"}, "latest"])
        raw = result[2:]
        length = int(raw[64:128], 16)
        data_hex = raw[128:128 + length * 2]
        symbol = bytes.fromhex(data_hex).decode("utf-8", errors="replace").strip("\x00")
        if symbol:
            return symbol
    except Exception:
        pass
    return f"{token_address[:6]}...{token_address[-4:]}"


def check_pair_liquidity(pair: dict):
    """Returns (is_notable, other_token_address, other_token_symbol,
    base_symbol, base_amount) if the pair clears the liquidity bar,
    else (False, None, None, None, None)."""
    reserves = get_reserves(pair["pair"])
    if reserves is None:
        return False, None, None, None, None

    reserve0, reserve1 = reserves
    token0, token1 = pair["token0"].lower(), pair["token1"].lower()

    for base_addr, base_symbol, threshold, reserve in (
        (WBNB, "WBNB", MIN_WBNB_LIQUIDITY, reserve0 if token0 == WBNB else reserve1 if token1 == WBNB else None),
        (BUSD, "BUSD", MIN_STABLE_LIQUIDITY, reserve0 if token0 == BUSD else reserve1 if token1 == BUSD else None),
        (USDT, "USDT", MIN_STABLE_LIQUIDITY, reserve0 if token0 == USDT else reserve1 if token1 == USDT else None),
    ):
        if reserve is None:
            continue
        amount = reserve / 1e18
        if amount >= threshold:
            other_token = pair["token1"] if token0 == base_addr else pair["token0"]
            other_symbol = get_token_symbol(other_token)
            return True, other_token, other_symbol, base_symbol, amount

    return False, None, None, None, None


def main() -> None:
    state = load_state()
    latest_block = get_latest_block()

    if state.get("last_block") is None:
        state["last_block"] = latest_block
        save_state(state)
        print(f"[first run] seeding at block {latest_block}, no scan performed")
        return

    from_block = state["last_block"] + 1
    to_block = min(latest_block, from_block + MAX_BLOCK_RANGE)

    if from_block > to_block:
        print("[done] no new blocks to scan")
        return

    print(f"[scan] blocks {from_block} to {to_block} ({to_block - from_block + 1} blocks)")
    pairs = get_new_pairs(from_block, to_block)
    print(f"[scan] found {len(pairs)} new pair(s)")

    for pair in pairs:
        is_notable, other_token, other_symbol, base_symbol, base_amount = check_pair_liquidity(pair)
        if is_notable:
            send_ntfy(
                "New BNB DEX listing",
                f"{other_symbol} ({other_token}) just listed on PancakeSwap with "
                f"{base_amount:,.1f} {base_symbol} liquidity. Pair: {pair['pair']}",
            )

    state["last_block"] = to_block
    save_state(state)
    print(f"[done] scanned through block {to_block}")


if __name__ == "__main__":
    main()

