#!/usr/bin/env python3
"""Collect validated PSA populations through a text-rendered public cert record."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CARDS_PATH = ROOT / "cards.json"
TRACKER_PATH = ROOT / "tracker.py"
PUBLISH_PATH = ROOT / "publish_site.py"
LOG_PATH = ROOT / "data" / "collector.log"
JST = ZoneInfo("Asia/Tokyo")


class Tee:
    """Write collector output to both the terminal and the persistent log."""

    def __init__(self, *streams: object) -> None:
        self.streams = streams

    def write(self, value: str) -> int:
        for stream in self.streams:
            stream.write(value)
        return len(value)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def extract(body: str, label: str) -> str | None:
    match = re.search(rf"(?im)^{re.escape(label)}\s*$\s*^([^\r\n]+)$", body)
    return match.group(1).strip() if match else None


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().upper()


def numeric(value: str | None) -> int:
    if not value:
        raise ValueError("missing numeric value")
    cleaned = re.sub(r"[^0-9]", "", value)
    if not cleaned:
        raise ValueError(f"invalid numeric value: {value!r}")
    return int(cleaned)


def mirror_url(card: dict[str, str]) -> str:
    return f"https://r.jina.ai/http://www.psacard.com/cert/{card['psa_cert']}/psa"


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "card-pop-monitor/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def mirror_field(body: str, label: str) -> str | None:
    match = re.search(rf"(?:^|\n){re.escape(label)}\s+([^\n]+)", body)
    return match.group(1).strip() if match else None


def parse_psa(body: str, card: dict[str, str]) -> tuple[int, float | None, str | None]:
    actual = {
        "expected_year": mirror_field(body, "Year"),
        "expected_brand": mirror_field(body, "Brand/Title"),
        "expected_subject": mirror_field(body, "Subject"),
        "expected_card_number": mirror_field(body, "Card Number"),
        "expected_variety": mirror_field(body, "Variety/Pedigree"),
    }
    mismatches = [
        key for key, value in actual.items() if normalized(value) != normalized(card[key])
    ]
    if mismatches:
        details = ", ".join(f"{key}={actual[key]!r}" for key in mismatches)
        raise RuntimeError(f"card identity mismatch: {details}")
    grade = mirror_field(body, "Item Grade")
    if normalized(grade) != "GEM MT 10":
        raise RuntimeError(f"unexpected grade: {grade!r}")
    population_match = re.search(r"PSA Population\s*\n\[([0-9,]+)\]", body)
    population = numeric(population_match.group(1) if population_match else None)
    estimate_match = re.search(r"PSA Estimate\s*\n\$([0-9,]+(?:\.[0-9]+)?)", body)
    estimate = None
    if estimate_match:
        estimate = float(estimate_match.group(1).replace(",", ""))
    image_match = re.search(
        r"!\[Image \d+: Cert image 1\]\((https://[^)]+)\)", body
    )
    return population, estimate, image_match.group(1) if image_match else None


def read_psa(card: dict[str, str]) -> tuple[int, float | None, str | None]:
    body = fetch(mirror_url(card)).decode("utf-8")
    return parse_psa(body, card)


def update_image(card_id: str, image_url: str | None) -> None:
    if not image_url:
        return
    target = ROOT / "images" / f"{card_id}.jpg"
    temporary = target.with_suffix(".jpg.tmp")
    temporary.write_bytes(fetch(image_url))
    temporary.replace(target)


def record(card_id: str, population: int, estimate: float | None, observed_at: str) -> None:
    command = [
        sys.executable,
        str(TRACKER_PATH),
        "record",
        "--card",
        card_id,
        "--psa10-pop",
        str(population),
        "--observed-at",
        observed_at,
        "--notes",
        "Automated snapshot validated from PSA cert record via text mirror",
    ]
    if estimate is not None:
        command.extend(["--estimate-usd", f"{estimate:.2f}"])
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = LOG_PATH.open("a", encoding="utf-8", buffering=1)
    sys.stdout = Tee(sys.stdout, log_handle)
    sys.stderr = Tee(sys.stderr, log_handle)
    print(f"\nCollector started {datetime.now(JST).isoformat(timespec='seconds')}")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card", help="collect only one configured card id")
    parser.add_argument("--delay", type=int, default=45, help="seconds between PSA requests")
    args = parser.parse_args()
    cards: dict[str, dict[str, str]] = json.loads(CARDS_PATH.read_text(encoding="utf-8"))
    if args.card:
        if args.card not in cards:
            parser.error(f"unknown card id: {args.card}")
        cards = {args.card: cards[args.card]}
    observed_at = datetime.now(JST).isoformat(timespec="seconds")
    failures: list[str] = []
    for index, (card_id, card) in enumerate(cards.items()):
        if index:
            time.sleep(max(args.delay, 0))
        try:
            population, estimate, image_url = read_psa(card)
            update_image(card_id, image_url)
            record(card_id, population, estimate, observed_at)
        except (RuntimeError, ValueError, urllib.error.URLError, subprocess.CalledProcessError) as exc:
            failures.append(f"{card_id}: {exc}")
            print(f"FAILED {card_id}: {exc}", file=sys.stderr)

    subprocess.run(
        [sys.executable, str(TRACKER_PATH), "dashboard", "--days", "30"],
        cwd=ROOT,
        check=True,
    )
    try:
        subprocess.run([sys.executable, str(PUBLISH_PATH)], cwd=ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        failures.append(f"publish: {exc}")
        print(f"FAILED publish: {exc}", file=sys.stderr)
    if failures:
        print(f"Completed with {len(failures)} failure(s).", file=sys.stderr)
        return 1
    print(f"Recorded {len(cards)} verified card populations at {observed_at}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
