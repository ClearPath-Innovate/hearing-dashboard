#!/usr/bin/env python3
"""
fetch_all_hearings.py — Fetches committee hearings from Congress.gov API
and merges them into hearings_seed.json.

The Congress.gov list endpoint only returns eventId + url per meeting.
This script fetches the individual detail URL for each meeting to get
the full data (date, committee, topic, location, etc.)

Usage:
    python3 fetch_all_hearings.py           # fetch current + next week
    python3 fetch_all_hearings.py --weeks 3 # fetch more weeks ahead
    python3 fetch_all_hearings.py --test    # print one detail response without saving
"""

import json
import hashlib
import re
import argparse
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

DATA_DIR      = Path("data")
HEARINGS_PATH = DATA_DIR / "hearings_seed.json"
CONFIG_PATH   = DATA_DIR / "config.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}

def load_hearings():
    if HEARINGS_PATH.exists():
        return json.loads(HEARINGS_PATH.read_text())
    return []

def save_hearings(hearings):
    HEARINGS_PATH.write_text(json.dumps(hearings, indent=2, default=str))

def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())

def compute_hearing_id(h):
    base = "|".join([
        norm(h.get("committee", "")),
        norm(h.get("subcommittee", "")),
        norm(h.get("date", "")),
        norm(h.get("time", "")),
        norm(h.get("topic", "")),
        norm(h.get("location", "")),
    ]).lower()
    return hashlib.sha1(base.encode()).hexdigest()[:12]

# ---------------------------------------------------------------------------
# Congress.gov API — list endpoint (returns eventId + url only)
# ---------------------------------------------------------------------------

def fetch_meeting_list(api_key, congress=119, chamber=None, offset=0,
                       from_dt=None, to_dt=None):
    """One page of the committee-meeting list. Returns minimal fields only."""
    params = {
        "format": "json",
        "limit":  250,
        "offset": offset,
        "api_key": api_key,
    }
    if chamber:
        params["chamber"] = chamber.lower()
    if from_dt:
        params["fromDateTime"] = from_dt  # e.g. "2026-03-23T00:00:00Z"
    if to_dt:
        params["toDateTime"] = to_dt

    url = f"https://api.congress.gov/v3/committee-meeting/{congress}"
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  List error ({chamber or 'all'}): {e}")
        return None


def fetch_meeting_detail(detail_url, api_key):
    """Fetch the full detail for a single meeting event."""
    try:
        r = requests.get(detail_url, params={"api_key": api_key}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Detail error for {detail_url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Parse a full detail response into our hearing dict
# ---------------------------------------------------------------------------

def parse_detail(detail_json, chamber_hint=""):
    """Convert a meeting detail API response to our hearing dict."""
    m = detail_json.get("committeeMeeting", detail_json)

    # Date
    raw_date = (m.get("meetingDate") or m.get("date") or
                m.get("startDate") or m.get("updateDate") or "")
    if not raw_date:
        return None
    try:
        meeting_date = datetime.fromisoformat(raw_date[:10]).date()
    except Exception:
        return None

    # Committee
    committees = m.get("committees") or []
    if committees:
        committee = norm(committees[0].get("name", ""))
        chamber_raw = (m.get("chamber") or chamber_hint or "").lower()
        if chamber_raw == "house" and not committee.lower().startswith("house"):
            committee = f"House {committee}"
        elif chamber_raw == "senate" and not committee.lower().startswith("senate"):
            committee = f"Senate {committee}"
    else:
        committee = norm(m.get("committeeName") or m.get("chamber") or chamber_hint or "Unknown")

    subcommittee = ""
    if len(committees) > 1:
        subcommittee = norm(committees[1].get("name", ""))

    # Topic
    topic = norm(m.get("title") or m.get("meetingTitle") or "")
    if not topic:
        return None

    # Location / time
    loc = m.get("location") or {}
    if isinstance(loc, dict):
        location = norm(f"{loc.get('building', '')} {loc.get('room', '')}").strip() or "TBD"
    else:
        location = norm(loc) or "TBD"
    raw_time = m.get("time") or m.get("startTime") or ""
    if not raw_time and raw_date and "T" in str(raw_date):
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            time_str = dt.strftime("%I:%M %p").lstrip("0")
        except Exception:
            time_str = "TBD"
    else:
        time_str = norm(raw_time) or "TBD"
    # URL
    url = norm(m.get("url") or "")
    if not url:
        event_id = m.get("eventId") or ""
        if event_id:
            chamber_path = (m.get("chamber") or chamber_hint or "").lower()
            url = f"https://www.congress.gov/committee-meeting/{event_id}"

    # Witnesses
    witnesses = []
    for w in (m.get("witnesses") or []):
        name = norm(w.get("name") or w.get("fullName") or "")
        if name:
            witnesses.append(name)
    if not witnesses:
        witnesses = ["TBD"]

    h = {
        "committee":    committee,
        "subcommittee": subcommittee,
        "topic":        topic,
        "date":         meeting_date.isoformat(),
        "time":         time_str,
        "location":     location,
        "why":          "",
        "tags":         [],
        "url":          url,
        "status":       norm(m.get("type") or m.get("status") or "Scheduled") or "Scheduled",
        "stream":       None,
        "witnesses":    witnesses,
        "sources":      [{"label": "Congress.gov API",
                          "url": url or "https://api.congress.gov/v3/committee-meeting"}],
        "docs":         [],
        "bills":        [],
        "provenance":   "CongressAPI",
        "confidence":   0.9,
        "needs_review": False,
    }
    h["hearing_id"] = compute_hearing_id(h)
    return h


# ---------------------------------------------------------------------------
# Main fetch loop
# ---------------------------------------------------------------------------

def fetch_all_for_range(api_key, start: date, end: date):
    """Fetch the meeting list filtered by date range, then get full details."""
    from_dt = f"{start.isoformat()}T00:00:00Z"
    to_dt   = f"{end.isoformat()}T23:59:59Z"

    all_hearings = []
    for chamber in ["house", "senate"]:
        print(f"  Fetching {chamber} meetings from Congress.gov API...")
        offset = 0
        page   = 0
        while True:
            data = fetch_meeting_list(api_key, chamber=chamber, offset=offset,
                                      from_dt=from_dt, to_dt=to_dt)
            if not data:
                break

            meetings = data.get("committeeMeetings", [])
            if not meetings:
                print(f"    No more meetings at offset={offset}")
                break

            page += 1
            print(f"    Page {page} (offset={offset}): {len(meetings)} meetings — fetching details...")

            for m in meetings:
                detail_url = m.get("url", "")
                if not detail_url:
                    continue
                detail = fetch_meeting_detail(detail_url, api_key)
                if not detail:
                    continue
                h = parse_detail(detail, chamber_hint=chamber)
                if h:
                    all_hearings.append(h)
                time.sleep(0.15)  # polite rate limit

            total = data.get("pagination", {}).get("count", len(meetings))
            offset += len(meetings)
            if offset >= total or len(meetings) < 250:
                break

    return all_hearings


# ---------------------------------------------------------------------------
# Merge into existing hearings_seed.json
# ---------------------------------------------------------------------------

def merge(existing, new_hearings):
    existing_ids = {h.get("hearing_id") for h in existing if h.get("hearing_id")}
    now_utc = datetime.now(timezone.utc).isoformat()
    added = 0
    for h in new_hearings:
        hid = h.get("hearing_id")
        if not hid:
            continue
        if hid not in existing_ids:
            h["first_seen_utc"] = now_utc
            h["last_seen_utc"]  = now_utc
            existing.append(h)
            existing_ids.add(hid)
            added += 1
        else:
            for ex in existing:
                if ex.get("hearing_id") == hid:
                    ex["last_seen_utc"] = now_utc
                    break
    return existing, added


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch committee hearings from Congress.gov API")
    parser.add_argument("--weeks", type=int, default=2,
                        help="How many weeks ahead to fetch (default: 2)")
    parser.add_argument("--test", action="store_true",
                        help="Fetch and print one meeting detail without saving")
    args = parser.parse_args()

    cfg = load_config()
    api_key = cfg.get("congress_api_key", "")
    if not api_key:
        print("ERROR: No congress_api_key found in data/config.json")
        print("Get a free key at https://api.congress.gov/sign-up/")
        return

    today      = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end   = week_start + timedelta(weeks=args.weeks) - timedelta(days=1)

    print(f"Fetching committee hearings: {week_start} → {week_end}")
    print(f"(Congress 119, both chambers)\n")

    if args.test:
        from_dt = f"{week_start.isoformat()}T00:00:00Z"
        to_dt   = f"{week_end.isoformat()}T23:59:59Z"
        data = fetch_meeting_list(api_key, chamber="senate",
                                  from_dt=from_dt, to_dt=to_dt)
        if data:
            meetings = data.get("committeeMeetings", [])
            print(f"List response — {len(meetings)} senate meetings in date range")
            if meetings:
                print(f"\nFirst meeting: {json.dumps(meetings[0], indent=2)}")
                detail_url = meetings[0].get("url", "")
                if detail_url:
                    print(f"\nFetching detail: {detail_url}")
                    detail = fetch_meeting_detail(detail_url, api_key)
                    if detail:
                        print(f"\nDetail keys: {list(detail.keys())}")
                        print(f"\nDetail preview:\n{json.dumps(detail, indent=2)[:2000]}")
        return

    new_hearings = fetch_all_for_range(api_key, week_start, week_end)
    print(f"\nTotal hearings found: {len(new_hearings)}")

    if not new_hearings:
        print("No hearings found in range. Try --test to inspect the API response.")
        return

    existing = load_hearings()
    existing, added = merge(existing, new_hearings)
    save_hearings(existing)

    print(f"Added {added} new hearings to hearings_seed.json")
    print(f"Total hearings in database: {len(existing)}")
    print("\nNow run: streamlit run app.py")


if __name__ == "__main__":
    main()