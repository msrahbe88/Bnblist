#!/usr/bin/env python3
"""
douyin_watcher.py

One-shot checker for Douyin's public trending-topics board ("what's
going viral" on Douyin right now). Designed to run on a schedule via
GitHub Actions, same pattern as listing_bot_oneshot.py.

WHAT THIS DOES AND DOES NOT DO
-------------------------------
Douyin has no public API for "this specific video is going viral" --
that data simply isn't exposed. The closest real, public signal is
Douyin's own trending-topics/hashtags board (word_list), which is what
powers the "hot search" list shown on douyin.com. This script watches
that board and alerts when a topic appears on it for the first time.

This uses an unofficial, unsigned endpoint (iesdouyin.com's "open"
hot-search billboard) that doesn't require Douyin's anti-bot signing
the way their main web API does. That said:
  - It is not an official/documented API and could change or break at
    any time without notice.
  - Douyin may still rate-limit or block requests from certain IPs
    (including cloud/datacenter IPs like GitHub Actions runners) even
    on this "open" endpoint. If every run starts failing, that's the
    most likely cause, and there may not be a clean fix the way there
    was for the Binance geo-block.

Each run:
  1. Loads previously-seen topic words from douyin_state.json.
  2. Fetches the current trending list.
  3. Alerts (via ntfy.sh) on any topic word not seen before.
  4. Saves the updated set of seen words back to douyin_state.json.

On the very first run ever, it seeds the state without alerting --
otherwise it would fire ~50 notifications for the entire existing
board at once.
"""

import json
from pathlib import Path

import requests

NTFY_TOPIC = "RHBNBNEW"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

STATE_FILE = Path(__file__).parent / "douyin_state.json"

DOUYIN_HOT_SEARCH_URL = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
HTTP_TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com",
}


def send_ntfy(title: str, message: str, tags: str = "fire", priority: str = "default") -> None:
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
    return {"seen_words": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def get_trending_words() -> list:
    resp = requests.get(DOUYIN_HOT_SEARCH_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    words = []
    for item in data.get("word_list", []):
        word = item.get("word")
        if not word:
            continue
        words.append({"word": word, "hot_value": item.get("hot_value")})
    return words


def main() -> None:
    first_run = not STATE_FILE.exists()
                                                                    
    state = load_state()
    seen_words = set(state.get("seen_words", []))

    current = get_trending_words()
    current_words = {item["word"] for item in current}

    if first_run:
        print(f"[first run] seeding state with {len(current_words)} trending topics, no alerts sent")
    else:
        new_words = current_words - seen_words
        for item in current:
            if item["word"] in new_words:
                hot_value = item.get("hot_value")
                hot_str = f" (heat: {hot_value:,})" if isinstance(hot_value, (int, float)) else ""
                send_ntfy(
                    "New Douyin trend",
                    f"{item['word']}{hot_str} just entered Douyin's trending board.",
                )

    state["seen_words"] = sorted(seen_words | current_words)
    save_state(state)
    print(f"[done] tracked {len(state['seen_words'])} total words, {len(current_words)} currently trending")


if __name__ == "__main__":
    main()
