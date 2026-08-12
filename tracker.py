#!/usr/bin/env python3
"""Record and report PSA population snapshots without third-party packages."""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CARDS_PATH = ROOT / "cards.json"
HISTORY_PATH = ROOT / "data" / "population_history.csv"
MARKET_HISTORY_PATH = ROOT / "data" / "market_history.csv"
DASHBOARD_PATH = ROOT / "dashboard.html"
ABOUT_ASI_PATH = ROOT / "about-asi.html"
FIELDNAMES = [
    "observed_at",
    "card_id",
    "psa10_population",
    "psa9_population",
    "total_population",
    "psa_estimate_usd",
    "source_url",
    "verified_identity",
    "notes",
]
MARKET_FIELDNAMES = [
    "date",
    "card_id",
    "psa10_price",
    "raw_price",
    "psa10_pop",
    "pop_change_30d",
    "sales_7d",
    "sales_30d",
    "lowest_listing",
    "listing_count",
    "raw_psa_spread",
    "APS",
    "ASI",
]
ASI_WEIGHTS = {
    "Price vs POP absorption": 0.30,
    "Sales velocity": 0.20,
    "PSA10 population growth": 0.15,
    "Price structure": 0.15,
    "Listing absorption": 0.10,
    "Raw/PSA10 spread": 0.10,
}


def load_cards() -> dict[str, dict[str, str]]:
    return json.loads(CARDS_PATH.read_text(encoding="utf-8"))


def parse_time(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone offset")
    return parsed


def load_history() -> list[dict[str, str]]:
    if not HISTORY_PATH.exists():
        return []
    with HISTORY_PATH.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows.sort(key=lambda row: parse_time(row["observed_at"]))
    return rows


def integer_or_blank(value: int | None) -> str:
    return "" if value is None else str(value)


def decimal_or_blank(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def optional_float(value: str | float | int | None) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def optional_int(value: str | float | int | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(float(value))


def recorded_snapshot_message(
    card_name: str, psa10_population: int, estimate_usd: float | None
) -> str:
    price = f"${estimate_usd:,.2f} USD" if estimate_usd is not None else "n/a"
    return (
        f"Recorded {card_name}: PSA 10 population {psa10_population:,}; "
        f"PSA estimate {price}"
    )


def load_market_history() -> list[dict[str, str]]:
    if not MARKET_HISTORY_PATH.exists():
        return []
    with MARKET_HISTORY_PATH.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return sorted(rows, key=lambda row: (row["date"], row["card_id"]))


def write_market_history(rows: list[dict[str, str]]) -> None:
    MARKET_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MARKET_HISTORY_PATH.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MARKET_FIELDNAMES)
        writer.writeheader()
        writer.writerows(
            {field: row.get(field, "") for field in MARKET_FIELDNAMES}
            for row in sorted(rows, key=lambda item: (item["date"], item["card_id"]))
        )
    temporary.replace(MARKET_HISTORY_PATH)


def prior_window_row(
    rows: list[dict[str, str]], current_index: int, days: int, required_field: str
) -> dict[str, str] | None:
    current = rows[current_index]
    cutoff = datetime.fromisoformat(current["date"]).date() - timedelta(days=days)
    earlier = [
        row
        for row in rows[:current_index]
        if row.get(required_field) and datetime.fromisoformat(row["date"]).date() <= cutoff
    ]
    if earlier:
        return earlier[-1]
    return None


def percentage_change(current: float | None, prior: float | None) -> float | None:
    if current is None or prior in (None, 0):
        return None
    return (current - prior) / prior * 100


def projected_window_change(
    rows: list[dict[str, str]], current_index: int, days: int, field: str
) -> tuple[float | None, int | None]:
    """Linearly project a real short-baseline change; never persist it as history."""
    current = optional_float(rows[current_index].get(field))
    current_date = datetime.fromisoformat(rows[current_index]["date"]).date()
    prior = next(
        (
            row
            for row in rows[:current_index]
            if optional_float(row.get(field)) is not None
            and datetime.fromisoformat(row["date"]).date() < current_date
        ),
        None,
    )
    if current is None or prior is None:
        return None, None
    elapsed = (current_date - datetime.fromisoformat(prior["date"]).date()).days
    observed_change = percentage_change(current, optional_float(prior.get(field)))
    if elapsed < 1 or observed_change is None:
        return None, None
    return observed_change * days / elapsed, elapsed


def calculate_aps(price_change_pct: float | None, pop_change_pct: float | None) -> float | None:
    """Return price percentage change normalized to +10% PSA10 population growth."""
    if price_change_pct is None or pop_change_pct is None or pop_change_pct <= 0:
        return None
    return price_change_pct * 10 / pop_change_pct


def aps_regime(aps: float | None) -> str:
    if aps is None:
        return "Unavailable"
    if aps > 0:
        return "Strong"
    if aps >= -3:
        return "Healthy"
    if aps >= -7:
        return "Moderate"
    if aps >= -12:
        return "Weak"
    return "Very weak"


def asi_zone(score: float | None) -> tuple[str, str]:
    if score is None:
        return "INSUFFICIENT DATA", "Unavailable"
    if score >= 80:
        return "BUY / HOLD", "Strong accumulation / markup"
    if score >= 65:
        return "ACCUMULATE", "Accumulation"
    if score >= 45:
        return "WAIT", "Neutral"
    if score >= 30:
        return "AVOID", "Distribution"
    return "WAIT FOR FLOOR", "Capitulation"


def asi_presentation(
    score: float | None, provisional: bool, projection_days: int | None
) -> tuple[float | None, str, str]:
    """Apply confidence-aware labels without changing the underlying calculation."""
    if not provisional:
        action, phase = asi_zone(score)
        return score, action, phase
    if score is None:
        return None, "WATCH", "Insufficient data"
    if score >= 65:
        return score, "POSITIVE WATCH", "Provisional strength"
    if score >= 45:
        return score, "WATCH", "Provisional neutral"
    return score, "CAUTION", "Provisional weakness"


def score_aps(value: float | None) -> float | None:
    if value is None:
        return None
    if value > 0:
        return 100
    if value >= -3:
        return 80
    if value >= -7:
        return 55
    if value >= -12:
        return 30
    return 10


def score_sales(row: dict[str, str]) -> float | None:
    sales_7d = optional_float(row.get("sales_7d"))
    sales_30d = optional_float(row.get("sales_30d"))
    if sales_7d is None or sales_30d in (None, 0):
        return None
    pace = sales_7d * 30 / (sales_30d * 7)
    if pace >= 1.25:
        return 100
    if pace >= 1:
        return 80
    if pace >= 0.75:
        return 55
    if pace >= 0.5:
        return 30
    return 10


def score_pop_growth(value: float | None) -> float | None:
    if value is None:
        return None
    if value <= 0:
        return 100
    if value <= 2:
        return 90
    if value <= 5:
        return 75
    if value <= 10:
        return 55
    if value <= 20:
        return 30
    return 10


def score_price_structure(price_change_pct: float | None) -> float | None:
    if price_change_pct is None:
        return None
    if price_change_pct >= 5:
        return 100
    if price_change_pct >= 0:
        return 80
    if price_change_pct >= -3:
        return 65
    if price_change_pct >= -7:
        return 45
    if price_change_pct >= -12:
        return 25
    return 5


def score_listing_absorption(
    card_rows: list[dict[str, str]], current_index: int
) -> float | None:
    current = optional_float(card_rows[current_index].get("listing_count"))
    prior = prior_window_row(card_rows, current_index, 7, "listing_count")
    change = percentage_change(current, optional_float(prior.get("listing_count")) if prior else None)
    if change is None:
        return None
    if change <= -10:
        return 100
    if change <= 0:
        return 80
    if change <= 10:
        return 55
    if change <= 25:
        return 30
    return 10


def score_raw_spread(value: float | None) -> float | None:
    if value is None:
        return None
    if 30 <= value <= 80:
        return 100
    if 20 <= value <= 100:
        return 80
    if 10 <= value <= 120:
        return 55
    if value > 0:
        return 30
    return 10


def calculate_asi(
    row: dict[str, str], card_rows: list[dict[str, str]], current_index: int
) -> dict[str, object]:
    price = optional_float(row.get("psa10_price"))
    price_prior = prior_window_row(card_rows, current_index, 30, "psa10_price")
    price_change = percentage_change(
        price, optional_float(price_prior.get("psa10_price")) if price_prior else None
    )
    pop_change = optional_float(row.get("pop_change_30d"))
    aps = optional_float(row.get("APS"))
    projected_fields: list[str] = []
    projection_spans: list[int] = []
    if price_change is None:
        price_change, elapsed = projected_window_change(
            card_rows, current_index, 30, "psa10_price"
        )
        if price_change is not None:
            projected_fields.append("PSA estimate")
            projection_spans.append(elapsed or 0)
    if pop_change is None:
        pop_change, elapsed = projected_window_change(
            card_rows, current_index, 30, "psa10_pop"
        )
        if pop_change is not None:
            projected_fields.append("PSA 10 population")
            projection_spans.append(elapsed or 0)
    if aps is None:
        aps = calculate_aps(price_change, pop_change)
    components: dict[str, float | None] = {
        "Price vs POP absorption": score_aps(aps),
        "Sales velocity": score_sales(row),
        "PSA10 population growth": score_pop_growth(pop_change),
        "Price structure": score_price_structure(price_change),
        "Listing absorption": score_listing_absorption(card_rows, current_index),
        "Raw/PSA10 spread": score_raw_spread(optional_float(row.get("raw_psa_spread"))),
    }
    available_weight = sum(
        ASI_WEIGHTS[name] for name, value in components.items() if value is not None
    )
    score = None
    if available_weight:
        score = sum(
            value * ASI_WEIGHTS[name]
            for name, value in components.items()
            if value is not None
        ) / available_weight
    first_date = datetime.fromisoformat(card_rows[0]["date"]).date()
    current_date = datetime.fromisoformat(row["date"]).date()
    span = (current_date - first_date).days
    if available_weight >= 0.8 and span >= 30:
        confidence = "High"
    elif available_weight >= 0.5 and span >= 14:
        confidence = "Medium"
    else:
        confidence = "Low"
    return {
        "score": score,
        "components": components,
        "available_weight": available_weight,
        "confidence": confidence,
        "price_change_30d": price_change,
        "pop_change_30d": pop_change,
        "aps": aps,
        "provisional": bool(projected_fields),
        "projected_fields": projected_fields,
        "projection_days": min(projection_spans) if projection_spans else None,
    }


def recompute_market_history(rows: list[dict[str, str]]) -> None:
    card_ids = sorted({row["card_id"] for row in rows})
    for card_id in card_ids:
        selected = sorted(
            (row for row in rows if row["card_id"] == card_id), key=lambda row: row["date"]
        )
        for index, row in enumerate(selected):
            pop = optional_float(row.get("psa10_pop"))
            pop_prior = prior_window_row(selected, index, 30, "psa10_pop")
            pop_change = percentage_change(
                pop, optional_float(pop_prior.get("psa10_pop")) if pop_prior else None
            )
            row["pop_change_30d"] = decimal_or_blank(pop_change)
            raw = optional_float(row.get("raw_price"))
            price = optional_float(row.get("psa10_price"))
            spread = percentage_change(price, raw)
            row["raw_psa_spread"] = decimal_or_blank(spread)
            price_prior = prior_window_row(selected, index, 30, "psa10_price")
            price_change = percentage_change(
                price, optional_float(price_prior.get("psa10_price")) if price_prior else None
            )
            aps = calculate_aps(price_change, pop_change)
            row["APS"] = decimal_or_blank(aps)
            result = calculate_asi(row, selected, index)
            row["ASI"] = decimal_or_blank(
                None if result["provisional"] else result["score"]
            )


def upsert_market_snapshot(card_id: str, date: str, **values: float | int | None) -> None:
    datetime.fromisoformat(date).date()
    rows = load_market_history()
    row = next(
        (item for item in rows if item["card_id"] == card_id and item["date"] == date), None
    )
    if row is None:
        row = {field: "" for field in MARKET_FIELDNAMES}
        row.update({"date": date, "card_id": card_id})
        rows.append(row)
    integer_fields = {"psa10_pop", "sales_7d", "sales_30d", "listing_count"}
    for field, value in values.items():
        if field not in MARKET_FIELDNAMES or value is None:
            continue
        row[field] = integer_or_blank(int(value)) if field in integer_fields else decimal_or_blank(float(value))
    recompute_market_history(rows)
    write_market_history(rows)


def migrate_population_history(quiet: bool = False) -> int:
    rows = load_market_history()
    by_key = {(row["date"], row["card_id"]): row for row in rows}
    migrated = 0
    for observation in load_history():
        date = parse_time(observation["observed_at"]).date().isoformat()
        key = (date, observation["card_id"])
        row = by_key.get(key)
        if row is None:
            row = {field: "" for field in MARKET_FIELDNAMES}
            row.update({"date": date, "card_id": observation["card_id"]})
            rows.append(row)
            by_key[key] = row
        row["psa10_pop"] = observation.get("psa10_population", "")
        if observation.get("psa_estimate_usd"):
            row["psa10_price"] = observation["psa_estimate_usd"]
        migrated += 1
    recompute_market_history(rows)
    write_market_history(rows)
    if not quiet:
        print(f"Migrated {migrated} population observation(s) into daily market history.")
    return 0


def record(args: argparse.Namespace) -> int:
    cards = load_cards()
    if args.card not in cards:
        raise ValueError(f"unknown card id: {args.card}")
    if args.psa10_pop < 0:
        raise ValueError("population cannot be negative")
    observed = parse_time(args.observed_at)
    history = load_history()
    previous = next(
        (row for row in reversed(history) if row["card_id"] == args.card), None
    )
    warning = ""
    if previous:
        prior_pop = int(previous["psa10_population"])
        if args.psa10_pop < prior_pop:
            warning = f"WARNING: population decreased by {prior_pop - args.psa10_pop:,}"
        elif prior_pop and args.psa10_pop > prior_pop * 1.25:
            warning = "WARNING: population jumped more than 25%; verify the card identity"

    duplicate = any(
        row["card_id"] == args.card
        and row["observed_at"] == observed.isoformat()
        and int(row["psa10_population"]) == args.psa10_pop
        for row in history
    )
    if duplicate:
        print("Identical snapshot already exists; nothing recorded.")
        return 0

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not HISTORY_PATH.exists() or HISTORY_PATH.stat().st_size == 0
    with HISTORY_PATH.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "observed_at": observed.isoformat(),
                "card_id": args.card,
                "psa10_population": args.psa10_pop,
                "psa9_population": integer_or_blank(args.psa9_pop),
                "total_population": integer_or_blank(args.total_pop),
                "psa_estimate_usd": decimal_or_blank(args.estimate_usd),
                "source_url": cards[args.card]["psa_url"],
                "verified_identity": str(args.verified_identity).lower(),
                "notes": args.notes or warning,
            }
        )
    upsert_market_snapshot(
        args.card,
        observed.date().isoformat(),
        psa10_pop=args.psa10_pop,
        psa10_price=args.estimate_usd,
    )
    print(
        recorded_snapshot_message(
            cards[args.card]["name"], args.psa10_pop, args.estimate_usd
        )
    )
    if warning:
        print(warning)
    return 0


def record_market(args: argparse.Namespace) -> int:
    cards = load_cards()
    if args.card not in cards:
        raise ValueError(f"unknown card id: {args.card}")
    values = {
        "psa10_price": args.psa10_price,
        "raw_price": args.raw_price,
        "psa10_pop": args.psa10_pop,
        "sales_7d": args.sales_7d,
        "sales_30d": args.sales_30d,
        "lowest_listing": args.lowest_listing,
        "listing_count": args.listing_count,
    }
    if not any(value is not None for value in values.values()):
        raise ValueError("record-market requires at least one market value")
    for name, value in values.items():
        if value is not None and value < 0:
            raise ValueError(f"{name} cannot be negative")
    upsert_market_snapshot(args.card, args.date, **values)
    print(f"Recorded daily market snapshot for {cards[args.card]['name']} on {args.date}.")
    return 0


def card_rows(history: list[dict[str, str]], card_id: str) -> list[dict[str, str]]:
    return [row for row in history if row["card_id"] == card_id]


def change_stats(rows: list[dict[str, str]], days: int) -> dict[str, float | int | bool | None]:
    if not rows:
        return {"current": None, "prior": None, "delta": None, "pct": None, "daily": None, "window_complete": False}
    latest = rows[-1]
    latest_time = parse_time(latest["observed_at"])
    cutoff = latest_time - timedelta(days=days)
    candidates = [row for row in rows if parse_time(row["observed_at"]) <= cutoff]
    prior = candidates[-1] if candidates else (rows[0] if len(rows) > 1 else None)
    current_pop = int(latest["psa10_population"])
    if not prior:
        return {"current": current_pop, "prior": None, "delta": None, "pct": None, "daily": None, "window_complete": False}
    prior_pop = int(prior["psa10_population"])
    elapsed = max((latest_time - parse_time(prior["observed_at"])).total_seconds() / 86400, 1)
    delta = current_pop - prior_pop
    return {
        "current": current_pop,
        "prior": prior_pop,
        "delta": delta,
        "pct": (delta / prior_pop * 100) if prior_pop else None,
        "daily": delta / elapsed,
        "window_complete": bool(candidates),
    }


def estimate_stats(rows: list[dict[str, str]], days: int) -> dict[str, float | bool | None]:
    priced_rows = [row for row in rows if row.get("psa_estimate_usd")]
    if not priced_rows:
        return {"current": None, "prior": None, "delta": None, "pct": None, "window_complete": False}
    latest = priced_rows[-1]
    latest_time = parse_time(latest["observed_at"])
    cutoff = latest_time - timedelta(days=days)
    candidates = [row for row in priced_rows if parse_time(row["observed_at"]) <= cutoff]
    prior = candidates[-1] if candidates else (priced_rows[0] if len(priced_rows) > 1 else None)
    current_price = float(latest["psa_estimate_usd"])
    if not prior:
        return {"current": current_price, "prior": None, "delta": None, "pct": None, "window_complete": False}
    prior_price = float(prior["psa_estimate_usd"])
    delta = current_price - prior_price
    return {
        "current": current_price,
        "prior": prior_price,
        "delta": delta,
        "pct": (delta / prior_price * 100) if prior_price else None,
        "window_complete": bool(candidates),
    }


def fmt(value: float | int | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:+,.2f}{suffix}"
    return f"{value:+,}{suffix}"


def report_text(days: int) -> str:
    cards = load_cards()
    history = load_history()
    lines = [f"PSA population report ({days}-day window)", ""]
    for card_id, card in cards.items():
        rows = card_rows(history, card_id)
        stats = change_stats(rows, days)
        latest = rows[-1] if rows else None
        lines.extend(
            [
                card["name"],
                f"  PSA 10 population: {stats['current']:,}" if stats["current"] is not None else "  PSA 10 population: n/a",
                f"  Change: {fmt(stats['delta'])} ({fmt(stats['pct'], '%')})",
                f"  Average daily additions: {fmt(stats['daily'])}",
                f"  Last verified: {latest['observed_at'] if latest else 'n/a'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def sparkline_svg(values: list[int], width: int = 520, height: int = 150) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    span = max(high - low, 1)
    step = width / max(len(values) - 1, 1)
    points = " ".join(
        f"{i * step:.1f},{height - ((value - low) / span * (height - 20)) - 10:.1f}"
        for i, value in enumerate(values)
    )
    marks = (
        f'<circle cx="{width / 2:.1f}" cy="{height / 2:.1f}" r="6" fill="currentColor"/>'
        if len(values) == 1
        else f'<polyline points="{points}" fill="none" stroke="currentColor" stroke-width="4" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Population history">'
        f'{marks}</svg>'
    )


def comparison_svg(items: list[tuple[str, int]], width: int = 1000) -> str:
    """Create a directly labelled horizontal bar chart for current populations."""
    if not items:
        return ""
    items = sorted(items, key=lambda item: item[1], reverse=True)
    left, right, top, row_height = 245, 120, 34, 48
    plot_width = width - left - right
    height = top + row_height * len(items) + 48
    maximum = max(value for _, value in items)
    ticks = 4
    parts = [
        f'<svg class="comparison-chart" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="comparison-title comparison-desc">',
        '<title id="comparison-title">Current PSA 10 population comparison</title>',
        '<desc id="comparison-desc">Horizontal bars compare the latest verified PSA 10 population of each tracked card.</desc>',
    ]
    for tick in range(ticks + 1):
        value = maximum * tick / ticks
        x = left + plot_width * tick / ticks
        parts.append(
            f'<line class="gridline" x1="{x:.1f}" y1="{top - 16}" x2="{x:.1f}" y2="{height - 38}"/>'
            f'<text class="axis-label" x="{x:.1f}" y="{height - 14}" text-anchor="middle">{value / 1000:.0f}k</text>'
        )
    for index, (name, value) in enumerate(items):
        y = top + index * row_height
        bar_width = plot_width * value / maximum if maximum else 0
        parts.append(
            f'<text class="bar-label" x="{left - 14}" y="{y + 21}" text-anchor="end">{html.escape(name)}</text>'
            f'<rect class="bar" x="{left}" y="{y}" width="{bar_width:.1f}" height="28" rx="5">'
            f'<title>{html.escape(name)}: {value:,} PSA 10</title></rect>'
            f'<text class="bar-value" x="{left + bar_width + 10:.1f}" y="{y + 21}">{value:,}</text>'
        )
    parts.append('</svg>')
    return "".join(parts)


def comparison_mobile_bars(items: list[tuple[str, int]]) -> str:
    if not items:
        return ""
    items = sorted(items, key=lambda item: item[1], reverse=True)
    maximum = max(value for _, value in items)
    rows = []
    for name, value in items:
        percent = value / maximum * 100 if maximum else 0
        rows.append(
            f'<div class="mobile-bar-row"><div class="mobile-bar-heading">'
            f'<span>{html.escape(name)}</span><strong>{value:,}</strong></div>'
            f'<div class="mobile-bar-track"><span style="width:{percent:.2f}%"></span></div></div>'
        )
    return f'<div class="comparison-mobile" role="img" aria-label="Current PSA 10 population comparison">{"".join(rows)}</div>'


def zone_class(score: float | None) -> str:
    if score is None:
        return "missing"
    if score >= 80:
        return "buy"
    if score >= 65:
        return "accumulate"
    if score >= 45:
        return "wait"
    if score >= 30:
        return "avoid"
    return "floor"


def historical_market_svg(
    rows: list[dict[str, str]], card_name: str, chart_id: str,
    width: int = 1000, height: int = 330
) -> str:
    usable = [
        row for row in rows if row.get("psa10_price") or row.get("psa10_pop") or row.get("ASI")
    ]
    if not usable:
        return '<p class="empty">No daily market history yet.</p>'
    left, right, top, bottom = 76, 82, 30, 54
    plot_width, plot_height = width - left - right, height - top - bottom
    prices = [optional_float(row.get("psa10_price")) for row in usable]
    pops = [optional_float(row.get("psa10_pop")) for row in usable]
    price_values = [value for value in prices if value is not None]
    pop_values = [value for value in pops if value is not None]

    def bounds(values: list[float]) -> tuple[float, float]:
        if not values:
            return 0, 1
        low, high = min(values), max(values)
        padding = max((high - low) * 0.12, high * 0.02, 1)
        return max(0, low - padding), high + padding

    price_low, price_high = bounds(price_values)
    pop_low, pop_high = bounds(pop_values)

    def x(index: int) -> float:
        return left + plot_width * index / max(len(usable) - 1, 1)

    def y(value: float, low: float, high: float) -> float:
        return top + plot_height - (value - low) / max(high - low, 1) * plot_height

    def path(values: list[float | None], low: float, high: float, css_class: str) -> str:
        segments: list[list[str]] = []
        current: list[str] = []
        for index, value in enumerate(values):
            if value is None:
                if current:
                    segments.append(current)
                    current = []
                continue
            command = "M" if not current else "L"
            current.append(f"{command}{x(index):.1f},{y(value, low, high):.1f}")
        if current:
            segments.append(current)
        return "".join(f'<path class="{css_class}" d="{" ".join(segment)}"/>' for segment in segments)

    parts = [
        f'<svg class="market-chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="market-chart-title-{html.escape(chart_id)} market-chart-desc-{html.escape(chart_id)}">',
        f'<title id="market-chart-title-{html.escape(chart_id)}">{html.escape(card_name)} PSA estimate, PSA 10 population, and ASI history</title>',
        f'<desc id="market-chart-desc-{html.escape(chart_id)}">PSA estimate in USD uses the left axis, population uses the right axis, and colored markers show ASI zones.</desc>',
    ]
    for tick in range(5):
        fraction = tick / 4
        grid_y = top + plot_height * fraction
        price_label = price_high - (price_high - price_low) * fraction
        pop_label = pop_high - (pop_high - pop_low) * fraction
        parts.append(
            f'<line class="market-grid" x1="{left}" y1="{grid_y:.1f}" x2="{width-right}" y2="{grid_y:.1f}"/>'
            f'<text class="axis left-axis" x="{left-10}" y="{grid_y+4:.1f}" text-anchor="end">${price_label:,.0f}</text>'
            f'<text class="axis right-axis" x="{width-right+10}" y="{grid_y+4:.1f}">{pop_label:,.0f}</text>'
        )
    parts.append(path(prices, price_low, price_high, "price-line"))
    parts.append(path(pops, pop_low, pop_high, "pop-line"))
    for index, value in enumerate(prices):
        if value is not None:
            parts.append(
                f'<circle class="price-point" cx="{x(index):.1f}" '
                f'cy="{y(value, price_low, price_high):.1f}" r="4"/>'
            )
    for index, value in enumerate(pops):
        if value is not None:
            parts.append(
                f'<circle class="pop-point" cx="{x(index):.1f}" '
                f'cy="{y(value, pop_low, pop_high):.1f}" r="4"/>'
            )
    for index, row in enumerate(usable):
        score = optional_float(row.get("ASI"))
        if score is None:
            continue
        marker_y = (
            y(prices[index], price_low, price_high)
            if prices[index] is not None
            else top + plot_height
        )
        action, phase = asi_zone(score)
        parts.append(
            f'<circle class="asi-marker {zone_class(score)}" cx="{x(index):.1f}" cy="{marker_y:.1f}" r="6">'
            f'<title>{row["date"]}: ASI {score:.0f}, {phase} ({action})</title></circle>'
        )
    label_indexes = sorted({0, len(usable) - 1})
    for index in label_indexes:
        parts.append(
            f'<text class="axis date-axis" x="{x(index):.1f}" y="{height-15}" '
            f'text-anchor="{"start" if index == 0 else "end"}">{html.escape(usable[index]["date"])}</text>'
        )
    parts.append('</svg>')
    return "".join(parts)


def historical_market_preview_svg(
    rows: list[dict[str, str]], card_name: str, chart_id: str,
    width: int = 520, height: int = 120
) -> str:
    """Render a compact price/population chart that opens the detailed modal."""
    usable = [
        row for row in rows if row.get("psa10_price") or row.get("psa10_pop")
    ]
    if not usable:
        return ""
    padding_x, padding_y = 12, 12
    plot_width, plot_height = width - padding_x * 2, height - padding_y * 2
    prices = [optional_float(row.get("psa10_price")) for row in usable]
    pops = [optional_float(row.get("psa10_pop")) for row in usable]

    def bounds(values: list[float | None]) -> tuple[float, float]:
        present = [value for value in values if value is not None]
        if not present:
            return 0, 1
        low, high = min(present), max(present)
        padding = max((high - low) * 0.12, high * 0.02, 1)
        return max(0, low - padding), high + padding

    price_low, price_high = bounds(prices)
    pop_low, pop_high = bounds(pops)

    def x(index: int) -> float:
        return padding_x + plot_width * index / max(len(usable) - 1, 1)

    def y(value: float, low: float, high: float) -> float:
        return padding_y + plot_height - (value - low) / max(high - low, 1) * plot_height

    def path(values: list[float | None], low: float, high: float, css_class: str) -> str:
        segments: list[list[str]] = []
        current: list[str] = []
        for index, value in enumerate(values):
            if value is None:
                if current:
                    segments.append(current)
                    current = []
                continue
            current.append(f'{"M" if not current else "L"}{x(index):.1f},{y(value, low, high):.1f}')
        if current:
            segments.append(current)
        return "".join(f'<path class="{css_class}" d="{" ".join(segment)}"/>' for segment in segments)

    title_id = f"preview-title-{html.escape(chart_id)}"
    desc_id = f"preview-desc-{html.escape(chart_id)}"
    parts = [
        f'<svg class="mini-market-chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="{title_id} {desc_id}">',
        f'<title id="{title_id}">{html.escape(card_name)} recent PSA estimate and PSA 10 population</title>',
        f'<desc id="{desc_id}">Yellow shows the PSA estimate in USD and blue shows PSA 10 population. Select the chart for detailed history.</desc>',
    ]
    for fraction in (0.25, 0.5, 0.75):
        grid_y = padding_y + plot_height * fraction
        parts.append(
            f'<line class="mini-grid" x1="{padding_x}" y1="{grid_y:.1f}" '
            f'x2="{width-padding_x}" y2="{grid_y:.1f}"/>'
        )
    parts.append(path(prices, price_low, price_high, "price-line"))
    parts.append(path(pops, pop_low, pop_high, "pop-line"))
    for values, low, high, css_class in (
        (prices, price_low, price_high, "price-point"),
        (pops, pop_low, pop_high, "pop-point"),
    ):
        for index, value in enumerate(values):
            if value is not None:
                parts.append(
                    f'<circle class="{css_class}" cx="{x(index):.1f}" '
                    f'cy="{y(value, low, high):.1f}" r="3"/>'
                )
    parts.append("</svg>")
    return "".join(parts)


def asi_change(rows: list[dict[str, str]], days: int = 30) -> float | None:
    scored = [row for row in rows if row.get("ASI")]
    if len(scored) < 2:
        return None
    cutoff = datetime.fromisoformat(scored[-1]["date"]).date() - timedelta(days=days)
    candidates = [row for row in scored[:-1] if datetime.fromisoformat(row["date"]).date() <= cutoff]
    if not candidates:
        return None
    prior = candidates[-1]
    return optional_float(scored[-1]["ASI"]) - optional_float(prior["ASI"])


def accumulation_alert(rows: list[dict[str, str]]) -> bool:
    if len(rows) < 2:
        return False
    current = rows[-1]
    pop_growth = optional_float(current.get("pop_change_30d"))
    current_price = optional_float(current.get("psa10_price"))
    prior_prices = [optional_float(row.get("psa10_price")) for row in rows[:-1]]
    prior_prices = [price for price in prior_prices if price is not None]
    sales_7d = optional_float(current.get("sales_7d"))
    sales_30d = optional_float(current.get("sales_30d"))
    if (
        pop_growth is None
        or pop_growth <= 0
        or current_price is None
        or not prior_prices
        or sales_7d is None
    ):
        return False
    price_stopped_new_lows = current_price >= min(prior_prices[-30:])
    prior_sales = [optional_float(row.get("sales_7d")) for row in rows[:-1]]
    prior_sales = [value for value in prior_sales if value is not None]
    velocity_stable = (
        sales_30d not in (None, 0) and sales_7d >= sales_30d * 7 / 30
    ) or (bool(prior_sales) and sales_7d >= prior_sales[-1])
    return price_stopped_new_lows and velocity_stable


def component_breakdown(result: dict[str, object]) -> str:
    components = result["components"]
    rows = []
    for name, weight in ASI_WEIGHTS.items():
        score = components[name]
        score_html = f"{score:.0f}/100" if score is not None else "n/a"
        rows.append(
            f'<tr><td>{html.escape(name)}</td><td>{weight:.0%}</td><td>{score_html}</td></tr>'
        )
    return (
        '<table class="components"><thead><tr><th>Component</th><th>Weight</th><th>Signal</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def card_asi_panel(
    card_id: str, card_name: str, market_history: list[dict[str, str]]
) -> str:
    rows = [row for row in market_history if row["card_id"] == card_id]
    if not rows:
        return (
            '<div class="card-asi missing" data-asi-card="true">'
            '<div class="eyebrow">ACCUMULATION STRENGTH</div>'
            '<div class="card-asi-score">ASI n/a<small>/100</small></div>'
            '<div class="card-asi-phase">Unavailable · <strong>INSUFFICIENT DATA</strong></div>'
            '</div>'
        )
    latest = rows[-1]
    result = calculate_asi(latest, rows, len(rows) - 1)
    calculated_score = result["score"]
    score, action, phase = asi_presentation(
        calculated_score, result["provisional"], result["projection_days"]
    )
    change = asi_change(rows)
    aps = result["aps"]
    score_text = f"{score:.0f}" if score is not None else "n/a"
    change_text = f"{change:+.1f}" if change is not None else "n/a"
    aps_text = f"{aps:+.2f}" if aps is not None else "n/a"
    alert = (
        '<div class="card-alert">SUPPLY ABSORPTION DETECTED</div>'
        if accumulation_alert(rows)
        else ""
    )
    provisional = ""
    if result["provisional"] and result["projection_days"] >= 7:
        fields = " and ".join(result["projected_fields"])
        provisional = (
            '<div class="provisional-note"><strong>PROVISIONAL</strong> '
            f'{result["projection_days"]}-day observed trend projected to 30 days '
            f'for {html.escape(fields)}. Definitive labels require 30 measured days. '
            'No predicted rows are stored.</div>'
        )
    return f'''<div class="card-asi {zone_class(score)}" data-asi-card="true" aria-label="ASI for {html.escape(card_name)}">
<div class="eyebrow">ACCUMULATION STRENGTH</div>
<div class="card-asi-heading"><div class="card-asi-score">ASI {score_text}<small>/100</small></div><div class="card-asi-phase">{html.escape(phase)} · <strong>{html.escape(action)}</strong></div></div>
{provisional}
{alert}
<button class="breakdown-button" type="button" aria-haspopup="dialog" aria-controls="breakdown-{html.escape(card_id)}">Component breakdown</button>
<dialog class="component-modal" id="breakdown-{html.escape(card_id)}">
<div class="modal-head"><div><div class="eyebrow">ASI COMPONENTS</div><h2>{html.escape(card_name)}</h2><p>{phase} · <strong>{action}</strong></p></div><button class="modal-close" type="button" aria-label="Close component breakdown">×</button></div>
<div class="component-kpis"><span>30D ASI <b>{change_text}</b></span><span>Confidence <b>{result["confidence"]}</b></span><span>APS <b>{aps_text}</b><small>{aps_regime(aps)}</small></span></div>
{component_breakdown(result)}
<p class="coverage">Available input weight: {result["available_weight"]:.0%}. Missing inputs are excluded and the remaining weights are normalized.</p>
</dialog>
</div>'''


def about_asi_html(generated: str) -> str:
    component_rows = [
        ("Price vs POP absorption", "30%", "APS: PSA-estimate resilience while PSA 10 supply grows", "The estimate holds or rises as population expands"),
        ("Sales velocity", "20%", "7-day sales pace compared with the 30-day pace", "Recent sales are stable or accelerating"),
        ("PSA 10 population growth", "15%", "30-day growth in graded PSA 10 supply", "Supply growth is low or slowing"),
        ("Price structure", "15%", "30-day PSA estimate change", "The estimate is flat, rising, or forming higher lows"),
        ("Listing absorption", "10%", "7-day change in active listing count", "Available inventory is stable or falling"),
        ("Raw/PSA 10 spread", "10%", "Premium of a PSA 10 over the raw card", "A healthy grading premium remains intact"),
    ]
    component_html = "".join(
        f"<tr><td><strong>{html.escape(name)}</strong></td><td>{weight}</td>"
        f"<td>{html.escape(measure)}</td><td>{html.escape(signal)}</td></tr>"
        for name, weight, measure, signal in component_rows
    )
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect x='12' y='3' width='40' height='58' rx='5' fill='%23dbeafe' stroke='%238cc8ff' stroke-width='3'/%3E%3Crect x='17' y='9' width='30' height='10' rx='2' fill='%23dc2626'/%3E%3Crect x='19' y='24' width='26' height='30' rx='2' fill='%23101827' stroke='%238cc8ff'/%3E%3Ccircle cx='32' cy='39' r='8' fill='%23ffd166'/%3E%3C/svg%3E">
<meta name="description" content="How the Accumulation Strength Indicator measures supply absorption and demand for PSA 10 trading cards.">
<title>About ASI | PSA Card Market Monitor</title><style>
:root{{--bg:#0c111b;--panel:#151d2b;--panel2:#101827;--text:#f4f7fb;--muted:#9ba9bd;--line:#2b3950;--blue:#8cc8ff;--green:#7ee787;--yellow:#ffd166;--orange:#ed8936;--red:#f56565}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:radial-gradient(circle at top,#17233a,var(--bg) 48%);color:var(--text);font:16px/1.65 system-ui,sans-serif}}main{{max-width:1040px;margin:auto;padding:34px 24px 64px}}a{{color:var(--blue)}}.nav{{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:64px}}.nav a{{text-decoration:none}}.back{{padding:8px 13px;border:1px solid #455875;border-radius:999px}}.back:hover{{border-color:var(--blue);background:#1c2a40}}.eyebrow{{color:var(--muted);font-size:.78rem;letter-spacing:.12em;text-transform:uppercase}}h1{{max-width:760px;margin:8px 0 16px;font-size:clamp(2.6rem,7vw,5.4rem);line-height:.98;letter-spacing:-.055em}}.lede{{max-width:760px;margin:0;color:#c8d2e1;font-size:clamp(1.05rem,2vw,1.3rem)}}.hero{{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(240px,.7fr);gap:26px;align-items:stretch;margin-bottom:34px}}.score-card,.section,.callout{{background:rgba(21,29,43,.94);border:1px solid var(--line);border-radius:20px;box-shadow:0 18px 45px #0005}}.score-card{{display:flex;flex-direction:column;justify-content:center;padding:28px}}.score-card strong{{font-size:4.4rem;line-height:1;letter-spacing:-.07em}}.score-card strong small{{color:var(--muted);font-size:1rem;letter-spacing:0}}.score-card span{{color:var(--green);font-weight:800}}.section{{margin-top:24px;padding:clamp(22px,4vw,38px)}}.section h2{{margin:4px 0 12px;font-size:clamp(1.45rem,3vw,2.1rem)}}.section h3{{margin:28px 0 6px}}.section p{{color:#c8d2e1}}.callout{{margin:24px 0;padding:20px 22px;border-left:4px solid var(--blue)}}.callout strong{{display:block;margin-bottom:4px}}.formula{{margin:18px 0;padding:18px;border:1px solid #33445f;border-radius:12px;background:var(--panel2);overflow:auto;color:#dce9f8;font:600 .96rem/1.7 ui-monospace,SFMono-Regular,Consolas,monospace}}table{{width:100%;border-collapse:collapse;margin-top:18px;font-size:.92rem}}th,td{{padding:12px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{color:var(--muted);font-size:.76rem;letter-spacing:.06em;text-transform:uppercase}}td:nth-child(2){{white-space:nowrap;font-weight:800}}.zones td:first-child,.aps td:first-child{{font-weight:800}}.zone{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px}}.z-green{{background:var(--green)}}.z-yellow{{background:var(--yellow)}}.z-orange{{background:var(--orange)}}.z-red{{background:var(--red)}}.steps{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:18px}}.step{{padding:16px;border:1px solid var(--line);border-radius:14px;background:var(--panel2)}}.step b{{display:block;color:var(--blue);font-size:.78rem;letter-spacing:.08em}}.step strong{{display:block;margin:5px 0}}.fine{{color:var(--muted);font-size:.84rem}}footer{{margin-top:34px;color:var(--muted);font-size:.84rem}}@media(max-width:760px){{main{{padding:24px 14px 48px}}.nav{{margin-bottom:42px}}.hero{{grid-template-columns:1fr}}.score-card strong{{font-size:3.7rem}}.steps{{grid-template-columns:1fr}}.table-wrap{{overflow-x:auto}}table{{min-width:650px}}}}
</style></head><body><main>
<nav class="nav" aria-label="Primary"><a href="dashboard.html">PSA market monitor</a><a class="back" href="dashboard.html">← Dashboard</a></nav>
<div class="hero"><header><div class="eyebrow">Methodology</div><h1>What is ASI?</h1><p class="lede"><strong>ASI</strong> is the Accumulation Strength Indicator: a 0–100 score that estimates whether buyer demand is absorbing new PSA 10 supply for an individual card.</p></header><aside class="score-card" aria-label="Example ASI score"><div class="eyebrow">Example</div><strong>72<small>/100</small></strong><span>ACCUMULATE</span></aside></div>
<div class="callout"><strong>The central question</strong>When PSA 10 population continues to rise, does the market absorb those additional slabs without price, sales, and listings deteriorating?</div>
<section class="section"><div class="eyebrow">01 · Interpretation</div><h2>ASI is a supply-and-demand monitor</h2><p>A large PSA 10 population is not automatically bearish. A card can support a high population when transaction demand and listing absorption remain strong. ASI therefore focuses on the relationship between supply growth, price behavior, sales activity, and available inventory—not population alone.</p><p>ASI is calculated independently for every tracked card and date. It is a monitoring signal, not a price target, appraisal, or promise of future returns.</p></section>
<section class="section"><div class="eyebrow">02 · Formula</div><h2>Six weighted components</h2><p>Each available component is converted to a 0–100 signal score, multiplied by its target weight, and combined into the final ASI.</p><div class="formula">ASI = Σ(component score × component weight) ÷ Σ(available weights)</div><div class="table-wrap"><table><thead><tr><th>Component</th><th>Weight</th><th>What it measures</th><th>Bullish interpretation</th></tr></thead><tbody>{component_html}</tbody></table></div><div class="callout"><strong>Why absorption has the largest weight</strong>Price holding steady while graded supply expands is direct evidence that buyers are absorbing new slabs. It receives 30% of the model's target weight.</div></section>
<section class="section"><div class="eyebrow">03 · APS</div><h2>Absorption per +10% Supply</h2><p>APS normalizes the price change to a common +10% increase in PSA 10 population. This makes cards with different rates of population growth easier to compare.</p><div class="formula">APS = 30-day price change % × (10 ÷ 30-day PSA 10 population change %)</div><p>Example: if population rises 10.4% while price rises 1.2%, APS is approximately +1.15. A positive APS means price increased despite expanding supply.</p><div class="table-wrap"><table class="aps"><thead><tr><th>APS</th><th>Regime</th><th>Component score</th></tr></thead><tbody><tr><td>&gt; 0</td><td>Strong</td><td>100</td></tr><tr><td>0 to −3</td><td>Healthy</td><td>80</td></tr><tr><td>−3 to −7</td><td>Moderate</td><td>55</td></tr><tr><td>−7 to −12</td><td>Weak</td><td>30</td></tr><tr><td>&lt; −12</td><td>Very weak</td><td>10</td></tr></tbody></table></div><p class="fine">APS is unavailable when there is no valid 30-day comparison or population growth is zero or negative.</p></section>
<section class="section"><div class="eyebrow">04 · Score zones</div><h2>How to read the result</h2><div class="table-wrap"><table class="zones"><thead><tr><th>ASI</th><th>Action label</th><th>Market phase</th><th>Reading</th></tr></thead><tbody><tr><td><span class="zone z-green"></span>80–100</td><td>BUY / HOLD</td><td>Strong accumulation / markup</td><td>Broad evidence of demand absorbing supply</td></tr><tr><td><span class="zone z-green"></span>65–79</td><td>ACCUMULATE</td><td>Accumulation</td><td>Constructive signals, but not uniformly strong</td></tr><tr><td><span class="zone z-yellow"></span>45–64</td><td>WAIT</td><td>Neutral</td><td>Mixed evidence; watch for confirmation</td></tr><tr><td><span class="zone z-orange"></span>30–44</td><td>AVOID</td><td>Distribution</td><td>Supply pressure or weakening demand</td></tr><tr><td><span class="zone z-red"></span>0–29</td><td>WAIT FOR FLOOR</td><td>Capitulation</td><td>Demand is not yet absorbing available supply</td></tr></tbody></table></div></section>
<section class="section"><div class="eyebrow">05 · Confidence &amp; missing data</div><h2>A score is only as useful as its coverage</h2><div class="steps"><div class="step"><b>HIGH</b><strong>≥80% input weight</strong><span>At least 30 days of history</span></div><div class="step"><b>MEDIUM</b><strong>≥50% input weight</strong><span>At least 14 days of history</span></div><div class="step"><b>LOW</b><strong>Anything less</strong><span>Early or incomplete dataset</span></div></div><p>Once two dated observations are available, the dashboard calculates a provisional score with softened POSITIVE WATCH, WATCH, or CAUTION language. Definitive BUY / HOLD, AVOID, and WAIT FOR FLOOR labels require a measured 30-day baseline.</p><p>The provisional calculation linearly projects the real change observed since baseline to a 30-day equivalent, keeps confidence Low, and never writes predicted rows into history. It automatically switches to measured 30-day changes when enough real data exists.</p><p>Missing components are excluded instead of being scored as zero. The remaining weights are normalized, and the modal reports the available input weight. This prevents absent data from creating a false bearish signal, but a low-confidence ASI should not be treated like a fully observed score.</p></section>
<section class="section"><div class="eyebrow">06 · Accumulation alert</div><h2>Supply absorption detected</h2><p>The dashboard raises an accumulation alert only when three conditions agree:</p><ol><li>PSA 10 population is still rising over 30 days.</li><li>The current price has stopped making new lows within the available recent window.</li><li>Seven-day sales velocity is stable or rising relative to the 30-day pace or the prior observation.</li></ol><p>This alert is confirmation-oriented: it looks for stabilization while new supply is still entering the market.</p></section>
<section class="section"><div class="eyebrow">07 · Data &amp; limitations</div><h2>What the model needs</h2><p>The current daily record uses PSA's USD estimate alongside PSA 10 population, and also supports raw price, 7-day and 30-day sales, lowest listing, and listing count when those inputs become available. The PSA estimate is not presented as a completed-sale transaction price. Derived fields include 30-day population growth, raw-to-PSA spread, APS, and ASI.</p><p>Market estimates, stale listings, sparse sales, grading backlogs, currency changes, promotions, and source errors can all distort short-term readings. Review the component breakdown and confidence level before using the headline score.</p><div class="callout"><strong>Important</strong>ASI is an experimental monitoring model for research and does not constitute financial or investment advice.</div></section>
<footer>Methodology generated {html.escape(generated)} · <a href="dashboard.html">Return to dashboard</a></footer>
</main></body></html>'''


def dashboard(days: int) -> int:
    migrate_population_history(quiet=True)
    cards = load_cards()
    history = load_history()
    market_history = load_market_history()
    sections = []
    comparison_items = []
    for card_id, card in cards.items():
        rows = card_rows(history, card_id)
        stats = change_stats(rows, days)
        price_stats = estimate_stats(rows, days)
        population_period = f"{days}d" if stats["window_complete"] else "Since baseline"
        estimate_period = f"{days}D" if price_stats["window_complete"] else "SINCE BASELINE"
        values = [int(row["psa10_population"]) for row in rows]
        current = f"{stats['current']:,}" if stats["current"] is not None else "n/a"
        current_price = f"${price_stats['current']:,.0f}" if price_stats["current"] is not None else "n/a"
        price_change = (
            f"{price_stats['delta']:+,.0f} ({price_stats['pct']:+.2f}%)"
            if price_stats["delta"] is not None and price_stats["pct"] is not None
            else "n/a"
        )
        image_relative = Path("images") / f"{card_id}.jpg"
        image_html = ""
        image_path = ROOT / image_relative
        if image_path.exists():
            image_version = image_path.stat().st_mtime_ns
            image_html = (
                f'<a class="card-image-link" href="{html.escape(card["psa_url"])}" '
                f'aria-label="Open PSA record for {html.escape(card["name"])}">'
                f'<img class="card-image" src="{image_relative.as_posix()}?v={image_version}" '
                f'alt="PSA slab photo of {html.escape(card["name"])}" loading="lazy" decoding="async"></a>'
            )
        if stats["current"] is not None:
            comparison_items.append((card["name"], int(stats["current"])))
        asi_panel = card_asi_panel(card_id, card["name"], market_history)
        market_rows = [row for row in market_history if row["card_id"] == card_id]
        market_chart = (
            f'''<button class="history-preview" type="button" aria-haspopup="dialog" aria-controls="history-{html.escape(card_id)}" aria-label="Open price, population and ASI history for {html.escape(card["name"])}">{historical_market_preview_svg(market_rows, card["name"], card_id)}</button>
<dialog class="history-modal" id="history-{html.escape(card_id)}">
<div class="modal-head"><div><div class="eyebrow">PRICE, POPULATION &amp; ASI HISTORY</div><h2>{html.escape(card["name"])}</h2></div><button class="history-close" type="button" aria-label="Close price and population history">×</button></div>
{historical_market_svg(market_rows, card["name"], card_id)}
<div class="legend"><span class="price-key">PSA estimate (USD)</span><span class="pop-key">PSA10 population</span><span>ASI regime markers: green ≥65, yellow 45–64, orange 30–44, red &lt;30</span></div>
</dialog>'''
            if market_rows
            else '<div class="history-preview-empty">No market history yet</div>'
        )
        sections.append(
            f'''<section class="card">
<div class="card-head"><div>
<div class="eyebrow">PSA 10 POPULATION</div>
<h2>{html.escape(card["name"])}</h2>
<div class="number">{current}</div>
</div>{image_html}</div>
<div class="price-row"><div><span>PSA ESTIMATE (USD)</span><strong>{current_price}</strong></div><div><span>{estimate_period} ESTIMATE CHANGE</span><b>{price_change}</b></div></div>
<div class="metrics"><span>{population_period} change <b>{fmt(stats['delta'])}</b></span><span>Growth <b>{fmt(stats['pct'], '%')}</b></span><span>Daily <b>{fmt(stats['daily'])}</b></span></div>
{asi_panel}
{market_chart}
</section>'''
        )
    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect x='12' y='3' width='40' height='58' rx='5' fill='%23dbeafe' stroke='%238cc8ff' stroke-width='3'/%3E%3Crect x='17' y='9' width='30' height='10' rx='2' fill='%23dc2626'/%3E%3Crect x='19' y='24' width='26' height='30' rx='2' fill='%23101827' stroke='%238cc8ff'/%3E%3Ccircle cx='32' cy='39' r='8' fill='%23ffd166'/%3E%3C/svg%3E">
<title>PSA Card Population Monitor</title><style>
:root{{--bg:#0c111b;--panel:#151d2b;--text:#f4f7fb;--muted:#9ba9bd;--accent:#7ee787;--grid:#33415a;--price:#ffd166;--pop:#66b3ff}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at top,#17233a,var(--bg) 50%);color:var(--text);font:16px/1.5 system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:48px 24px}}h1{{font-size:clamp(2rem,5vw,4rem);margin:0 0 8px}}.sub,.eyebrow,.empty,.coverage{{color:var(--muted)}}.asi-feature,.comparison{{margin-top:38px;background:rgba(21,29,43,.92);border:1px solid #2b3950;border-radius:20px;padding:26px;box-shadow:0 18px 45px #0005}}.asi-feature.buy,.asi-feature.accumulate{{border-color:#318a52}}.asi-feature.wait{{border-color:#a9852b}}.asi-feature.avoid{{border-color:#b66a2b}}.asi-feature.floor{{border-color:#a3444d}}.asi-heading{{display:flex;justify-content:space-between;gap:28px;align-items:flex-start}}.asi-heading h2{{font-size:2rem;margin:4px 0}}.asi-heading h2 span{{font-size:4rem;letter-spacing:-.06em}}.asi-heading h2 small{{color:var(--muted);font-size:1.1rem}}.phase{{margin:0;color:var(--muted)}}.asi-kpis{{display:grid;grid-template-columns:repeat(3,minmax(95px,1fr));gap:10px}}.asi-kpis div{{background:#101827;border:1px solid #2b3950;border-radius:14px;padding:12px}}.asi-kpis span,.asi-kpis small{{display:block;color:var(--muted);font-size:.72rem}}.asi-kpis strong{{display:block;font-size:1.25rem}}.accumulation-alert,.alert-pending{{display:flex;gap:14px;align-items:center;margin:22px 0;padding:14px 16px;border-radius:12px}}.accumulation-alert{{background:#123322;border:1px solid #318a52;color:#c6f6d5}}.alert-pending{{background:#101827;border:1px solid #2b3950;color:var(--muted)}}.accumulation-alert span{{font-size:.9rem}}.asi-layout{{display:grid;grid-template-columns:minmax(0,2fr) minmax(260px,1fr);gap:26px}}.asi-layout h3{{margin:8px 0 12px}}.market-chart{{display:block;width:100%;height:auto;background:#101827;border-radius:14px}}.market-grid{{stroke:#2b3950;stroke-width:1}}.axis{{fill:var(--muted);font-size:12px}}.price-line,.pop-line{{fill:none;stroke-width:4;stroke-linecap:round;stroke-linejoin:round}}.price-line{{stroke:var(--price)}}.pop-line{{stroke:var(--pop)}}.asi-marker{{stroke:#fff;stroke-width:2}}.asi-marker.buy,.asi-marker.accumulate{{fill:#48bb78}}.asi-marker.wait{{fill:#ecc94b}}.asi-marker.avoid{{fill:#ed8936}}.asi-marker.floor{{fill:#f56565}}.legend{{display:flex;flex-wrap:wrap;gap:14px;color:var(--muted);font-size:.78rem;margin-top:8px}}.price-key:before,.pop-key:before{{content:"";display:inline-block;width:16px;height:3px;margin:0 6px 3px 0}}.price-key:before{{background:var(--price)}}.pop-key:before{{background:var(--pop)}}.components{{width:100%;border-collapse:collapse;font-size:.86rem}}.components th,.components td{{padding:9px 7px;border-bottom:1px solid #2b3950;text-align:left}}.components th{{color:var(--muted)}}.coverage{{font-size:.78rem}}.comparison h2{{margin-top:0}}.comparison-chart{{display:block;width:100%;height:auto;overflow:visible}}.comparison-chart .gridline{{stroke:var(--grid);stroke-width:1}}.comparison-chart .axis-label,.comparison-chart .bar-label{{fill:var(--muted);font-size:13px}}.comparison-chart .bar-value{{fill:var(--text);font-size:13px;font-weight:700}}.comparison-chart .bar{{fill:var(--accent)}}.comparison-mobile{{display:none}}.mobile-bar-row{{margin:18px 0}}.mobile-bar-heading{{display:flex;justify-content:space-between;gap:12px;font-size:13px}}.mobile-bar-heading span{{color:var(--muted)}}.mobile-bar-track{{height:18px;background:var(--grid);border-radius:4px;overflow:hidden;margin-top:6px}}.mobile-bar-track span{{display:block;height:100%;background:var(--accent)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:22px;margin-top:36px}}.card{{background:rgba(21,29,43,.92);border:1px solid #2b3950;border-radius:20px;padding:26px;box-shadow:0 18px 45px #0005}}.card-head{{display:grid;grid-template-columns:minmax(0,1fr) minmax(88px,120px);gap:18px;align-items:start}}.card-head>div{{min-width:0}}.card-image-link{{display:block;border:1px solid #3b4b65;border-radius:12px;overflow:hidden;background:#080d15;box-shadow:0 10px 24px #0007;transition:transform .18s ease,border-color .18s ease}}.card-image-link:hover{{transform:translateY(-2px);border-color:#8cc8ff}}.card-image{{display:block;width:100%;height:auto}}h2{{margin:5px 0 18px}}.number{{font-size:clamp(2.25rem,5vw,3.8rem);font-weight:800;line-height:1;letter-spacing:-.06em;white-space:nowrap}}.price-row{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:20px 0 4px;padding:13px 14px;background:#101827;border:1px solid #2b3950;border-radius:14px}}.price-row div{{min-width:0}}.price-row span{{display:block;color:var(--muted);font-size:.7rem;letter-spacing:.07em}}.price-row strong{{display:block;color:#ffd166;font-size:1.55rem;line-height:1.2;margin-top:3px}}.price-row b{{display:block;font-size:.9rem;margin-top:7px;white-space:nowrap}}.metrics{{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}}.metrics span{{background:#202b3d;padding:7px 10px;border-radius:999px}}.chart{{color:var(--accent);border-bottom:1px solid var(--grid);margin:12px 0 20px}}a{{color:#8cc8ff}}footer{{color:var(--muted);margin-top:28px;font-size:.9rem}}@media(max-width:850px){{.asi-heading{{display:block}}.asi-kpis{{margin-top:18px}}.asi-layout{{grid-template-columns:1fr}}}}@media(max-width:650px){{main{{padding:30px 14px}}.asi-feature,.comparison{{padding:18px}}.asi-kpis{{grid-template-columns:1fr}}.accumulation-alert{{display:block}}.comparison-chart{{display:none}}.comparison-mobile{{display:block}}.grid{{grid-template-columns:1fr}}.card{{padding:20px}}.card-head{{grid-template-columns:minmax(0,1fr) 96px;gap:12px}}.number{{font-size:clamp(1.9rem,9vw,2.8rem)}}.price-row{{grid-template-columns:1fr}}.price-row b{{white-space:normal}}}}
.history-preview{{display:block;width:100%;margin:14px 0 4px;padding:0;border:1px solid #2f405a;border-radius:12px;background:#101827;color:inherit;overflow:hidden;cursor:pointer}}.history-preview:hover{{border-color:#8cc8ff;background:#131e30}}.history-preview:focus-visible,.history-close:focus-visible{{outline:2px solid #8cc8ff;outline-offset:3px}}.mini-market-chart{{display:block;width:100%;height:auto}}.mini-grid{{stroke:#26344a;stroke-width:1}}.history-preview-empty{{margin:14px 0 4px;padding:18px;border:1px solid #2f405a;border-radius:12px;background:#101827;color:var(--muted);font-size:.75rem;text-align:center}}.history-modal{{width:min(1040px,calc(100% - 28px));max-height:min(88vh,850px);padding:24px;border:1px solid #455875;border-radius:18px;background:#151d2b;color:var(--text);box-shadow:0 28px 90px #000c}}.history-modal::backdrop{{background:#050910c7;backdrop-filter:blur(4px)}}.history-close{{flex:0 0 auto;width:38px;height:38px;border:1px solid #455875;border-radius:999px;background:#202b3d;color:var(--text);font-size:1.5rem;line-height:1;cursor:pointer}}.history-modal .market-chart{{border:1px solid #26344a}}body:has(.history-modal[open]){{overflow:hidden}}@media(max-width:650px){{.history-modal{{padding:16px}}.history-modal .axis{{font-size:15px}}}}
.comparison-content:not([data-ready="true"]){{display:none}}
.card-image{{aspect-ratio:380/628;height:auto;object-fit:cover}}.component-kpis{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:0 0 18px}}.component-kpis span{{min-width:0;padding:10px;background:#1c2636;border-radius:9px;color:var(--muted);font-size:.72rem}}.component-kpis b,.component-kpis small{{display:block;color:var(--text);font-size:.86rem}}.component-kpis small{{color:var(--muted);font-size:.68rem}}.provisional-note{{margin:12px 0;padding:10px 12px;border:1px solid #a9852b;border-radius:10px;background:#2b2412;color:#f6df9b;font-size:.75rem}}.provisional-note strong{{margin-right:5px}}@media(max-width:650px){{.component-kpis{{grid-template-columns:1fr}}}}
</style><style>
.asi-history{{margin-top:38px;background:rgba(21,29,43,.92);border:1px solid #2b3950;border-radius:20px;padding:26px;box-shadow:0 18px 45px #0005}}.asi-history h2{{margin:4px 0 18px}}.card-asi{{margin:18px 0;padding:16px;background:#101827;border:1px solid #2b3950;border-left:4px solid #59677c;border-radius:14px}}.card-asi.buy,.card-asi.accumulate{{border-left-color:#48bb78}}.card-asi.wait{{border-left-color:#ecc94b}}.card-asi.avoid{{border-left-color:#ed8936}}.card-asi.floor{{border-left-color:#f56565}}.card-asi-heading{{display:flex;justify-content:space-between;gap:12px;align-items:baseline;margin-top:3px}}.card-asi-score{{font-size:1.55rem;font-weight:800;white-space:nowrap}}.card-asi-score small{{color:var(--muted);font-size:.72rem}}.card-asi-phase{{color:var(--muted);font-size:.78rem;text-align:right}}.card-asi-phase strong{{color:var(--text)}}.card-asi-metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:12px}}.card-asi-metrics span{{min-width:0;padding:7px;background:#1c2636;border-radius:8px;color:var(--muted);font-size:.68rem}}.card-asi-metrics b,.card-asi-metrics small{{display:block;color:var(--text);font-size:.78rem}}.card-asi-metrics small{{color:var(--muted);font-size:.65rem}}.card-alert{{margin-top:10px;padding:8px;border-radius:8px;background:#123322;color:#c6f6d5;font-size:.72rem;font-weight:800}}.card-asi details{{margin-top:12px}}.card-asi summary{{cursor:pointer;color:#8cc8ff;font-size:.78rem}}.card-asi .components{{margin-top:8px;font-size:.72rem}}.card-asi .components th,.card-asi .components td{{padding:6px 3px}}.card-asi .coverage{{margin-bottom:0}}@media(max-width:650px){{.asi-history{{padding:18px}}.card-asi-heading{{display:block}}.card-asi-phase{{text-align:left}}}}
</style><style>
.card-market-history{{margin:18px 0 20px}}.card-market-history>.eyebrow{{margin-bottom:8px;font-size:.72rem}}.card-market-history .market-chart{{border:1px solid #26344a}}.price-point{{fill:var(--price)}}.pop-point{{fill:var(--pop)}}.breakdown-button{{margin-top:12px;padding:0;border:0;background:none;color:#8cc8ff;font:inherit;font-size:.78rem;cursor:pointer;text-decoration:underline;text-underline-offset:3px}}.breakdown-button:before{{content:"▸ ";text-decoration:none}}.breakdown-button:hover{{color:#b8ddff}}.breakdown-button:focus-visible,.modal-close:focus-visible{{outline:2px solid #8cc8ff;outline-offset:3px}}.component-modal{{width:min(580px,calc(100% - 28px));max-height:min(82vh,760px);padding:24px;border:1px solid #455875;border-radius:18px;background:#151d2b;color:var(--text);box-shadow:0 28px 90px #000c}}.component-modal::backdrop{{background:#050910c7;backdrop-filter:blur(4px)}}.modal-head{{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:18px}}.modal-head h2{{margin:4px 0 0}}.modal-head p{{margin:4px 0;color:var(--muted)}}.modal-close{{flex:0 0 auto;width:38px;height:38px;border:1px solid #455875;border-radius:999px;background:#202b3d;color:var(--text);font-size:1.5rem;line-height:1;cursor:pointer}}.component-modal .components{{font-size:.9rem}}.component-modal .coverage{{margin-bottom:0}}body:has(.component-modal[open]){{overflow:hidden}}@media(max-width:650px){{.component-modal{{padding:18px}}.card-market-history .axis{{font-size:15px}}}}
</style><style>.metrics span{{font-size:.78rem;padding:6px 9px}}.metrics b{{font-size:.78rem}}.comparison-head{{display:flex;align-items:center;justify-content:space-between;gap:16px}}.comparison-head h2{{margin:0}}.comparison-toggle{{flex:0 0 auto;padding:7px 12px;border:1px solid #455875;border-radius:999px;background:#202b3d;color:#8cc8ff;font:inherit;font-size:.78rem;cursor:pointer}}.comparison-toggle:hover{{border-color:#8cc8ff;background:#26354a}}.comparison-toggle:focus-visible{{outline:2px solid #8cc8ff;outline-offset:3px}}.comparison-content{{margin-top:18px}}@media(max-width:650px){{.comparison-head{{align-items:flex-start}}}}</style></head><body><main><div class="eyebrow">SUPPLY &amp; DEMAND TRACKER</div><h1>PSA card market monitor</h1><p class="sub">Verified population snapshots with a daily accumulation-strength model.</p><section class="comparison"><div class="comparison-head"><h2>Current PSA 10 populations</h2><button class="comparison-toggle" type="button" aria-expanded="true" aria-controls="population-comparison-content">Hide chart</button></div><div class="comparison-content" id="population-comparison-content">{comparison_svg(comparison_items)}{comparison_mobile_bars(comparison_items)}</div></section><div class="grid">{''.join(sections)}</div><footer>Generated {html.escape(generated)}. ASI is a monitoring model, not investment advice.</footer></main><script>
const comparisonToggle=document.querySelector(".comparison-toggle"),comparisonContent=document.querySelector(".comparison-content"),comparisonStorageKey="psa-population-comparison-hidden-v2";
function setComparisonCollapsed(collapsed){{comparisonContent.hidden=collapsed;comparisonContent.dataset.ready="true";comparisonToggle.setAttribute("aria-expanded",String(!collapsed));comparisonToggle.textContent=collapsed?"Show chart":"Hide chart"}}
try{{const savedComparisonState=localStorage.getItem(comparisonStorageKey);setComparisonCollapsed(savedComparisonState===null?true:savedComparisonState==="true")}}catch(error){{setComparisonCollapsed(true)}}
comparisonToggle.addEventListener("click",()=>{{const collapsed=!comparisonContent.hidden;setComparisonCollapsed(collapsed);try{{localStorage.setItem(comparisonStorageKey,String(collapsed))}}catch(error){{}}}});
document.addEventListener("click",event=>{{const historyOpen=event.target.closest?.(".history-preview");if(historyOpen){{historyOpen.closest(".card").querySelector(".history-modal").showModal();return}}const historyClose=event.target.closest?.(".history-close");if(historyClose){{historyClose.closest("dialog").close();return}}const open=event.target.closest?.(".breakdown-button");if(open){{open.closest(".card").querySelector(".component-modal").showModal();return}}const close=event.target.closest?.(".modal-close");if(close){{close.closest("dialog").close();return}}if(event.target.matches?.("dialog.component-modal,dialog.history-modal"))event.target.close()}});
</script></body></html>'''
    dashboard_nav = (
        '<nav aria-label="Primary" style="display:flex;align-items:center;justify-content:space-between;'
        'gap:16px;margin-bottom:26px"><span class="eyebrow">SUPPLY &amp; DEMAND TRACKER</span>'
        '<a href="about-asi.html" style="text-decoration:none;padding:7px 12px;border:1px solid #455875;'
        'border-radius:999px">About ASI</a></nav>'
    )
    page = page.replace(
        '<body><main><div class="eyebrow">SUPPLY &amp; DEMAND TRACKER</div>',
        f'<body><main>{dashboard_nav}',
        1,
    )
    DASHBOARD_PATH.write_text(page, encoding="utf-8")
    ABOUT_ASI_PATH.write_text(about_asi_html(generated), encoding="utf-8")
    print(f"Wrote {DASHBOARD_PATH}")
    print(f"Wrote {ABOUT_ASI_PATH}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record_parser = sub.add_parser("record", help="append a verified population snapshot")
    record_parser.add_argument("--card", required=True)
    record_parser.add_argument("--psa10-pop", type=int, required=True)
    record_parser.add_argument("--psa9-pop", type=int)
    record_parser.add_argument("--total-pop", type=int)
    record_parser.add_argument("--estimate-usd", type=float)
    record_parser.add_argument("--observed-at", required=True, help="ISO 8601 timestamp with timezone")
    record_parser.add_argument("--verified-identity", action=argparse.BooleanOptionalAction, default=True)
    record_parser.add_argument("--notes", default="")
    record_parser.set_defaults(func=record)
    market_parser = sub.add_parser(
        "record-market", help="upsert daily prices, sales, listings, and population"
    )
    market_parser.add_argument("--card", required=True)
    market_parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    market_parser.add_argument("--psa10-price", type=float)
    market_parser.add_argument("--raw-price", type=float)
    market_parser.add_argument("--psa10-pop", type=int)
    market_parser.add_argument("--sales-7d", type=int)
    market_parser.add_argument("--sales-30d", type=int)
    market_parser.add_argument("--lowest-listing", type=float)
    market_parser.add_argument("--listing-count", type=int)
    market_parser.set_defaults(func=record_market)
    migrate_parser = sub.add_parser(
        "migrate-market-history",
        help="idempotently seed daily market history from population observations",
    )
    migrate_parser.set_defaults(func=lambda args: migrate_population_history())
    report_parser = sub.add_parser("report", help="print population changes")
    report_parser.add_argument("--days", type=int, default=7)
    report_parser.set_defaults(func=lambda args: print(report_text(args.days)) or 0)
    dashboard_parser = sub.add_parser("dashboard", help="generate dashboard.html")
    dashboard_parser.add_argument("--days", type=int, default=30)
    dashboard_parser.set_defaults(func=lambda args: dashboard(args.days))
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
