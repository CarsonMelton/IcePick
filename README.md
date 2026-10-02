# IcePick - eBay Discovery & Pre-Filter

Discovers newly-listed, buy-it-now eBay sports trading card listings ($100 and
under), drops obvious noise with free rule-based checks, and publishes the
day's candidates as JSON to a GitHub Gist. Does not do comp lookups, scoring,
or purchasing -- that's a separate downstream process.

## Setup

1. **Python**: 3.9+ recommended. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. **eBay credentials**: From your [developer.ebay.com](https://developer.ebay.com)
   account, use the **App ID (Client ID)** and **Cert ID (Client Secret)** from
   your *production* keyset (not sandbox).

3. **GitHub token**: Create a **classic** Personal Access Token
   (github.com -> Settings -> Developer settings -> Personal access tokens ->
   Tokens (classic)) with the **gist** scope checked. Fine-grained tokens do
   not currently support the Gists API.

4. Copy `.env.example` to `.env` and fill in the three values:
   ```
   EBAY_CLIENT_ID=...
   EBAY_CLIENT_SECRET=...
   GITHUB_TOKEN=...
   ```
   `.env` is gitignored -- never commit it.

## Running

Sanity-check filtering without publishing anything:
```
python ebay_discover.py --dry-run
```

Normal run (fetches, filters, writes local JSON, publishes to Gist):
```
python ebay_discover.py
```

On the **first non-dry-run execution**, the script creates a new secret Gist
and records its ID in `.state.json` (gitignored). Every run after that
updates the same Gist in place, so the downstream process can always fetch
the same stable raw URL -- check `logs/` or stdout for that URL after the
first run.

Useful flags:
- `--lookback-hours N` (default 26) -- only keep listings created within the
  last N hours. The default gives a small overlap buffer over a 24h cadence.
- `--max-pages N` (default 50) -- safety cap on API pages fetched per run.

## Editing the filter

`filters_config.json` holds three editable lists:
- `exclude_keywords` -- title matches here are dropped outright (reprints,
  customs, novelty items, etc).
- `manufacturer_keywords` / `grading_keywords` -- these never drop a listing,
  they just set `manufacturer_match` / `grading_match` flags on the output so
  the downstream process can prioritize listings more likely to have usable
  comp data.

Edit the JSON file directly; no code changes needed.

## Output

Each run writes `output/candidates_YYYY-MM-DD.json` locally (always, even on
`--dry-run`) and, on a real run, the same payload to the Gist. Shape:

```json
{
  "generated_at": "2026-10-02T09:00:00+00:00",
  "lookback_hours": 26,
  "category_id": "261328",
  "max_price_usd": 100,
  "total_fetched": 412,
  "total_survived": 287,
  "candidates": [
    {
      "listing_id": "...",
      "title": "...",
      "price": "24.99",
      "price_currency": "USD",
      "shipping_cost": "4.50",
      "condition": "Ungraded",
      "item_url": "https://www.ebay.com/itm/...",
      "date_listed": "2026-10-01T18:32:11.000Z",
      "manufacturer_match": true,
      "grading_match": false
    }
  ]
}
```

Note: `shipping_cost` can be `null` for listings with calculated/freight
shipping or local-pickup-only -- that's an eBay data gap, not a bug.

## Logging

Each run writes `logs/ebay_discover_YYYYMMDD.log` (also echoed to console)
with: counts of listings fetched/survived/dropped, the Gist URL on success,
and any API errors. Check this file first if a scheduled run looks off.

On eBay API failure (bad credentials, rate limit, network error) the script
logs the problem and exits with a non-zero status rather than crashing or
publishing partial/stale data.

## Scheduling on Windows (Task Scheduler)

1. Open **Task Scheduler** -> **Create Task** (not "Basic Task", so you get
   the full options).
2. **General** tab: name it (e.g. "IcePick eBay Discovery"); select
   "Run whether user is logged on or not" if you want it to run unattended.
3. **Triggers** tab -> New -> Daily, pick a time (e.g. 6:00 AM), Enabled.
4. **Actions** tab -> New -> Action: "Start a program".
   - Program/script: full path to your Python executable, e.g.
     `C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe`
   - Add arguments: `ebay_discover.py`
   - Start in: the full path to this project folder, e.g.
     `C:\Users\<you>\IcePick`
     (This matters -- it's how the script finds `.env`, `filters_config.json`,
     and writes `logs/`/`output/` in the right place.)
5. **Conditions**/**Settings** tabs: uncheck "Start the task only if the
   computer is on AC power" if this is a laptop, and consider checking
   "Run task as soon as possible after a scheduled start is missed" in case
   the machine is asleep at the scheduled time.
6. Save, then right-click the task -> **Run** once to test it for real before
   trusting the daily schedule. Check `logs/` afterward.

To test manually from a terminal first:
```
cd C:\Users\<you>\IcePick
python ebay_discover.py --dry-run
```
