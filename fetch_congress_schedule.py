#!/usr/bin/env python3
"""
fetch_congress_schedule.py
Fetches ALL committee hearings from congress.gov daily schedule pages
(no API key needed) and adds them to hearings_seed.json.

Run with: python3 fetch_congress_schedule.py
"""

import json, re, hashlib, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

DATA_DIR      = Path("data")
HEARINGS_PATH = DATA_DIR / "hearings_seed.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())

def compute_id(h):
    base = "|".join([
        norm(h.get("committee","")),
        norm(h.get("subcommittee","")),
        norm(h.get("date","")),
        norm(h.get("time","")),
        norm(h.get("topic","")),
    ]).lower()
    return hashlib.sha1(base.encode()).hexdigest()[:12]

def fetch_day(target_date: date):
    url = f"https://www.congress.gov/committee-schedule/daily/{target_date.strftime('%Y/%m/%d')}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  Could not fetch {target_date}: {e}")
        return None

def parse_day(html: str, target_date: date):
    soup = BeautifulSoup(html, "html.parser")
    hearings = []

    # congress.gov uses an expanded list — each item has chamber + committee info
    # Try multiple selectors to be robust
    items = (
        soup.select("li.committee-meeting") or
        soup.select("li.expanded") or
        soup.select(".expanded li") or
        soup.select("table.committee-schedule tr") or
        []
    )

    # Fallback: look for any block that has "hearing" / "meeting" text near a committee name
    if not items:
        # Try finding the main schedule content area
        main = soup.find("main") or soup.find("div", {"id": "content"}) or soup.find("div", {"id": "main"})
        if main:
            items = main.find_all("li") or main.find_all("tr")

    for item in items:
        text = item.get_text(" ", strip=True)
        if len(text) < 20:
            continue

        # Chamber
        chamber = ""
        for word in ["SENATE", "HOUSE"]:
            if word in text.upper():
                chamber = word.capitalize()
                break

        # Committee — look for bold or strong tags, or the first substantial text
        committee = ""
        for tag in item.find_all(["strong", "b", "span", "td"]):
            t = norm(tag.get_text())
            if len(t) > 10 and any(w in t.lower() for w in ["committee", "senate", "house", "subcommittee"]):
                committee = t
                break

        if not committee:
            # Take first line-ish chunk as committee
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if lines:
                committee = lines[0][:100]

        # Topic — look for em, italic, or quoted text
        topic = ""
        for tag in item.find_all(["em", "i", "a", "p"]):
            t = norm(tag.get_text())
            if len(t) > 20 and t != committee:
                topic = t
                break

        if not topic:
            # Use the longest chunk of text
            chunks = [c.strip() for c in re.split(r"[|\n]", text) if len(c.strip()) > 30]
            if chunks:
                topic = max(chunks, key=len)[:300]

        if not topic or not committee:
            continue

        # Time
        time_match = re.search(r"\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)", text)
        time_str = time_match.group(0) if time_match else "TBD"

        # Location
        location = "TBD"
        loc_match = re.search(r"(\d+\w*[,\s]+(?:Dirksen|Russell|Hart|Rayburn|Longworth|Cannon|Capitol)[^|]+)", text)
        if loc_match:
            location = norm(loc_match.group(1))

        # Link
        link_tag = item.find("a", href=True)
        url_val = ""
        if link_tag:
            href = link_tag["href"]
            url_val = href if href.startswith("http") else f"https://www.congress.gov{href}"

        h = {
            "committee":    norm(committee),
            "subcommittee": "",
            "topic":        norm(topic),
            "date":         target_date.isoformat(),
            "time":         time_str,
            "location":     location,
            "why":          "",
            "tags":         [],
            "url":          url_val,
            "status":       "Scheduled",
            "stream":       None,
            "witnesses":    ["TBD"],
            "sources":      [{"label": "Congress.gov", "url": url_val or "https://www.congress.gov/committee-schedule"}],
            "docs":         [],
            "bills":        [],
            "provenance":   "CongressSchedule",
            "confidence":   0.8,
            "needs_review": False,
        }
        h["hearing_id"] = compute_id(h)
        hearings.append(h)

    return hearings

def main():
    today = date.today()
    # Fetch Mon–Fri for current week and next week
    week_start = today - timedelta(days=today.weekday())
    dates_to_fetch = []
    for week_offset in range(2):
        for day_offset in range(5):  # Mon–Fri
            d = week_start + timedelta(weeks=week_offset, days=day_offset)
            dates_to_fetch.append(d)

    print(f"Fetching congress.gov schedule for {len(dates_to_fetch)} days...\n")

    all_new = []
    for d in dates_to_fetch:
        print(f"  {d.strftime('%a %b %d')}...", end=" ")
        html = fetch_day(d)
        if html:
            found = parse_day(html, d)
            print(f"{len(found)} hearings found")
            all_new.extend(found)
        time.sleep(0.5)  # be polite to congress.gov

    # Load existing and merge
    existing = json.loads(HEARINGS_PATH.read_text()) if HEARINGS_PATH.exists() else []
    existing_ids = {h.get("hearing_id") for h in existing}
    now_utc = datetime.now(timezone.utc).isoformat()
    added = 0
    for h in all_new:
        hid = h.get("hearing_id")
        if hid and hid not in existing_ids:
            h["first_seen_utc"] = now_utc
            h["last_seen_utc"]  = now_utc
            existing.append(h)
            existing_ids.add(hid)
            added += 1

    HEARINGS_PATH.write_text(json.dumps(existing, indent=2, default=str))
    print(f"\nAdded {added} new hearings. Total in database: {len(existing)}")
    print("Now run: python3 digest.py --preview")

if __name__ == "__main__":
    main()
