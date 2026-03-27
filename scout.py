import json
import re
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

DATA_DIR = Path("data")
SOURCES_PATH = DATA_DIR / "committee_sources.json"
HEARINGS_PATH = DATA_DIR / "hearings_seed.json"   # your canonical store for now
REVIEW_QUEUE_PATH = DATA_DIR / "review_queue.json"
HEALTH_PATH = DATA_DIR / "source_health.json"
CONFIG_PATH = DATA_DIR / "config.json"

DATA_DIR.mkdir(exist_ok=True)

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}

CONGRESS_API_KEY = load_config().get("congress_api_key", "")


# -----------------------------
# Basic normalization / IDs
# -----------------------------
def norm_text(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def safe_date_str(d: Any) -> str:
    if isinstance(d, str) and d:
        return d
    return ""

def compute_hearing_id(h: Dict[str, Any]) -> str:
    base = "|".join([
        norm_text(h.get("committee")),
        norm_text(h.get("subcommittee")),
        norm_text(safe_date_str(h.get("date"))),
        norm_text(h.get("time", "")),
        norm_text(h.get("topic")),
        norm_text(h.get("location", "")),
        norm_text(h.get("url", "")),
    ]).lower()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]

def normalize_hearing(h: Dict[str, Any]) -> Dict[str, Any]:
    hh = dict(h)
    hh["committee"] = norm_text(hh.get("committee"))
    hh["subcommittee"] = norm_text(hh.get("subcommittee"))
    hh["topic"] = norm_text(hh.get("topic"))
    hh["time"] = norm_text(hh.get("time", "TBD")) or "TBD"
    hh["location"] = norm_text(hh.get("location", "TBD")) or "TBD"
    hh["status"] = norm_text(hh.get("status", "Scheduled")) or "Scheduled"
    hh["url"] = norm_text(hh.get("url", ""))
    hh["why"] = (hh.get("why") or "").strip()
    hh["tags"] = sorted({t.strip() for t in (hh.get("tags") or []) if t and t.strip()})
    hh["witnesses"] = hh.get("witnesses") or ["TBD"]
    hh["sources"] = hh.get("sources") or []
    hh["docs"] = hh.get("docs") or []
    hh["bills"] = hh.get("bills") or []
    hh["provenance"] = hh.get("provenance") or "Scout"
    hh["confidence"] = float(hh.get("confidence") or 0.55)

    now_utc = datetime.now(timezone.utc).isoformat()
    hh["first_seen_utc"] = hh.get("first_seen_utc") or now_utc
    hh["last_seen_utc"] = hh.get("last_seen_utc") or now_utc

    hh["hearing_id"] = hh.get("hearing_id") or compute_hearing_id(hh)
    return hh


# -----------------------------
# IO helpers
# -----------------------------
def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default

def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2))


# -----------------------------
# Source registry
# committee_sources.json expected:
# {
#   "House Energy & Commerce": {"calendar": "...", "hearings": "...", "homepage": "...", "subcommittees": [{"name":"Energy","url":"..."}]}
# }
# -----------------------------
def load_sources() -> Dict[str, Any]:
    return load_json(SOURCES_PATH, {})
def build_committee_name_map(sources: dict) -> dict:
    """
    Map fuzzy congress.gov committee strings -> your canonical committee keys.
    We'll match by chamber + key words (commerce, energy, environment, etc.).
    """
    canon = list(sources.keys())

    # Precompute normalized canonical forms
    canon_norm = {c: re.sub(r"[^a-z0-9]+", " ", c.lower()).strip() for c in canon}

    def normalize(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    mapping = {}
    # Basic alias patterns seen on Congress.gov weekly page
    # e.g. "Senate Committee on Commerce, Science, and Transportation"
    # e.g. "House Committee on Energy and Commerce"
    for c in canon:
        cn = canon_norm[c]
        # Add a couple of generic aliases
        mapping[cn] = c

    return {"canon": canon, "canon_norm": canon_norm, "normalize": lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()}

def match_committee_name(raw: str, sources: dict, name_map: dict) -> str:
    """
    Given a Congress.gov committee string, return the best matching canonical committee key.
    Falls back to raw if no match.
    """
    normalize = name_map["normalize"]
    raw_n = normalize(raw)

    # Strip common phrases
    raw_n = raw_n.replace("house committee on", "house").replace("senate committee on", "senate")
    raw_n = raw_n.replace("committee on", "").strip()

    best = ("", 0)
    for canon, canon_n in name_map["canon_norm"].items():
        score = 0
        # Strong chamber match
        if raw_n.startswith("house") and canon_n.startswith("house"):
            score += 3
        if raw_n.startswith("senate") and canon_n.startswith("senate"):
            score += 3

        # token overlap
        raw_tokens = set(raw_n.split())
        canon_tokens = set(canon_n.split())
        overlap = len(raw_tokens & canon_tokens)
        score += overlap

        # boost for key distinctive words
        for kw in ["commerce", "energy", "environment", "public", "works", "natural", "resources",
                   "transportation", "infrastructure", "science", "technology", "ways", "means",
                   "foreign", "relations", "agriculture", "forestry"]:
            if kw in raw_tokens and kw in canon_tokens:
                score += 2

        if score > best[1]:
            best = (canon, score)

    return best[0] if best[1] >= 6 else raw  # threshold prevents bad matches


# -----------------------------
# HTML scouting (best-effort)
# -----------------------------
MONTH_WORD = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t\.?|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DATE_PATTERNS = [
    re.compile(rf"\b{MONTH_WORD}\s+\d{{1,2}}(?:,\s*\d{{4}})?\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
]

HEARING_HINTS = [
    "hearing", "markup", "roundtable", "business meeting",
    "member day", "oversight", "legislative hearing"
]

def parse_date_from_text(text: str) -> Optional[str]:
    t = norm_text(text)
    for pat in DATE_PATTERNS:
        m = pat.search(t)
        if m:
            chunk = m.group(0)
            try:
                dt = dtparser.parse(chunk, fuzzy=True)
                return dt.date().strftime("%Y-%m-%d")
            except Exception:
                return None
    return None

def looks_like_hearing(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in HEARING_HINTS)

def absolute_url(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    # simple join
    if base.endswith("/") and href.startswith("/"):
        return base[:-1] + href
    if (not base.endswith("/")) and (not href.startswith("/")):
        return base + "/" + href
    return base + href

def scout_page(committee: str, url: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (candidates, health_entry). Candidates may be partial and marked needs_review."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ClearPathHearingsBot/0.1; +https://clearpath.org)"
    }
    health = {
        "status": "ok",
        "last_checked_utc": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "error": ""
    }

    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code >= 400:
            health["status"] = "failed"
            health["error"] = f"HTTP {r.status_code}"
            return [], health

        soup = BeautifulSoup(r.text, "html.parser")

        candidates: List[Dict[str, Any]] = []

        # Heuristic: look at links + surrounding text blocks
        for a in soup.find_all("a", href=True):
            title = norm_text(a.get_text(" ", strip=True))
            href = a.get("href", "")
            if not title or len(title) < 8:
                continue

            # Only keep likely hearing-ish items
            context_text = title
            parent = a.parent
            if parent:
                context_text = norm_text(parent.get_text(" ", strip=True))[:400]

            if not looks_like_hearing(context_text) and not looks_like_hearing(title):
                continue

            d = parse_date_from_text(context_text) or parse_date_from_text(title)
            full_url = absolute_url(url.split("/")[0] + "//" + url.split("/")[2], href)

            cand = {
                "date": d or "",  # may be empty; handled as needs_review
                "committee": committee,
                "subcommittee": "",
                "topic": title,
                "time": "TBD",
                "location": "TBD",
                "why": "",
                "tags": [],
                "url": full_url or url,
                "status": "Scheduled",
                "stream": None,
                "witnesses": ["TBD"],
                "sources": [{"label": "Committee page", "url": url}],
                "docs": [],
                "bills": [],
                "provenance": "Scout",
                "confidence": 0.55,
                "needs_review": True,  # default; upgraded below if we have date+url
            }

            if d and full_url:
                cand["confidence"] = 0.75
                cand["needs_review"] = False

            candidates.append(normalize_hearing(cand))

        # Deduplicate candidates by hearing_id
        uniq = {}
        for c in candidates:
            uniq[c["hearing_id"]] = c
        return list(uniq.values()), health

    except Exception as e:
        health["status"] = "failed"
        health["error"] = str(e)[:240]
        return [], health


# -----------------------------
# Merge + diff
# -----------------------------
def index_by_id(hearings: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for h in hearings:
        hh = normalize_hearing(h)
        out[hh["hearing_id"]] = hh
    return out

DIFF_FIELDS = ["date", "time", "location", "topic", "status", "url"]
def monday_of_week(d):
    return d - timedelta(days=d.weekday())

def to_week_url(week_start_date: date) -> str:
    return f"https://www.congress.gov/committee-schedule/weekly/{week_start_date.strftime('%Y/%m/%d')}"

def parse_time_from_text(text: str) -> str:
    m = re.search(r"\b\d{1,2}:\d{2}\s*(AM|PM)\b", text, re.I)
    if m:
        return m.group(0).upper()
    return "TBD"

# -----------------------------
# Congress.gov API Integration
# -----------------------------
def scout_congress_api(congress: int = 119, days_ahead: int = 30) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch committee meetings from Congress.gov API for both chambers.

    Only fetches detail for meetings within the next `days_ahead` days to avoid rate limits.
    """
    if not CONGRESS_API_KEY:
        return [], {"status": "failed", "error": "No API key configured", "last_checked_utc": datetime.now(timezone.utc).isoformat()}

    base_url = "https://api.congress.gov/v3/committee-meeting"
    all_candidates = []
    health = {"status": "ok", "last_checked_utc": datetime.now(timezone.utc).isoformat(), "error": "", "meetings_found": 0}

    today = datetime.now().date()
    cutoff_date = today + timedelta(days=days_ahead)

    for chamber in ["house", "senate"]:
        url = f"{base_url}/{congress}/{chamber}"
        params = {
            "api_key": CONGRESS_API_KEY,
            "limit": 250,
            "format": "json"
        }

        try:
            print(f"  Fetching {chamber} meetings...")
            r = requests.get(url, params=params, timeout=30)
            if r.status_code >= 400:
                health["status"] = "partial" if all_candidates else "failed"
                health["error"] += f"{chamber}: HTTP {r.status_code}; "
                continue

            data = r.json()
            meetings = data.get("committeeMeetings", [])
            print(f"  Found {len(meetings)} {chamber} meetings in list")

            fetched_details = 0
            for meeting in meetings:
                # Fetch detailed meeting info (only for recent meetings to avoid rate limits)
                detail_url = meeting.get("url")
                meeting_detail = meeting

                if detail_url and fetched_details < 100:  # Limit detail fetches
                    try:
                        detail_r = requests.get(detail_url, params={"api_key": CONGRESS_API_KEY, "format": "json"}, timeout=10)
                        if detail_r.status_code == 200:
                            meeting_detail = detail_r.json().get("committeeMeeting", {})
                            fetched_details += 1
                    except:
                        pass

                # Parse meeting data
                meeting_date = meeting_detail.get("date", "")
                if meeting_date:
                    try:
                        dt = dtparser.parse(meeting_date)
                        date_str = dt.strftime("%Y-%m-%d")
                        time_str = dt.strftime("%I:%M %p").lstrip("0")
                    except:
                        date_str = meeting_date[:10] if len(meeting_date) >= 10 else ""
                        time_str = "TBD"
                else:
                    date_str = ""
                    time_str = "TBD"

                # Get committee info
                committees = meeting_detail.get("committees", [])
                committee_name = ""
                subcommittee_name = ""
                for comm in committees:
                    name = comm.get("name", "")
                    comm_type = comm.get("type", "")
                    if comm_type == "Subcommittee":
                        subcommittee_name = name
                    else:
                        chamber_prefix = "House" if chamber == "house" else "Senate"
                        committee_name = f"{chamber_prefix} {name}" if name and not name.startswith(chamber_prefix) else name

                # Get witnesses
                witnesses = []
                for w in meeting_detail.get("witnesses", []):
                    witness_name = w.get("name", "")
                    witness_org = w.get("organization", "")
                    if witness_name:
                        witnesses.append(f"{witness_name}" + (f" ({witness_org})" if witness_org else ""))

                # Get related bills - relatedItems is a dict with 'bills' key
                bills = []
                related = meeting_detail.get("relatedItems", {})
                if isinstance(related, dict):
                    for item in related.get("bills", []):
                        bill_num = item.get("number", "")
                        bill_type = item.get("type", "")
                        if bill_num:
                            bills.append(f"{bill_type} {bill_num}")

                # Parse location - can be dict with building/room
                location_raw = meeting_detail.get("location", "")
                if isinstance(location_raw, dict):
                    building = location_raw.get("building", "")
                    room = location_raw.get("room", "")
                    location_str = f"{room}, {building}" if room and building else room or building or "TBD"
                else:
                    location_str = location_raw or "TBD"

                # Build hearing record
                cand = {
                    "date": date_str,
                    "committee": committee_name or f"{chamber.title()} Committee",
                    "subcommittee": subcommittee_name,
                    "topic": meeting_detail.get("title", "") or "Committee Meeting",
                    "time": time_str,
                    "location": location_str,
                    "why": "",
                    "tags": [],
                    "url": meeting_detail.get("url", "") or f"https://www.congress.gov/committee-meeting/{congress}/{chamber}/{meeting.get('eventId', '')}",
                    "status": meeting_detail.get("meetingStatus", "Scheduled"),
                    "stream": None,
                    "witnesses": witnesses if witnesses else ["TBD"],
                    "sources": [{"label": "Congress.gov API", "url": f"https://api.congress.gov/v3/committee-meeting/{congress}/{chamber}"}],
                    "docs": [],
                    "bills": bills,
                    "provenance": "Congress.gov API",
                    "confidence": 0.95,
                    "event_id": meeting.get("eventId", ""),
                    "first_seen_utc": datetime.now(timezone.utc).isoformat(),
                    "last_seen_utc": datetime.now(timezone.utc).isoformat(),
                }

                all_candidates.append(normalize_hearing(cand))

        except Exception as e:
            health["status"] = "partial" if all_candidates else "failed"
            health["error"] += f"{chamber}: {str(e)[:100]}; "

    health["meetings_found"] = len(all_candidates)
    return all_candidates, health


def scout_congress_weekly(week_start_date: date, sources: dict, name_map: dict) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    url = to_week_url(week_start_date)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ClearPathHearingsBot/0.2)"}
    health = {"status": "ok", "last_checked_utc": datetime.now(timezone.utc).isoformat(), "url": url, "error": ""}

    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code >= 400:
            health["status"] = "failed"
            health["error"] = f"HTTP {r.status_code}"
            return [], health

        soup = BeautifulSoup(r.text, "html.parser")
        candidates = []

        # Strategy:
        # Each meeting line usually contains "Meeting Details" link and committee name nearby.
        for a in soup.find_all("a", href=True):
            if not a.get_text(strip=True).lower().startswith("meeting details"):
                continue

            meeting_url = a["href"]
            if meeting_url.startswith("/"):
                meeting_url = "https://www.congress.gov" + meeting_url

            # Get surrounding block text
            block = a.find_parent()
            block_text = norm_text(block.get_text(" ", strip=True)) if block else ""

            # Find date by walking backwards for a day header
            date_str = None
            node = a
            for _ in range(60):
                node = node.find_previous()
                if not node:
                    break
                t = norm_text(node.get_text(" ", strip=True))
                maybe = parse_date_from_text(t)
                if maybe:
                    date_str = maybe
                    break

            time_str = parse_time_from_text(block_text)

            # Committee parsing: look for "House Committee on ..." or "Senate Committee on ..."
            # If not found, use the first chunk before "|"
            committee_raw = ""
            m = re.search(r"(House|Senate)\s+Committee\s+on\s+([^|]+)", block_text, re.I)
            if m:
                chamber = m.group(1).title()
                name = m.group(2).strip()
                committee_raw = f"{chamber} {name}"
            else:
                # fallback: first pipe segment
                committee_raw = block_text.split("|")[0].strip()

            committee = match_committee_name(committee_raw, sources, name_map)

            # Subcommittee often appears as "Subcommittee on X"
            subcommittee = ""
            sm = re.search(r"Subcommittee\s+on\s+([^|]+)", block_text, re.I)
            if sm:
                subcommittee = sm.group(1).strip()

            topic = block_text.split("|")[0].strip() if "|" in block_text else block_text
            topic = topic[:220]  # keep it clean

            cand = {
                "date": date_str or "",
                "committee": committee,
                "subcommittee": subcommittee,
                "topic": topic,
                "time": time_str,
                "location": "TBD",
                "why": "",
                "tags": [],
                "url": meeting_url or url,
                "status": "Scheduled",
                "stream": None,
                "witnesses": ["TBD"],
                "sources": [{"label": "Congress.gov weekly schedule", "url": url}],
                "docs": [],
                "bills": [],
                "provenance": "CongressGov",
                "confidence": 0.7 if date_str else 0.6,
                "needs_review": True,
            }
            candidates.append(normalize_hearing(cand))

        uniq = {c["hearing_id"]: c for c in candidates}
        return list(uniq.values()), health

    except Exception as e:
        health["status"] = "failed"
        health["error"] = str(e)[:240]
        return [], health

def diff_hearing(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    changed = []
    for f in DIFF_FIELDS:
        if norm_text(str(old.get(f, ""))) != norm_text(str(new.get(f, ""))):
            changed.append(f)
    return changed

def run_scout() -> None:
    sources = load_sources()
    existing = load_json(HEARINGS_PATH, [])
    existing_index = index_by_id(existing)

    review_queue = {
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "new": [],
        "changed": [],
        "missing": [],
        "needs_review": [],
        "failed_sources": []
    }
    health_report: Dict[str, Any] = {}
    seen_ids = set()
    name_map = build_committee_name_map(sources)

    # PRIMARY SOURCE: Congress.gov API (119th Congress)
    print("Fetching from Congress.gov API...")
    api_candidates, api_health = scout_congress_api(congress=119)
    health_report["Congress.gov API"] = api_health
    if api_health["status"] == "failed":
        review_queue["failed_sources"].append("Congress.gov API")
        print(f"  API failed: {api_health.get('error', 'unknown error')}")
    else:
        print(f"  Found {api_health.get('meetings_found', 0)} meetings from API")

    for c in api_candidates:
        hid = c["hearing_id"]
        seen_ids.add(hid)

        if hid not in existing_index:
            review_queue["new"].append(hid)
            existing_index[hid] = c
        else:
            old = existing_index[hid]
            changed_fields = diff_hearing(old, c)
            if changed_fields:
                review_queue["changed"].append({"id": hid, "fields": changed_fields})
                merged = dict(old)
                for k, v in c.items():
                    if v not in ["", None, [], ["TBD"], "TBD"]:
                        merged[k] = v
                merged["last_seen_utc"] = datetime.now(timezone.utc).isoformat()
                existing_index[hid] = normalize_hearing(merged)

    # FALLBACK: Congress.gov weekly scraping (if API didn't work well)
    today = datetime.now().date()
    wk0 = monday_of_week(today)

    for i in range(4):
        week_start = wk0 + timedelta(days=7*i)
        cg_candidates, cg_health = scout_congress_weekly(week_start, sources, name_map)
        key = f"Congress.gov weekly ({week_start.strftime('%Y-%m-%d')})"
        health_report[key] = cg_health
        if cg_health["status"] != "ok":
            review_queue["failed_sources"].append(key)

        for c in cg_candidates:
            hid = c["hearing_id"]
            seen_ids.add(hid)

            if hid not in existing_index:
                review_queue["new"].append(hid)
                existing_index[hid] = c
            else:
                old = existing_index[hid]
                changed_fields = diff_hearing(old, c)
                if changed_fields:
                    review_queue["changed"].append({"id": hid, "fields": changed_fields})
                    merged = dict(old)
                    for k, v in c.items():
                        if v not in ["", None, [], ["TBD"], "TBD"]:
                            merged[k] = v
                    merged["last_seen_utc"] = datetime.now(timezone.utc).isoformat()
                    existing_index[hid] = normalize_hearing(merged)

            if existing_index[hid].get("needs_review"):
                review_queue["needs_review"].append(hid)

    for committee, meta in sources.items():
        # choose best primary source url
        url = meta.get("calendar") or meta.get("hearings") or meta.get("homepage")
        if not url:
            health_report[committee] = {
                "status": "failed",
                "last_checked_utc": datetime.now(timezone.utc).isoformat(),
                "url": "",
                "error": "No source URL configured"
            }
            review_queue["failed_sources"].append(committee)
            continue

        candidates, health = scout_page(committee, url)
        health_report[committee] = health
        if health["status"] != "ok":
            review_queue["failed_sources"].append(committee)

        for c in candidates:
            hid = c["hearing_id"]
            seen_ids.add(hid)

            # If we don't have a date, force needs_review
            if not c.get("date"):
                c["needs_review"] = True
                c["confidence"] = min(float(c.get("confidence", 0.55)), 0.55)

            if hid not in existing_index:
                # new candidate
                review_queue["new"].append(hid)
                existing_index[hid] = c
            else:
                old = existing_index[hid]
                changed_fields = diff_hearing(old, c)
                if changed_fields:
                    review_queue["changed"].append({"id": hid, "fields": changed_fields})
                    # merge "newer" values in (keep old rich fields if new is empty)
                    merged = dict(old)
                    for k, v in c.items():
                        if v not in ["", None, [], ["TBD"], "TBD"]:
                            merged[k] = v
                    merged["last_seen_utc"] = datetime.now(timezone.utc).isoformat()
                    existing_index[hid] = normalize_hearing(merged)
                else:
                    # just mark seen
                    old["last_seen_utc"] = datetime.now(timezone.utc).isoformat()
                    existing_index[hid] = normalize_hearing(old)

            if existing_index[hid].get("needs_review"):
                review_queue["needs_review"].append(hid)

    # Anything in existing not seen in this run could be "missing" (only if provenance is Scout)
    for hid, h in existing_index.items():
        if h.get("provenance") == "Scout" and hid not in seen_ids:
            review_queue["missing"].append(hid)

    # Write outputs
    updated = list(existing_index.values())
    save_json(HEARINGS_PATH, updated)
    save_json(REVIEW_QUEUE_PATH, review_queue)
    save_json(HEALTH_PATH, health_report)

    print(f"✅ Scout complete. Hearings: {len(updated)} | New: {len(review_queue['new'])} | Changed: {len(review_queue['changed'])} | Needs review: {len(review_queue['needs_review'])} | Missing: {len(review_queue['missing'])}")


if __name__ == "__main__":
    run_scout()
