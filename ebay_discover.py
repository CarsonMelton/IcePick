"""
IcePick - eBay sports trading card deal-sourcing: discovery + cheap pre-filter.

Scope (intentionally tight): find newly-listed, buy-it-now sports trading card
listings under eBay's Browse API, drop obvious noise with free rule-based
checks, and publish the day's candidate list as JSON to a GitHub Gist for a
downstream comp-lookup/scoring process to pick up. No comp pricing, no
scoring, no purchasing happens here.
"""

import argparse
import base64
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
OUTPUT_DIR = BASE_DIR / "output"
STATE_FILE = BASE_DIR / ".state.json"
FILTERS_FILE = BASE_DIR / "filters_config.json"

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
EBAY_MARKETPLACE_ID = "EBAY_US"

# Sports Trading Card Singles. Deliberately NOT a Pokemon/TCG category
# (those live under CCG Individual Cards, 183454) -- this exclusion is
# structural via category choice, not a keyword filter.
SPORTS_CARDS_CATEGORY_ID = "261328"

MAX_PRICE_USD = 100
PAGE_SIZE = 200
DEFAULT_LOOKBACK_HOURS = 26
MAX_OFFSET = 10000  # eBay Browse API search cap (offset + limit <= 10000)

GITHUB_API_BASE = "https://api.github.com"
GIST_FILENAME = "icepick_candidates.json"
GIST_DESCRIPTION = "IcePick - daily eBay sports card candidate listings"


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"ebay_discover_{datetime.now().strftime('%Y%m%d')}.log"

    logger = logging.getLogger("icepick")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


def load_filters():
    with open(FILTERS_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return (
        [k.lower() for k in cfg.get("exclude_keywords", [])],
        [k.lower() for k in cfg.get("manufacturer_keywords", [])],
        [k.lower() for k in cfg.get("grading_keywords", [])],
    )


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_ebay_access_token(client_id, client_secret, logger):
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {credentials}",
    }
    data = {"grant_type": "client_credentials", "scope": EBAY_OAUTH_SCOPE}

    try:
        resp = requests.post(EBAY_TOKEN_URL, headers=headers, data=data, timeout=30)
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error requesting eBay OAuth token: {e}")
        return None

    if resp.status_code != 200:
        logger.error(f"Failed to get eBay OAuth token: HTTP {resp.status_code} - {resp.text[:500]}")
        return None

    return resp.json().get("access_token")


def fetch_listings(access_token, lookback_hours, max_pages, logger):
    """Page through item_summary/search, newest first, stopping once listings
    fall outside the lookback window (or a safety cap is hit)."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
        "Content-Type": "application/json",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    all_items = []
    offset = 0
    page = 0

    while True:
        if page >= max_pages:
            logger.warning(f"Hit max_pages safety cap ({max_pages}); stopping pagination early.")
            break
        if offset >= MAX_OFFSET:
            logger.warning("Hit eBay's 10,000 result offset cap; stopping pagination.")
            break

        params = {
            "category_ids": SPORTS_CARDS_CATEGORY_ID,
            "filter": f"price:[..{MAX_PRICE_USD}],priceCurrency:USD,buyingOptions:{{FIXED_PRICE}}",
            "sort": "newlyListed",
            "limit": str(PAGE_SIZE),
            "offset": str(offset),
            "fieldgroups": "EXTENDED",
        }

        try:
            resp = requests.get(EBAY_SEARCH_URL, headers=headers, params=params, timeout=30)
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error calling eBay search API: {e}")
            return None

        if resp.status_code == 429:
            logger.error("eBay API rate limit hit (HTTP 429). Stopping run; try again later.")
            return None
        if resp.status_code != 200:
            logger.error(f"eBay search API error: HTTP {resp.status_code} - {resp.text[:500]}")
            return None

        body = resp.json()
        items = body.get("itemSummaries", [])
        if not items:
            break

        stop = False
        for item in items:
            created_raw = item.get("itemCreationDate")
            if created_raw:
                created = datetime.strptime(created_raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                if created < cutoff:
                    stop = True
                    break
            all_items.append(item)

        page += 1
        offset += PAGE_SIZE

        if stop:
            break
        if offset >= body.get("total", 0):
            break

    return all_items


def title_matches_any(title, keywords):
    title_lower = title.lower()
    return any(kw in title_lower for kw in keywords)


def filter_listings(items, exclude_keywords, manufacturer_keywords, grading_keywords, logger):
    survivors = []
    dropped_count = 0

    for item in items:
        title = item.get("title", "")

        if title_matches_any(title, exclude_keywords):
            dropped_count += 1
            continue

        price = item.get("price", {})
        shipping_options = item.get("shippingOptions", [])
        shipping_cost = None
        if shipping_options:
            cost = shipping_options[0].get("shippingCost", {})
            shipping_cost = cost.get("value")

        survivors.append({
            "listing_id": item.get("itemId"),
            "title": title,
            "price": price.get("value"),
            "price_currency": price.get("currency"),
            "shipping_cost": shipping_cost,
            "condition": item.get("condition"),
            "item_url": item.get("itemWebUrl"),
            "date_listed": item.get("itemCreationDate"),
            "manufacturer_match": title_matches_any(title, manufacturer_keywords),
            "grading_match": title_matches_any(title, grading_keywords),
        })

    logger.info(f"Filter dropped {dropped_count} listing(s) as noise (exclude-keyword match).")
    return survivors


def publish_to_gist(payload, github_token, state, logger):
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
    }
    content = json.dumps(payload, indent=2)
    gist_id = state.get("gist_id")

    if gist_id:
        resp = requests.patch(
            f"{GITHUB_API_BASE}/gists/{gist_id}",
            headers=headers,
            json={"files": {GIST_FILENAME: {"content": content}}},
            timeout=30,
        )
        if resp.status_code == 404:
            logger.warning(f"Configured gist_id {gist_id} not found (deleted?); creating a new gist.")
            gist_id = None
        elif resp.status_code != 200:
            logger.error(f"Failed to update gist: HTTP {resp.status_code} - {resp.text[:500]}")
            return None

    if not gist_id:
        resp = requests.post(
            f"{GITHUB_API_BASE}/gists",
            headers=headers,
            json={
                "description": GIST_DESCRIPTION,
                "public": False,
                "files": {GIST_FILENAME: {"content": content}},
            },
            timeout=30,
        )
        if resp.status_code != 201:
            logger.error(f"Failed to create gist: HTTP {resp.status_code} - {resp.text[:500]}")
            return None
        gist_id = resp.json().get("id")
        state["gist_id"] = gist_id
        save_state(state)

    gist_data = resp.json()
    raw_url = gist_data.get("files", {}).get(GIST_FILENAME, {}).get("raw_url")
    return raw_url


def main():
    parser = argparse.ArgumentParser(description="IcePick eBay sports card discovery + pre-filter")
    parser.add_argument("--dry-run", action="store_true", help="Print results to console; skip Gist publish.")
    parser.add_argument("--lookback-hours", type=float, default=DEFAULT_LOOKBACK_HOURS,
                         help=f"Only keep listings created within this many hours (default: {DEFAULT_LOOKBACK_HOURS}).")
    parser.add_argument("--max-pages", type=int, default=50,
                         help="Safety cap on number of API pages fetched (default: 50, i.e. up to 10,000 items).")
    args = parser.parse_args()

    logger = setup_logging()
    logger.info("=== IcePick eBay discovery run starting ===")

    client_id = os.environ.get("EBAY_CLIENT_ID")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET")
    github_token = os.environ.get("GITHUB_TOKEN")

    if not client_id or not client_secret:
        logger.error("Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET environment variables. Exiting.")
        sys.exit(1)
    if not args.dry_run and not github_token:
        logger.error("Missing GITHUB_TOKEN environment variable (required to publish). Use --dry-run to skip publishing, or set it.")
        sys.exit(1)

    exclude_keywords, manufacturer_keywords, grading_keywords = load_filters()

    access_token = get_ebay_access_token(client_id, client_secret, logger)
    if not access_token:
        logger.error("Could not obtain eBay access token. Exiting.")
        sys.exit(1)

    items = fetch_listings(access_token, args.lookback_hours, args.max_pages, logger)
    if items is None:
        logger.error("Listing fetch failed. Exiting.")
        sys.exit(1)

    logger.info(f"Fetched {len(items)} raw listing(s) from eBay within the last {args.lookback_hours}h.")

    survivors = filter_listings(items, exclude_keywords, manufacturer_keywords, grading_keywords, logger)
    logger.info(f"{len(survivors)} listing(s) survived pre-filtering.")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": args.lookback_hours,
        "category_id": SPORTS_CARDS_CATEGORY_ID,
        "max_price_usd": MAX_PRICE_USD,
        "total_fetched": len(items),
        "total_survived": len(survivors),
        "candidates": survivors,
    }

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"candidates_{datetime.now().strftime('%Y-%m-%d')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    logger.info(f"Wrote local candidate file: {out_path}")

    if args.dry_run:
        logger.info("DRY RUN: skipping Gist publish.")
        print(json.dumps(payload, indent=2))
    else:
        state = load_state()
        raw_url = publish_to_gist(payload, github_token, state, logger)
        if not raw_url:
            logger.error("Gist publish failed. Exiting.")
            sys.exit(1)
        logger.info(f"Published candidates to Gist: {raw_url}")

    logger.info("=== IcePick eBay discovery run complete ===")


if __name__ == "__main__":
    main()
