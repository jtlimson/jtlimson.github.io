#!/usr/bin/env python3
"""Sync generated monitor assets to the GitHub Pages checkout and publish them."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = ROOT / "publish" / "jtlimson.github.io"
JST = ZoneInfo("Asia/Tokyo")
PUBLIC_FILES = (
    "dashboard.html",
    "about-asi.html",
    "cards.json",
    "README.md",
    "data/population_history.csv",
    "data/market_history.csv",
    "tracker.py",
    "test_tracker.py",
    "pi_collector.py",
    "publish_site.py",
    "issue_agent.py",
)


def run_git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def sync_assets(repo: Path) -> list[str]:
    cards = json.loads((ROOT / "cards.json").read_text(encoding="utf-8"))
    relative_paths = list(PUBLIC_FILES)
    relative_paths.extend(
        f"images/{card_id}.jpg"
        for card_id in cards
        if (ROOT / "images" / f"{card_id}.jpg").exists()
    )
    for relative in relative_paths:
        source = ROOT / relative
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return relative_paths


def publish(repo: Path, push: bool = True) -> bool:
    if not (repo / ".git").exists():
        raise RuntimeError(f"GitHub Pages checkout not found: {repo}")
    if push:
        run_git(repo, "pull", "--ff-only")
    paths = sync_assets(repo)
    run_git(repo, "add", "--", *paths)
    if not run_git(repo, "diff", "--cached", "--quiet", check=False).returncode:
        print("GitHub Pages checkout already matches the rendered dashboard.")
        return False
    timestamp = datetime.now(JST).isoformat(timespec="seconds")
    run_git(
        repo, "-c", "user.name=card-pop-monitor",
        "-c", "user.email=card-pop-monitor@users.noreply.github.com",
        "commit", "-m", f"Update card monitor {timestamp}",
    )
    if push:
        run_git(repo, "push", "origin", "HEAD")
        print(f"Published dashboard update to {repo}.")
    else:
        print(f"Committed dashboard update in {repo}; push skipped.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path,
        default=Path(os.environ.get("CARD_MONITOR_PUBLISH_REPO", DEFAULT_REPO)),
        help="local checkout of jtlimson.github.io",
    )
    parser.add_argument("--no-push", action="store_true", help="commit without pushing")
    args = parser.parse_args()
    publish(args.repo.resolve(), push=not args.no_push)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
