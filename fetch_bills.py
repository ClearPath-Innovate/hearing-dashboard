#!/usr/bin/env python3
"""
fetch_bills.py — ClearPath Legislative Bill Tracker
Searches Congress.gov for ClearPath priority bills and updates their status.

Uses tracked_bills.json as the curated source of truth (tied to 119th Congress KPIs).
Writes enriched bill data to data/bills_status.json.

Usage:
    python3 fetch_bills.py              # fetch status for all tracked bills
    python3 fetch_bills.py --list       # show current tracked bills and status
    python3 fetch_bills.py --bill arc-act  # update a specific bill by ID
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_DIR        = Path("data")
CONFIG_PATH     = DATA_DIR / "config.json"
TRACKED_PATH    = DATA_DIR / "tracked_bills.json"
BILLS_PATH      = DATA_DIR / "bills_status.json"

DATA_DIR.mkdir(exist_ok=True)

STATUS_DISPLAY = {
    "introduced":     ("Introduced",      "#6B7280"),
    "in_committee":   ("In Committee",    "#2563EB"),
    "markup":         ("Markup",          "#D97706"),
    "passed_chamber": ("Passed Chamber",  "#7C3AED"),
    "enacted":        ("Enacted ✓",       "#16A34A"),
    "stalled":        ("Stalled",         "#9CA3AF"),
    "watching":       ("Watching",        "#193D69"),
}

PRIORITY_COLORS = {
    "High":   ("#9D1C20", "#FDECEA"),
    "Medium": ("#193D69", "#EBF2FA"),
    "Low":    ("#6B7280", "#F3F4F6"),
}


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2))


def load_config():
    return load_json(CONFIG_PATH, {})


def congress_api_get(endpoint: str, params: dict, api_key: str) -> dict:
    """Make a Congress.gov API request. Returns parsed JSON or empty dict."""
    base = "https://api.congress.gov/v3"
    params["api_key"] = api_key
    params["format"]  = "json"
    try:
        r = requests.get(f"{base}{endpoint}", params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"  ⚠️  API {r.status_code} for {endpoint}")
            return {}
    except Exception as e:
        print(f"  ⚠️  Request failed: {e}")
        return {}


def search_bills_by_keyword(keyword: str, api_key: str, congress: int = 119) -> list:
    """Search Congress.gov for bills matching a keyword in the 119th Congress."""
    data = congress_api_get(
        "/bill",
        {"query": keyword, "congress": congress, "limit": 5, "sort": "updateDate+desc"},
        api_key
    )
    return data.get("bills", [])


def get_bill_detail(bill_type: str, bill_number: int, congress: int, api_key: str) -> dict:
    """Fetch full detail for a specific bill."""
    data = congress_api_get(
        f"/bill/{congress}/{bill_type.lower()}/{bill_number}",
        {},
        api_key
    )
    return data.get("bill", {})


def infer_status(bill_detail: dict) -> str:
    """Map Congress.gov bill data to our status vocabulary."""
    actions = bill_detail.get("latestAction", {})
    action_text = (actions.get("text") or "").lower()

    if "became public law" in action_text or "signed by president" in action_text:
        return "enacted"
    if "passed senate" in action_text or "passed house" in action_text:
        return "passed_chamber"
    if "ordered to be reported" in action_text or "markup" in action_text:
        return "markup"
    if "referred to" in action_text or "committee" in action_text:
        return "in_committee"
    if "introduced" in action_text:
        return "introduced"
    return "watching"


def fetch_bill_status(tracked_bill: dict, api_key: str) -> dict:
    """Search for a tracked bill and return enriched status data."""
    bill_id    = tracked_bill["id"]
    search_terms = tracked_bill.get("search_terms", [tracked_bill["name"]])

    print(f"  🔍 Searching: {tracked_bill['name']}")

    best_match = None
    for term in search_terms[:2]:  # try first 2 search terms max
        results = search_bills_by_keyword(term, api_key)
        if results:
            best_match = results[0]
            break
        time.sleep(0.3)  # be polite to the API

    if not best_match:
        print(f"     No match found — keeping as Watching")
        return {
            "id": bill_id,
            "api_found": False,
            "status": tracked_bill.get("status", "watching").lower(),
            "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        }

    # Optionally fetch full detail
    bill_type   = best_match.get("type", "")
    bill_num    = best_match.get("number")
    congress    = best_match.get("congress", 119)
    latest_action = best_match.get("latestAction", {})
    action_text   = latest_action.get("text", "")
    action_date   = latest_action.get("actionDate", "")
    bill_url      = best_match.get("url", "")
    title         = best_match.get("title", tracked_bill["full_name"])

    inferred_status = infer_status({"latestAction": latest_action})

    print(f"     ✓ Found: {bill_type} {bill_num} — {inferred_status.upper()}")
    print(f"       Latest: {action_text[:80]}")

    return {
        "id":              bill_id,
        "api_found":       True,
        "bill_type":       bill_type,
        "bill_number":     bill_num,
        "congress":        congress,
        "api_title":       title,
        "status":          inferred_status,
        "latest_action":   action_text,
        "action_date":     action_date,
        "congress_url":    bill_url,
        "last_updated_utc": datetime.now(timezone.utc).isoformat(),
    }


def run(target_id: str = None):
    cfg     = load_config()
    api_key = cfg.get("congress_api_key", "").strip()

    tracked = load_json(TRACKED_PATH, [])
    if not tracked:
        print("❌ No tracked bills found in data/tracked_bills.json")
        return

    existing = {b["id"]: b for b in load_json(BILLS_PATH, [])}

    if target_id:
        tracked = [b for b in tracked if b["id"] == target_id]
        if not tracked:
            print(f"❌ Bill ID '{target_id}' not found in tracked_bills.json")
            return

    if not api_key:
        print("⚠️  No congress_api_key in data/config.json — skipping API fetch.")
        print("   Add your Congress.gov API key to data/config.json to enable live status.")
        print("   Bills will display from tracked_bills.json with 'Watching' status.")
        return

    print(f"\n🏛️  Fetching status for {len(tracked)} bill(s) from Congress.gov...\n")
    updated = []
    for bill in tracked:
        result = fetch_bill_status(bill, api_key)
        # Merge: API result wins for status fields, but preserve manual notes
        merged = {**existing.get(bill["id"], {}), **result}
        updated.append(merged)
        time.sleep(0.5)  # rate limiting

    # Merge with any existing entries not in this run
    all_ids = {b["id"] for b in updated}
    for bid, bdata in existing.items():
        if bid not in all_ids:
            updated.append(bdata)

    save_json(BILLS_PATH, updated)
    print(f"\n✅ Updated {len(updated)} bills → data/bills_status.json")


def list_bills():
    tracked  = load_json(TRACKED_PATH, [])
    statuses = {b["id"]: b for b in load_json(BILLS_PATH, [])}

    print(f"\n{'ID':<25} {'NAME':<30} {'PRIORITY':<8} {'STATUS'}")
    print("-" * 80)
    for b in tracked:
        sid     = b["id"]
        status  = statuses.get(sid, {}).get("status", b.get("status", "watching"))
        label   = STATUS_DISPLAY.get(status.lower(), (status.title(), ""))[0]
        pri     = b.get("clearpath_priority", "")
        print(f"{sid:<25} {b['name']:<30} {pri:<8} {label}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch ClearPath bill status from Congress.gov")
    parser.add_argument("--list",  action="store_true", help="List all tracked bills and current status")
    parser.add_argument("--bill",  type=str, help="Update a specific bill by ID (e.g. arc-act)")
    args = parser.parse_args()

    if args.list:
        list_bills()
    else:
        run(target_id=args.bill)
