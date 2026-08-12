# PSA Card Population Monitor

Tracks verified PSA population snapshots for:

- 2025 Japanese MEGA Dream ex Mega Gengar ex SAR 240/193
- 2024 Japanese Terastal Fest ex Umbreon ex SAR 217/187
- 2026 Japanese Abyss Eye Mega Darkrai ex SAR 114/081
- 2025 Japanese MEGA Dream ex Pikachu ex SAR 234/193
- 2025 Japanese MEGA Dream ex Rocket's Mewtwo ex SAR 237/193
- 2025 Japanese MEGA Dream ex Mega Dragonite ex SAR 246/193

The baseline was verified against the rendered PSA certification pages on
2026-08-09 (Japan time). The cert number is used as the stable lookup key, and
the expected year, set, subject, card number, and variety are stored in
`cards.json` to prevent variant mix-ups.

## Commands

```powershell
$py = 'C:\Users\jlims\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py tracker.py report --days 7
& $py tracker.py dashboard --days 30
& $py tracker.py migrate-market-history
& $py tracker.py record --card mega-gengar-ex-240 --psa10-pop 33000 `
  --estimate-usd 591 --observed-at 2026-08-10T09:00:00+09:00
& $py tracker.py record-market --card mega-gengar-ex-240 --date 2026-08-11 `
  --psa10-price 589 --raw-price 390 --psa10-pop 32984 `
  --sales-7d 120 --sales-30d 480 --lowest-listing 575 --listing-count 31
```

`record` rejects unknown cards and negative populations. It warns when the
population decreases or jumps by more than 25%, since those changes often mean
the wrong variant or a source error was captured. Identical observations are
deduplicated.

Open `dashboard.html` after running the dashboard command. Historical snapshots
are stored in `data/population_history.csv` so they remain portable.

## Reference rankings

Cards may include `reference_rank`, `reference_total_graded`, and
`reference_basis` in `cards.json`. These fields power the small “most graded”
badge on a card tile. They are contextual reference data only: the supplied
total is PSA grading submissions during the stated reference period, not the
card's current GEM MT 10 population. The large population number on each tile
continues to come from the latest verified PSA 10 snapshot.

TCG Stacked's most-valuable list ranks English cards by raw market price. For a
Japanese PSA 10 market universe, use the POKECA PSA Index constituents list;
its ordering is index weight, so it should not be presented as an equivalent
raw-price ranking without a separate price collection step.

## Mega Gengar ASI

The dashboard derives a 0–100 Accumulation Strength Indicator from daily rows
in `data/market_history.csv`. The persisted daily fields are `date`,
`psa10_price`, `raw_price`, `psa10_pop`, `pop_change_30d`, `sales_7d`,
`sales_30d`, `lowest_listing`, `listing_count`, `raw_psa_spread`, `APS`, and
`ASI` (plus `card_id` so the file remains usable for every tracked card).

ASI weights are price-vs-pop absorption 30%, sales velocity 20%, PSA10
population growth 15%, price structure 15%, listing absorption 10%, and the
raw/PSA10 spread 10%. Missing components are excluded and the available
weights are normalized; the UI exposes both coverage and confidence rather
than treating missing live-source data as zero.

APS is the price percentage change normalized to exactly +10% PSA10 population
growth: `price_change_pct * 10 / pop_change_pct`. Its regimes are strong above
0, healthy from 0 through -3, moderate below -3 through -7, weak below -7
through -12, and very weak below -12.

ASI actions are BUY/HOLD (80–100), ACCUMULATE (65–79), WAIT (45–64), AVOID
(30–44), and WAIT FOR FLOOR (0–29). The accumulation alert requires all three
signals: rising PSA10 population, no new recent price low, and stable/rising
sales velocity.

Every card tile includes its own ASI summary and dual-axis price/population
history with ASI regime markers. The Component breakdown button opens a native
modal containing that card's six weighted signals; it closes with its close
button, Escape, or a backdrop click.

`migrate-market-history` is idempotent. It collapses existing timestamped
population observations to one row per card/day and maps the last daily PSA
estimate into `psa10_price`. The dashboard also runs this migration before it
renders, and each future PSA collector run upserts the same daily row.

Keep all price inputs for a card in one currency. Existing PSA estimate values
are USD; if the tracker is switched to JPY market prices, migrate the historical
price values at the same time rather than mixing currencies.

## Data-collection rule

Only record a value after the PSA page confirms all of the following:

1. Expected year and set
2. Expected subject and card number
3. Expected variety
4. Item grade `GEM MT 10`
5. A numeric `PSA POPULATION`

If the PSA text record is incomplete, stale-looking, or missing population,
record nothing and report the failed check. Never substitute zero.

PSA states that its population report is updated daily. Its public API does not
reliably expose population counts, and direct automated HTTP requests may be
blocked, so the recurring Codex monitor verifies the rendered cert pages.

## Raspberry Pi deployment

The Pi runs `pi_collector.py` against a text-rendered copy of each public PSA
certification record. The collector validates all identity fields from
`cards.json`, requires GEM MT 10 and a numeric population, refreshes the official
front-slab image when available, records only validated values, and rebuilds the
dashboard. User-level systemd services serve the dashboard on port 8080.

The scheduled collector checks every configured card once per day at 21:00
Asia/Tokyo, opens a fresh browser for each card, and waits 45 seconds between
requests. A missed run starts when the Pi returns online. If the mirror or PSA
record is unavailable, mismatched, or incomplete, the collector records nothing
for that card, reports the failure, and continues with the remaining cards.

Scheduled output and errors are appended to `data/collector.log`. Check recent
failures with `tail -n 100 data/collector.log`; systemd journal output remains
available with `journalctl --user -u card-pop-collector.service`.
