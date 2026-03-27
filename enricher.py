"""
enricher.py — ClearPath Hearings Dashboard
Enriches hearing data with:
  1. Congress.gov API  → committee member rosters (party, state, leadership roles)
  2. GovInfo.gov API   → transcript / document links for past hearings

Uses the same Congress.gov API key already in data/config.json — no new keys needed.

Usage:
  python enricher.py                   # enrich all relevant hearings
  python enricher.py --committee-only  # only fetch member data, skip GovInfo
  python enricher.py --govinfo-only    # only search for transcripts
  python enricher.py --force           # re-fetch even if cached

Optional: add a free GovInfo key to data/config.json for higher rate limits:
  https://api.govinfo.gov/ → "Request API Key"
  {
    "congress_api_key": "...",
    "govinfo_api_key": "YOUR_GOVINFO_KEY"
  }
"""

import argparse
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR        = Path("data")
HEARINGS_PATH   = DATA_DIR / "hearings_seed.json"
CONFIG_PATH     = DATA_DIR / "config.json"
ENRICHMENT_PATH = DATA_DIR / "enrichment_cache.json"   # separate cache file
MEMBERS_PATH    = DATA_DIR / "committee_members.json"  # committee roster cache

DATA_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}

cfg = load_config()
CONGRESS_API_KEY = cfg.get("congress_api_key", "")
GOVINFO_KEY      = cfg.get("govinfo_api_key", "DEMO_KEY")
CONGRESS_NUM     = 119  # 119th Congress (2025-2027)

# ---------------------------------------------------------------------------
# ProPublica committee ID mapping
# Maps ClearPath canonical committee names → ProPublica committee codes
# ---------------------------------------------------------------------------
COMMITTEE_ID_MAP: Dict[str, Tuple[str, str]] = {
    # (chamber, propublica_committee_id)
    "House Energy & Commerce":               ("house",  "HSIF"),
    "House Natural Resources":               ("house",  "HSII"),
    "House Transportation & Infrastructure": ("house",  "HSPW"),
    "House Ways & Means":                    ("house",  "HSWM"),
    "House Science, Space, & Technology":    ("house",  "HSSY"),
    "Senate Energy & Natural Resources":     ("senate", "SSEG"),
    "Senate Environment & Public Works":     ("senate", "SSEV"),
    "Senate Agriculture, Nutrition, & Forestry": ("senate", "SSAF"),
    "Senate Foreign Relations":              ("senate", "SSFR"),
    "Senate Commerce, Science, & Transportation": ("senate", "SSCM"),
}

# Fuzzy matching: map congress.gov API committee name fragments → ClearPath canonical
COMMITTEE_NAME_FRAGMENTS: Dict[str, str] = {
    "energy and commerce":                   "House Energy & Commerce",
    "energy & commerce":                     "House Energy & Commerce",
    "natural resources":                     "House Natural Resources",
    "transportation and infrastructure":     "House Transportation & Infrastructure",
    "transportation & infrastructure":       "House Transportation & Infrastructure",
    "ways and means":                        "House Ways & Means",
    "ways & means":                          "House Ways & Means",
    "science, space, and technology":        "House Science, Space, & Technology",
    "science, space, & technology":          "House Science, Space, & Technology",
    "energy and natural resources":          "Senate Energy & Natural Resources",
    "energy & natural resources":            "Senate Energy & Natural Resources",
    "environment and public works":          "Senate Environment & Public Works",
    "environment & public works":            "Senate Environment & Public Works",
    "agriculture, nutrition":               "Senate Agriculture, Nutrition, & Forestry",
    "foreign relations":                     "Senate Foreign Relations",
    "commerce, science, and transportation": "Senate Commerce, Science, & Transportation",
    "commerce, science, & transportation":   "Senate Commerce, Science, & Transportation",
}

def normalize_committee_name(name: str) -> str:
    return name.lower().replace("&", "and").strip()

def resolve_canonical_committee(committee_raw: str) -> Optional[str]:
    """Map a raw committee name string to one of ClearPath's tracked committees."""
    n = normalize_committee_name(committee_raw)
    # Sort fragments longest-first so more specific matches win
    sorted_fragments = sorted(COMMITTEE_NAME_FRAGMENTS.items(), key=lambda x: -len(x[0]))
    for fragment, canonical in sorted_fragments:
        if normalize_committee_name(fragment) in n:
            return canonical
    # Exact match against canonical keys (also longest-first)
    for canonical in sorted(COMMITTEE_ID_MAP, key=len, reverse=True):
        if normalize_committee_name(canonical) in n or n in normalize_committee_name(canonical):
            return canonical
    return None

# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default

def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str))

# ---------------------------------------------------------------------------
# Congress.gov Committee API client
# Uses the same API key already in config.json — no new signup needed
# ---------------------------------------------------------------------------
class CongressCommitteeClient:
    BASE = "https://api.congress.gov/v3"

    def __init__(self, api_key: str):
        self.key = api_key
        self.session = requests.Session()

    def _params(self, extra: dict = {}) -> dict:
        return {"api_key": self.key, "format": "json", **extra}

    def get_committee_members(self, chamber: str, committee_code: str) -> List[Dict]:
        """
        Fetch current member list for a committee via Congress.gov API.
        Returns list of member dicts with name, party, state, role fields.
        """
        url = f"{self.BASE}/committee/{CONGRESS_NUM}/{chamber}/{committee_code}"
        try:
            r = self.session.get(url, params=self._params(), timeout=15)
            r.raise_for_status()
            data = r.json()
            committee_data = data.get("committee", {})
            members = []

            # History / current members
            history = committee_data.get("history", [])
            for entry in history:
                # Only current members (no end date or end date in future)
                official_name = entry.get("officialName", "") or entry.get("name", "")
                if not official_name:
                    continue
                members.append({
                    "name":          official_name,
                    "party":         entry.get("partyName", ""),
                    "state":         "",   # not always in this endpoint
                    "role":          entry.get("relationshipType", ""),
                    "bioguide_id":   entry.get("bioguideId", ""),
                    "start_date":    entry.get("startDate", ""),
                    "end_date":      entry.get("endDate", ""),
                    "is_leadership": False,
                })

            # Also pull from subcommittees list for subcommittee chair info
            # Mark chair/ranking based on role field
            for m in members:
                role_lower = (m.get("role") or "").lower()
                if "chair" in role_lower or "ranking" in role_lower:
                    m["is_leadership"] = True

            # Dedupe by bioguide_id
            seen = set()
            unique_members = []
            for m in members:
                bid = m.get("bioguide_id") or m.get("name")
                if bid not in seen:
                    seen.add(bid)
                    unique_members.append(m)

            return unique_members

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # Try alternate endpoint: committee list then filter
                return self._get_members_from_list(chamber, committee_code)
            print(f"  Congress API error ({committee_code}): {e}")
            return []
        except Exception as e:
            print(f"  Congress API error ({committee_code}): {e}")
            return []

    def _get_members_from_list(self, chamber: str, committee_code: str) -> List[Dict]:
        """Fallback: fetch committee list and find matching committee."""
        url = f"{self.BASE}/committee/{CONGRESS_NUM}/{chamber}"
        try:
            r = self.session.get(url, params=self._params({"limit": 250}), timeout=15)
            r.raise_for_status()
            data = r.json()
            committees = data.get("committees", [])
            for c in committees:
                if c.get("systemCode", "").upper() == committee_code.upper():
                    # Fetch detail
                    detail_url = c.get("url", "")
                    if detail_url:
                        dr = self.session.get(detail_url, params=self._params(), timeout=15)
                        if dr.status_code == 200:
                            detail = dr.json().get("committee", {})
                            return self._parse_members_from_detail(detail)
            return []
        except Exception as e:
            print(f"  Congress API fallback error: {e}")
            return []

    def _parse_members_from_detail(self, detail: dict) -> List[Dict]:
        members = []
        for entry in detail.get("history", []):
            name = entry.get("officialName", "") or entry.get("name", "")
            if not name:
                continue
            members.append({
                "name":          name,
                "party":         entry.get("partyName", ""),
                "state":         "",
                "role":          entry.get("relationshipType", ""),
                "bioguide_id":   entry.get("bioguideId", ""),
                "is_leadership": "chair" in (entry.get("relationshipType") or "").lower(),
            })
        return members


# ---------------------------------------------------------------------------
# GovInfo.gov client
# ---------------------------------------------------------------------------
class GovInfoClient:
    BASE    = "https://api.govinfo.gov"
    SEARCH  = f"{BASE}/search"
    COLLECT = f"{BASE}/collections"

    def __init__(self, api_key: str = "DEMO_KEY"):
        self.key = api_key
        self.session = requests.Session()

    def _params(self, extra: dict = {}) -> dict:
        return {"api_key": self.key, **extra}

    def search_hearing_transcript(
        self,
        committee: str,
        hearing_date: date,
        topic: str = "",
        window_days: int = 90,
    ) -> Optional[Dict]:
        """
        Search GovInfo CHRG collection for a transcript matching this hearing.
        Transcripts are typically published 2-8 weeks after the hearing date.
        Returns a dict with 'title', 'pdf_url', 'package_id', 'date_issued' or None.
        """
        # GovInfo transcripts publish after the hearing — search a forward window
        search_start = hearing_date.strftime("%Y-%m-%dT00:00:00Z")
        search_end   = (hearing_date + timedelta(days=window_days)).strftime("%Y-%m-%dT00:00:00Z")

        # Build search query: committee name keywords + hearing topic keywords
        committee_clean = re.sub(r"[^a-zA-Z0-9 ]", " ", committee)
        topic_words = " ".join(topic.split()[:6]) if topic else ""
        query_parts = [w for w in committee_clean.split() if len(w) > 3][:4]
        if topic_words:
            query_parts += topic.split()[:3]
        query = " ".join(query_parts)

        params = self._params({
            "query":               query,
            "collection":          "CHRG",
            "dateIssuedStartDate": search_start,
            "dateIssuedEndDate":   search_end,
            "pageSize":            5,
            "offsetMark":          "*",
        })

        try:
            r = self.session.get(self.SEARCH, params=params, timeout=20)
            if r.status_code == 429:
                print("  GovInfo rate limited — pausing 10s...")
                time.sleep(10)
                r = self.session.get(self.SEARCH, params=params, timeout=20)
            if r.status_code >= 400:
                print(f"  GovInfo search error: HTTP {r.status_code}")
                return None

            data = r.json()
            results = data.get("results", {}).get("packages", [])
            if not results:
                return None

            # Pick best match: prefer results where committee name appears in title
            committee_words = set(normalize_committee_name(committee).split()) - {"on", "the", "and", "of", "for", "in"}
            best = None
            best_score = 0

            for pkg in results:
                title = (pkg.get("title") or "").lower()
                score = sum(1 for w in committee_words if w in title)
                if score > best_score:
                    best_score = score
                    best = pkg

            if not best or best_score < 2:
                return None

            package_id = best.get("packageId", "")
            pdf_url    = f"https://www.govinfo.gov/content/pkg/{package_id}/pdf/{package_id}.pdf"
            html_url   = f"https://www.govinfo.gov/content/pkg/{package_id}/html/{package_id}.htm"

            return {
                "package_id":   package_id,
                "title":        best.get("title", ""),
                "date_issued":  best.get("dateIssued", ""),
                "pdf_url":      pdf_url,
                "html_url":     html_url,
                "govinfo_url":  f"https://www.govinfo.gov/app/details/{package_id}",
                "score":        best_score,
            }

        except Exception as e:
            print(f"  GovInfo error: {e}")
            return None

    def get_recent_hearings(self, days_back: int = 60) -> List[Dict]:
        """Fetch a bulk list of recent hearing transcripts from CHRG collection."""
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=days_back)
        url   = f"{self.COLLECT}/CHRG/{start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        params = self._params({"pageSize": 100})

        try:
            r = self.session.get(url, params=params, timeout=25)
            r.raise_for_status()
            data = r.json()
            return data.get("packages", [])
        except Exception as e:
            print(f"  GovInfo collection error: {e}")
            return []


# ---------------------------------------------------------------------------
# Core enrichment logic
# ---------------------------------------------------------------------------
def enrich_committee_members(
    client: CongressCommitteeClient,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Fetch and cache member rosters for all tracked committees.
    Returns the full member cache dict.
    """
    cache = load_json(MEMBERS_PATH, {})
    now   = datetime.now(timezone.utc).isoformat()

    for canonical, (chamber, committee_id) in COMMITTEE_ID_MAP.items():
        # Skip if recently cached (within 24h) and not forcing
        cached = cache.get(canonical, {})
        last_fetched = cached.get("fetched_utc", "")
        if not force and last_fetched:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(last_fetched)
                if age.total_seconds() < 86400:  # 24 hours
                    print(f"  ✓ {canonical} (cached {int(age.total_seconds()/3600)}h ago)")
                    continue
            except Exception:
                pass

        print(f"  → Fetching members: {canonical} ({committee_id})...")
        members = client.get_committee_members(chamber, committee_id)
        cache[canonical] = {
            "committee_id": committee_id,
            "chamber":      chamber,
            "members":      members,
            "count":        len(members),
            "fetched_utc":  now,
        }
        print(f"    Found {len(members)} members")
        time.sleep(0.4)  # be polite to ProPublica

    save_json(MEMBERS_PATH, cache)
    print(f"  Committee member cache saved → {MEMBERS_PATH}")
    return cache


def enrich_govinfo_transcripts(
    hearings: List[Dict],
    client: GovInfoClient,
    cache: Dict,
    force: bool = False,
    min_days_old: int = 14,  # don't search for hearings newer than this
) -> Tuple[List[Dict], Dict]:
    """
    For past hearings, search GovInfo for transcript PDFs.
    Updates hearings in-place and returns updated list + cache.
    """
    today = date.today()
    enriched_count = 0

    for h in hearings:
        hearing_id = h.get("hearing_id", "")
        raw_date   = h.get("date")

        # Parse date
        if isinstance(raw_date, str) and raw_date:
            try:
                hearing_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except Exception:
                continue
        elif isinstance(raw_date, date):
            hearing_date = raw_date
        else:
            continue

        # Only look for transcripts of past hearings that are old enough
        days_old = (today - hearing_date).days
        if days_old < min_days_old:
            continue

        # Skip if already has a GovInfo transcript link
        existing_docs = h.get("docs") or []
        has_transcript = any("govinfo" in (d.get("url", "") or "").lower() for d in existing_docs)
        if has_transcript and not force:
            continue

        # Skip if already in cache and not forcing
        if hearing_id in cache and not force:
            cached_result = cache[hearing_id]
            if cached_result.get("transcript"):
                # Apply cached transcript link
                _apply_transcript(h, cached_result["transcript"])
            continue

        committee = h.get("committee", "")
        topic     = h.get("topic", "")

        print(f"  → GovInfo search: {hearing_date} | {committee[:40]}...")
        result = client.search_hearing_transcript(committee, hearing_date, topic)

        cache[hearing_id] = {
            "searched_utc": datetime.now(timezone.utc).isoformat(),
            "transcript":   result,
        }

        if result:
            _apply_transcript(h, result)
            enriched_count += 1
            print(f"    ✓ Found: {result['title'][:60]}...")
        else:
            print(f"    — No transcript found yet")

        time.sleep(0.5)  # rate limiting

    print(f"  GovInfo: added/updated transcript links for {enriched_count} hearings")
    return hearings, cache


def _apply_transcript(hearing: Dict, transcript: Dict) -> None:
    """Apply a GovInfo transcript result to a hearing's docs field."""
    docs = hearing.get("docs") or []
    # Remove old govinfo entries if re-enriching
    docs = [d for d in docs if "govinfo" not in (d.get("url", "") or "").lower()]
    docs.append({
        "label": "📄 Transcript (PDF)",
        "url":   transcript["pdf_url"],
        "type":  "transcript",
    })
    docs.append({
        "label": "📃 Transcript (HTML)",
        "url":   transcript["html_url"],
        "type":  "transcript_html",
    })
    hearing["docs"]          = docs
    hearing["transcript_url"] = transcript["pdf_url"]
    hearing["govinfo_package"] = transcript["package_id"]


def apply_member_data_to_hearings(
    hearings: List[Dict],
    members_cache: Dict,
) -> List[Dict]:
    """
    For each hearing, attach the relevant committee member roster.
    Adds a 'committee_members' field with leadership highlighted.
    """
    for h in hearings:
        committee_raw = h.get("committee", "")
        canonical     = resolve_canonical_committee(committee_raw)
        if not canonical or canonical not in members_cache:
            continue

        member_data = members_cache[canonical]
        members     = member_data.get("members", [])
        if not members:
            continue

        # Sort: leadership first, then by party (R/D), then name
        def sort_key(m):
            is_lead = 1 if m.get("is_leadership") else 0
            party   = 0 if m.get("party", "").upper() == "R" else 1
            return (-is_lead, party, m.get("name", ""))

        h["committee_members"]       = sorted(members, key=sort_key)
        h["committee_members_count"] = len(members)
        h["committee_fetched_utc"]   = member_data.get("fetched_utc", "")

    return hearings


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------
def run_enricher(
    committee_only: bool = False,
    govinfo_only:   bool = False,
    force:          bool = False,
) -> None:
    print("=" * 60)
    print("ClearPath Hearings Enricher")
    print("=" * 60)

    hearings = load_json(HEARINGS_PATH, [])
    if not hearings:
        print("No hearings found — run scout.py first.")
        return

    print(f"Loaded {len(hearings)} hearings from {HEARINGS_PATH}\n")

    enrichment_cache = load_json(ENRICHMENT_PATH, {})
    members_cache    = load_json(MEMBERS_PATH, {})

    # -----------------------------------------------------------------------
    # Step 1: Congress.gov API — committee member rosters
    # -----------------------------------------------------------------------
    if not govinfo_only:
        if not CONGRESS_API_KEY:
            print("⚠️  Congress.gov API key not set in data/config.json")
        else:
            print("── Congress.gov: Fetching committee member rosters ──")
            cg_client     = CongressCommitteeClient(CONGRESS_API_KEY)
            members_cache = enrich_committee_members(cg_client, force=force)
            hearings      = apply_member_data_to_hearings(hearings, members_cache)
            print()

    # -----------------------------------------------------------------------
    # Step 2: GovInfo — transcript links for past hearings
    # -----------------------------------------------------------------------
    if not committee_only:
        print("── GovInfo: Searching for hearing transcripts ──")
        gi_client = GovInfoClient(GOVINFO_KEY)
        hearings, enrichment_cache = enrich_govinfo_transcripts(
            hearings,
            gi_client,
            enrichment_cache,
            force=force,
        )
        save_json(ENRICHMENT_PATH, enrichment_cache)
        print()

    # -----------------------------------------------------------------------
    # Save enriched hearings back to seed file
    # -----------------------------------------------------------------------
    # Serialize dates for JSON
    serial = []
    for h in hearings:
        hh = dict(h)
        if isinstance(hh.get("date"), date):
            hh["date"] = hh["date"].strftime("%Y-%m-%d")
        serial.append(hh)

    save_json(HEARINGS_PATH, serial)
    print(f"✅ Enriched hearings saved → {HEARINGS_PATH}")
    print(f"   Members cache          → {MEMBERS_PATH}")
    print(f"   GovInfo cache          → {ENRICHMENT_PATH}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enrich hearings with ProPublica + GovInfo data")
    parser.add_argument("--committee-only", action="store_true", help="Only fetch committee member data")
    parser.add_argument("--govinfo-only",   action="store_true", help="Only search for transcripts")
    parser.add_argument("--force",          action="store_true", help="Re-fetch even if cached")
    args = parser.parse_args()

    run_enricher(
        committee_only=args.committee_only,
        govinfo_only=args.govinfo_only,
        force=args.force,
    )
