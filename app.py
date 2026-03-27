import streamlit as st
from datetime import date, datetime, timedelta
from collections import defaultdict
import textwrap
import json
import csv
from io import StringIO
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

CONFIG_PATH = DATA_DIR / "committees_focus.json"
HEARINGS_PATH = DATA_DIR / "hearings_seed.json"

SOURCES_PATH = DATA_DIR / "committee_sources.json"

def load_sources():
    if SOURCES_PATH.exists():
        return json.loads(SOURCES_PATH.read_text())
    return {}

sources = load_sources()

# ======================
# Files: config + data
# ======================
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

CONFIG_PATH = DATA_DIR / "committees_focus.json"
HEARINGS_PATH = DATA_DIR / "hearings_seed.json"

def load_config():
    default = {"house": [], "senate": [], "topic_keywords": []}
    if CONFIG_PATH.exists():
        try:
            return {**default, **json.loads(CONFIG_PATH.read_text())}
        except Exception:
            return default
    return default
import re
import hashlib

def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def compute_hearing_id(h: dict) -> str:
    # stable across reruns as long as these fields match
    base = "|".join([
        norm_text(h.get("committee")),
        norm_text(h.get("subcommittee")),
        (h.get("date").strftime("%Y-%m-%d") if isinstance(h.get("date"), date) else norm_text(h.get("date"))),
        norm_text(h.get("time", "")),
        norm_text(h.get("topic")),
        norm_text(h.get("location", "")),
    ]).lower()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]

def strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities. Returns empty string for generic fallback links."""
    if not text:
        return ""
    clean = re.sub(r'<[^>]+>', '', text).strip()
    # Collapse whitespace
    clean = re.sub(r'\s+', ' ', clean).strip()
    # If the only content is a generic search/link label, treat as empty
    if clean.lower() in {"search congress.gov", "search", "view details", ""}:
        return ""
    return clean

def normalize_hearing(h: dict) -> dict:
    hh = dict(h)
    hh["committee"] = norm_text(hh.get("committee"))
    hh["subcommittee"] = norm_text(hh.get("subcommittee"))
    hh["topic"] = norm_text(hh.get("topic"))
    hh["time"] = norm_text(hh.get("time", "TBD")) or "TBD"
    hh["location"] = norm_text(hh.get("location", "TBD")) or "TBD"
    hh["status"] = norm_text(hh.get("status", "Scheduled")) or "Scheduled"
    hh["url"] = norm_text(hh.get("url", ""))
    hh["why"] = strip_html(hh.get("why") or "")
    hh["tags"] = sorted({t.strip() for t in (hh.get("tags") or []) if t and t.strip()})
    hh["witnesses"] = hh.get("witnesses") or ["TBD"]
    hh["sources"] = hh.get("sources") or []
    hh["docs"] = hh.get("docs") or []
    hh["bills"] = hh.get("bills") or []
    hh["hearing_id"] = hh.get("hearing_id") or compute_hearing_id(hh)
    return hh

_JUNK_TOPICS = {"all events", "committee activity", "hearings/votes", "calendar"}

def _completeness_score(h: dict) -> int:
    """Higher = more complete/useful entry. Used to pick the best duplicate."""
    score = 0
    if h.get("time") and h["time"] != "TBD":      score += 4
    if h.get("location") and h["location"] != "TBD": score += 3
    witnesses = [w for w in (h.get("witnesses") or []) if w and w != "TBD"]
    score += min(len(witnesses), 5)                # up to +5 for witnesses
    if h.get("why"):                               score += 2
    if h.get("tags"):                              score += len(h["tags"])
    topic = (h.get("topic") or "").strip().lower()
    if topic in _JUNK_TOPICS or not topic:         score -= 10
    # Prefer longer committee names (subcommittee > parent committee)
    score += min(len(h.get("committee", "")), 20) // 10
    return score

def _parent_committee(name: str) -> str:
    """Normalize and strip 'Subcommittee on ...' suffix to get a canonical parent name."""
    name = (norm_text(name) or "").replace("&", "and").replace(",", "").lower()
    for marker in (" subcommittee on ", " subcommittee for ", " subcommittee to "):
        idx = name.find(marker)
        if idx != -1:
            return name[:idx].strip()
    return name

_STOP_WORDS = {
    "to", "the", "a", "an", "and", "or", "of", "in", "for", "on", "with", "by",
    "is", "are", "was", "were", "at", "from", "that", "this", "be", "as", "its",
    # Congressional boilerplate that appears in nearly every hearing title
    "hearing", "hearings", "examine", "examining", "consider", "consideration",
    "committee", "subcommittee", "joint", "senate", "house", "congress",
    "full", "markup", "meeting", "session", "executive", "business",
}

def _topic_overlap(a: str, b: str) -> float:
    """Meaningful-word overlap between two topic strings (ignores boilerplate)."""
    def meaningful(text):
        words = re.sub(r'[^a-z0-9 ]', '', (text or "").lower()).split()
        return {w for w in words if w not in _STOP_WORDS and len(w) > 2}
    wa, wb = meaningful(a), meaningful(b)
    if not wa or not wb:
        return 0.0
    shorter = wa if len(wa) <= len(wb) else wb
    return len(wa & wb) / len(shorter)

def dedupe_hearings(items: list[dict]) -> list[dict]:
    # Pass 1 — dedup by hearing_id (content hash)
    seen_id = {}
    for h in items:
        hh = normalize_hearing(h)
        hid = hh["hearing_id"]
        sc = _completeness_score(hh)
        if hid not in seen_id or sc > seen_id[hid][0]:
            seen_id[hid] = (sc, hh)
    after_pass1 = list(seen_id.values())

    # Pass 2 — dedup by exact congress.gov committee-meeting URL
    seen_url = {}
    no_url = []
    for sc, hh in after_pass1:
        url = (hh.get("url") or "").strip()
        if "congress.gov/committee-meeting" in url:
            if url not in seen_url or sc > seen_url[url][0]:
                seen_url[url] = (sc, hh)
        else:
            no_url.append((sc, hh))
    after_pass2 = list(seen_url.values()) + no_url

    # Pass 3 — cross-source dedup: same date + parent committee + overlapping topic
    # Handles cases where congress.gov API and committee website both scraped the same event
    by_date_committee: dict = {}
    for sc, hh in after_pass2:
        d    = str(hh.get("date", ""))
        pcom = _parent_committee(hh.get("committee", ""))
        key  = (d, pcom)
        by_date_committee.setdefault(key, []).append((sc, hh))

    result = []
    for key, group in by_date_committee.items():
        if len(group) == 1:
            result.append(group[0][1])
            continue
        # Check each pair for topic overlap ≥ 40% → treat as same event
        merged: list[tuple] = []
        used = [False] * len(group)
        for i, (sc_i, hi) in enumerate(group):
            if used[i]:
                continue
            best_sc, best_h = sc_i, dict(hi)  # copy so we can annotate
            absorbed_urls = []
            for j, (sc_j, hj) in enumerate(group):
                if i == j or used[j]:
                    continue
                if _topic_overlap(hi.get("topic",""), hj.get("topic","")) >= 0.60:
                    used[j] = True
                    absorbed_urls.append(hj.get("url",""))
                    if sc_j > best_sc:
                        best_sc = sc_j
                        absorbed_urls.append(best_h.get("url",""))
                        best_h = dict(hj)

            # Rescue the committee website URL from a merged entry if we lost it
            # Prefer *.senate.gov / *.house.gov over congress.gov for the display link
            all_urls = [best_h.get("url","")] + absorbed_urls
            committee_url = next(
                (u for u in all_urls
                 if u and "congress.gov" not in u
                 and any(d in u for d in [".senate.gov", ".house.gov", "energycommerce", "transportation.house"])),
                None
            )
            congress_url = next(
                (u for u in all_urls if u and "congress.gov/committee-meeting" in u),
                None
            )
            if committee_url:
                best_h["committee_url"] = committee_url
            if congress_url:
                best_h["congress_url"] = congress_url
            # Primary link: committee website if we have it, else congress.gov
            best_h["url"] = committee_url or best_h.get("url","")

            merged.append(best_h)
        result.extend(merged)

    return result

def load_hearings():
    if HEARINGS_PATH.exists():
        raw = json.loads(HEARINGS_PATH.read_text())
        for h in raw:
            if isinstance(h.get("date"), str) and h["date"]:
                h["date"] = datetime.strptime(h["date"], "%Y-%m-%d").date()
            elif not h.get("date"):
                h["date"] = None
        return dedupe_hearings(raw)
    return []


def save_hearings(items):
    items = dedupe_hearings(items)
    serial = []
    for h in items:
        hh = dict(h)
        if isinstance(hh.get("date"), date):
            hh["date"] = hh["date"].strftime("%Y-%m-%d")
        serial.append(hh)
    HEARINGS_PATH.write_text(json.dumps(serial, indent=2))

def update_hearing_field(hearing_id: str, field: str, value):
    """Update a specific field for a hearing and save."""
    raw = json.loads(HEARINGS_PATH.read_text()) if HEARINGS_PATH.exists() else []
    for h in raw:
        if h.get("hearing_id") == hearing_id:
            h[field] = value
            break
    HEARINGS_PATH.write_text(json.dumps(raw, indent=2))


config = load_config()
CLEARPATH_FOCUS = set(config.get("house", []) + config.get("senate", []))
TOPIC_KEYWORDS = [k.lower() for k in config.get("topic_keywords", [])]
hearings = load_hearings()
if not hearings:
    st.warning("No hearings found in data/hearings_seed.json yet. Add one from the sidebar.")
def apply_keyword_tags(h):
    topic = (h.get("topic") or "").lower()
    hits = []
    for kw in TOPIC_KEYWORDS:
        if kw in topic:
            hits.append(kw.title())
    h.setdefault("tags", [])
    for t in hits:
        if t not in h["tags"]:
            h["tags"].append(t)

for h in hearings:
    apply_keyword_tags(h)

# ======================
# Sidebar: Filters (consolidated single section)
# ======================
st.sidebar.header("Filters")

# 1) Date drives Selected Date + This Week
selected_date = st.sidebar.date_input("View date", value=date.today())

# 2) Committee filter
TRACKED_COMMITTEES = config.get("house", []) + config.get("senate", [])

# Add "(any)" option
committee_options = ["(any)"] + TRACKED_COMMITTEES
selected_committees_raw = st.sidebar.multiselect(
    "Committee",
    options=committee_options,
    default=committee_options[:1] if committee_options else []
)

# If "(any)" is selected, show all committees (no filter applied)
if "(any)" in selected_committees_raw:
    selected_committees = []  # empty = no committee filter, show everything
else:
    selected_committees = [c for c in selected_committees_raw if c != "(any)"]

# 3) Subcommittee filter (dynamic)
subcommittee_options = []
for c in selected_committees:
    for s in sources.get(c, {}).get("subcommittees", []):
        if s.get("name"):
            subcommittee_options.append(s["name"])
subcommittee_options = sorted(set(subcommittee_options))

selected_subcommittee = st.sidebar.selectbox(
    "Subcommittee",
    options=["(any)"] + subcommittee_options
)

# 4) Tags filter
all_tags = sorted({t for h in hearings for t in h.get("tags", [])})
selected_tags = st.sidebar.multiselect("Tags", options=all_tags, default=[])

# 5) Search
search_text = st.sidebar.text_input("Search", value="").strip().lower()

# Optional simple toggles
tracked_only = st.sidebar.toggle("Tracked committees only", value=False)
priority_only = st.sidebar.toggle("Priority keywords only", value=False)

# Set FOCUS_COMMITTEES for use throughout the app
FOCUS_COMMITTEES = TRACKED_COMMITTEES
FOCUS_SET = set(FOCUS_COMMITTEES)

st.sidebar.markdown("---")

# ======================
# Sidebar: Add hearing (DATA ENTRY)
# ======================
with st.sidebar.expander("➕ Add a hearing", expanded=False):

    # Keep committee in session_state so subcommittee can react
    if "add_committee" not in st.session_state:
        st.session_state.add_committee = FOCUS_COMMITTEES[0] if FOCUS_COMMITTEES else ""

    def subcommittee_options(committee: str) -> list[str]:
        if committee in sources:
            subs = sources[committee].get("subcommittees", []) or []
            names = [s.get("name") for s in subs if s.get("name")]
            return ["(none)"] + names
        return ["(none)"]

    # Build current options
    current_sub_opts = subcommittee_options(st.session_state.add_committee)

    # If the stored subcommittee is no longer valid, reset it
    if "add_subcommittee" in st.session_state and st.session_state.add_subcommittee not in current_sub_opts:
        st.session_state.add_subcommittee = "(none)"

    with st.form("add_hearing_form", clear_on_submit=True):
        new_date = st.date_input("Hearing date", value=date.today(), key="add_date")

        new_committee = st.selectbox(
            "Committee (tracked)",
            options=FOCUS_COMMITTEES,
            key="add_committee"
        )

        # recompute options based on the selected committee
        sub_opts = subcommittee_options(new_committee)

        new_subcommittee = st.selectbox(
            "Subcommittee (optional)",
            options=sub_opts,
            key="add_subcommittee"
        )

        new_topic = st.text_input("Topic", key="add_topic")
        new_time = st.text_input("Time (e.g., 10:00 AM)", value="TBD", key="add_time")
        new_location = st.text_input("Location", value="TBD", key="add_location")

        new_status = st.selectbox(
            "Status",
            ["Scheduled", "Hearing noticed", "Markup", "Roundtable", "Business meeting", "Added manually"],
            index=0,
            key="add_status"
        )

        new_why = st.text_area("Why it matters", value="", key="add_why")
        new_tags_raw = st.text_input("Tags (comma-separated)", value="", key="add_tags")

        # Auto-fill URL from sources
        default_url = ""
        if new_committee in sources:
            default_url = (
                sources[new_committee].get("calendar")
                or sources[new_committee].get("hearings")
                or sources[new_committee].get("homepage")
                or ""
            )

        new_url = st.text_input("Committee/notice URL", value=default_url, key="add_url")

        submitted = st.form_submit_button("Add hearing")

    if submitted and new_topic.strip():
        subcommittee_val = "" if new_subcommittee == "(none)" else new_subcommittee
        manual_tags = [t.strip() for t in new_tags_raw.split(",") if t.strip()]

        hearings.append({
            "date": new_date,
            "committee": new_committee,
            "subcommittee": subcommittee_val,
            "topic": new_topic.strip(),
            "time": new_time.strip() or "TBD",
            "location": new_location.strip() or "TBD",
            "why": new_why.strip(),
            "tags": manual_tags,
            "url": new_url.strip(),
            "status": new_status,
            "stream": None,
            "witnesses": ["TBD"],
            "sources": [{"label": "Manual entry", "url": new_url.strip()}] if new_url.strip() else [],
            "docs": [],
            "bills": []
        })

        save_hearings(hearings)
        st.success("Saved to data/hearings_seed.json")
        st.rerun()

# ======================
# Sidebar: Bulk Import from CSV
# ======================
with st.sidebar.expander("📥 Bulk import from CSV", expanded=False):
    st.markdown("""
Upload a CSV file with columns:
- Date (YYYY-MM-DD)
- Committee
- Subcommittee (optional)
- Topic
- Time
- Location
- Why it matters
- Tags (comma-separated)
- URL
    """)

    uploaded_file = st.file_uploader("Choose CSV file", type=['csv'], key="csv_uploader")

    if uploaded_file is not None:
        try:
            csv_content = uploaded_file.read().decode('utf-8')
            csv_reader = csv.DictReader(StringIO(csv_content))

            new_hearings = []
            for row in csv_reader:
                hearing_date = datetime.strptime(row['Date'].strip(), "%Y-%m-%d").date()
                tags = [t.strip() for t in row.get('Tags', '').split(',') if t.strip()]

                new_hearings.append({
                    "date": hearing_date,
                    "committee": row['Committee'].strip(),
                    "subcommittee": row.get('Subcommittee', '').strip(),
                    "topic": row['Topic'].strip(),
                    "time": row.get('Time', 'TBD').strip() or "TBD",
                    "location": row.get('Location', 'TBD').strip() or "TBD",
                    "why": row.get('Why it matters', '').strip(),
                    "tags": tags,
                    "url": row.get('URL', '').strip(),
                    "status": "Scheduled",
                    "stream": None,
                    "witnesses": ["TBD"],
                    "sources": [{"label": "CSV import", "url": row.get('URL', '').strip()}] if row.get('URL', '').strip() else [],
                    "docs": [],
                    "bills": []
                })

            if new_hearings:
                hearings.extend(new_hearings)
                save_hearings(hearings)
                st.success(f"✅ Imported {len(new_hearings)} hearings!")
                st.rerun()
        except Exception as e:
            st.error(f"Error importing CSV: {e}")

st.sidebar.markdown("---")

# ======================
# ClearPath styling
# ======================
CP_NAVY = "#193D69"
CP_RED = "#9D1C20"
CP_GREY_LIGHT = "#EFEFEF"
CP_BLUE_LIGHT = "#A8CBE5"
CP_RED_LIGHT = "#D8B8BD"
CP_GREY_DARK = "#767676"
TEXT_DARK = "#333333"


# Priority keywords - hearings matching these are marked as priority
PRIORITY_KEYWORDS = set(k.lower() for k in config.get("topic_keywords", []))

def is_priority_hearing(h: dict) -> bool:
    """Check if hearing matches any priority keywords in topic or tags."""
    topic = (h.get("topic") or "").lower()
    tags  = " ".join(t.lower() for t in (h.get("tags") or []))
    text  = topic + " " + tags
    return any(re.search(r'\b' + re.escape(kw) + r'\b', text) for kw in PRIORITY_KEYWORDS)


def normalize_committee_name(name: str) -> str:
    """Normalize committee name for matching (lowercase, & -> and, remove extra spaces)."""
    return name.lower().replace("&", "and").replace(",", "").replace("  ", " ").strip()

def committee_matches(hearing_committee: str, filter_committee: str) -> bool:
    """Check if a hearing's committee matches a filter committee (fuzzy matching).

    Matches if:
    - Exact match (after normalization)
    - Hearing committee starts with filter committee (for subcommittees)
    - Filter committee is contained in hearing committee
    """
    h_norm = normalize_committee_name(hearing_committee)
    f_norm = normalize_committee_name(filter_committee)

    # Exact match
    if h_norm == f_norm:
        return True

    # Hearing is a subcommittee of the filter committee
    # e.g., "House Energy and Commerce Subcommittee on Health" matches "House Energy & Commerce"
    if h_norm.startswith(f_norm.split(" subcommittee")[0]):
        return True

    # Filter name is contained in hearing name
    # Handle cases like "House Science Space and Technology" matching "House Science, Space, & Technology"
    f_words = set(f_norm.split())
    h_words = set(h_norm.split())
    # If most filter words are in the hearing name, it's a match
    common = f_words & h_words
    if len(common) >= len(f_words) - 1 and len(common) >= 3:
        return True

    return False


st.set_page_config(page_title="ClearPath Hearings Dashboard", layout="wide")

st.markdown(
    f"""
    <style>
      .cp-card {{
        background: {CP_GREY_LIGHT};
        padding: 16px;
        border-radius: 10px;
        margin-bottom: 14px;
        box-shadow: 0 1px 0 rgba(0,0,0,0.04);
      }}
      .cp-badge {{
        font-size: 12px;
        padding: 4px 10px;
        border-radius: 999px;
        font-weight: 600;
        display: inline-block;
        white-space: nowrap;
      }}
      .cp-chip {{
        font-size: 12px;
        padding: 3px 10px;
        border-radius: 999px;
        border: 1px solid rgba(0,0,0,0.10);
        display: inline-block;
        margin-right: 6px;
        margin-bottom: 6px;
        background: white;
      }}
      .cp-muted {{
        color: {CP_GREY_DARK};
      }}
      a {{
        text-decoration: none;
      }}
      a:hover {{
        text-decoration: underline;
      }}
    </style>
    """,
    unsafe_allow_html=True
)

# ======================
# Header
# ======================
logo_url = "https://clearpath.org/wp-content/uploads/sites/44/2023/08/clearpath-logo.png"
st.markdown(
    f"""
    <div style="display:flex; align-items:center; gap:20px; margin-bottom:18px;">
        <img src="{logo_url}" width="180">
        <div>
            <h1 style="color:{CP_NAVY}; margin-bottom:4px;">Clean Energy Hearings Dashboard</h1>
            <p style="color:{CP_GREY_DARK}; margin:0;">
                Congressional hearings relevant to clean energy, critical minerals, and permitting
            </p>
        </div>
    </div>
    <hr style="border:none; border-top:2px solid {CP_NAVY}; margin-bottom:22px;">
    """,
    unsafe_allow_html=True
)

# ======================
# Load hearings data
# ======================
hearings = load_hearings()

if not hearings:
    st.sidebar.info("No hearings in database. Use the form below to add hearings, or check data/hearings_templates.json for examples.")



# ======================
# Sidebar: Resources
# ======================
st.sidebar.markdown("---")
st.sidebar.subheader("🔗 Resources")

# Link to Congress.gov weekly schedule
week_url = f"https://www.congress.gov/committee-schedule/weekly/{date.today().strftime('%Y/%m/%d')}"
st.sidebar.markdown(f"**📅 [Congress.gov Weekly Schedule]({week_url})**")
st.sidebar.caption("Check this weekly for upcoming hearings")

# Committee hearing pages
with st.sidebar.expander("Committee Hearing Pages"):
    committee_links = {
        "House Energy & Commerce": "https://energycommerce.house.gov/calendar/",
        "Senate Energy & Natural Resources": "https://www.energy.senate.gov/hearings",
        "House Natural Resources": "https://naturalresources.house.gov/calendar/",
        "Senate Environment & Public Works": "https://www.epw.senate.gov/public/index.cfm/hearings",
        "House Transportation & Infrastructure": "https://transportation.house.gov/calendar/",
        "House Ways & Means": "https://waysandmeans.house.gov/hearings/",
        "House Science, Space, & Technology": "https://science.house.gov/hearings",
        "Senate Agriculture, Nutrition, & Forestry": "https://www.agriculture.senate.gov/hearings",
        "Senate Foreign Relations": "https://www.foreign.senate.gov/hearings/",
        "Senate Commerce, Science, & Transportation": "https://www.commerce.senate.gov/hearings",
    }

    for committee, url in committee_links.items():
        # Use shorter names for display
        display_name = committee.replace("House ", "H: ").replace("Senate ", "S: ")
        st.sidebar.markdown(f"[{display_name}]({url})")

# Helper script info
with st.sidebar.expander("💡 How to use"):
    st.sidebar.markdown("""
**Auto-refresh:**
The scout automatically checks Congress.gov for new hearings.
- Click "Run Scout Now" in the Updates tab
- Or set up daily auto-refresh (see below)

**Daily auto-refresh (cron):**
```bash
# Edit crontab
crontab -e

# Add this line (runs at 7am daily):
0 7 * * * cd ~/Desktop/hearings-dashboard && python3 scout.py
```

**Manual workflow:**
1. Check the Updates tab for new/changed hearings
2. Review priority hearings in the This Week tab
3. Add notes via the sidebar form
""".format(week_url))

# ======================
# Helpers
# ======================
def within_week(d: date, anchor: date) -> bool:
    start = anchor - timedelta(days=anchor.weekday())
    end = start + timedelta(days=7)
    return start <= d < end

def parse_time_for_sort(time_str: str) -> tuple:
    """Parse time string for proper sorting (morning first).

    Returns a tuple (hour_24, minute) for sorting, with TBD/unknown times at the end.
    """
    if not time_str or time_str.upper() == "TBD":
        return (99, 0)  # Sort TBD times at the end

    time_str = time_str.strip().upper()

    # Try common time formats
    import re

    # Match patterns like "10:00 AM", "3:30 PM", "10:00AM", "3:30PM"
    match = re.match(r'(\d{1,2}):?(\d{2})?\s*(AM|PM)?', time_str)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        period = match.group(3)

        # Convert to 24-hour for sorting
        if period == "PM" and hour != 12:
            hour += 12
        elif period == "AM" and hour == 12:
            hour = 0

        return (hour, minute)

    # Fallback: try to extract any numbers
    nums = re.findall(r'\d+', time_str)
    if nums:
        hour = int(nums[0])
        minute = int(nums[1]) if len(nums) > 1 else 0
        # Assume PM if hour is less than 8 (most hearings are 9am-6pm)
        if hour < 8 and "AM" not in time_str:
            hour += 12
        return (hour, minute)

    return (99, 0)  # Unknown format, sort at end

def build_ics(hearing: dict) -> str:
    dt = hearing["date"].strftime("%Y%m%d")
    title = f'{hearing["committee"]}: {hearing["topic"]}'
    desc = strip_html(hearing.get("why") or "").replace("\n", " ")
    url = hearing.get("url") or ""
    uid = f'{dt}-{abs(hash(title))}@clearpath'
    return "\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ClearPath//Hearings Dashboard//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;VALUE=DATE:{dt}",
        f"SUMMARY:{title}",
        f"DESCRIPTION:{desc} {url}".strip(),
        f"URL:{url}",
        "END:VEVENT",
        "END:VCALENDAR",
    ])

def export_to_csv(hearings_list: list) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Date", "Time", "Location", "Committee", "Topic",
        "Why it Matters", "Priority", "Status", "Tags",
        "Witnesses", "Bills", "URL"
    ])
    for h in sorted(hearings_list, key=lambda x: (x.get("date") or date.max, x.get("committee",""))):
        d = h.get("date")
        writer.writerow([
            d.strftime("%Y-%m-%d") if isinstance(d, date) else "",
            h.get("time", "TBD"),
            h.get("location", "TBD"),
            h.get("committee", ""),
            h.get("topic", ""),
            h.get("why", ""),
            "Yes" if is_priority_hearing(h) else "No",
            h.get("status", ""),
            ", ".join(h.get("tags", [])),
            ", ".join(h.get("witnesses", ["TBD"])),
            ", ".join(h.get("bills", [])),
            h.get("url", ""),
        ])
    return output.getvalue()

def render_card(hearing: dict, idx: int, tab_prefix: str = ""):
    import urllib.parse
    is_priority = is_priority_hearing(hearing)
    status = hearing.get("status", "Scheduled")
    is_rescheduled = status.lower() == "rescheduled"

    # Set colors based on priority and status
    if is_priority:
        border_color = CP_RED
        badge_bg = CP_RED_LIGHT
        badge_text = CP_RED
        badge_label = "Priority"
    elif is_rescheduled:
        border_color = "#D4A017"  # Gold/yellow
        badge_bg = "#FFF3CD"  # Light yellow
        badge_text = "#856404"  # Dark yellow/brown
        badge_label = "Rescheduled"
    else:
        border_color = CP_NAVY
        badge_bg = CP_BLUE_LIGHT
        badge_text = CP_NAVY
        badge_label = status or "Hearing"

    tags_html = "".join([f"<span class='cp-chip'>{t}</span>" for t in hearing.get("tags", [])])

    sources = hearing.get("sources", []) or []
    docs = hearing.get("docs", []) or []
    witnesses = hearing.get("witnesses", ["TBD"]) or ["TBD"]
    bills = hearing.get("bills", []) or []
    location = hearing.get("location", "TBD")

    card_html = f"""
    <div class="cp-card" style="border-left: 7px solid {border_color};">
      <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:14px;">
        <div>
          <div style="font-size:14px; font-weight:700; color:{CP_NAVY}; margin-bottom:4px;">
            {hearing["committee"]}
          </div>
          <div style="font-size:16px; font-weight:700; color:{TEXT_DARK};">
            {hearing["topic"]}
          </div>
          <div class="cp-muted" style="margin-top:4px;">
            <strong>Date:</strong> {hearing["date"].strftime("%b %d, %Y")} &nbsp; | &nbsp;
            <strong>Time:</strong> {hearing.get("time","TBD")} &nbsp; | &nbsp;
            <strong>Location:</strong> {location}
          </div>
        </div>
        <span class="cp-badge" style="background:{badge_bg}; color:{badge_text};">
          {badge_label}
        </span>
      </div>

      {f'<div style="margin-top:10px; color:{TEXT_DARK};"><strong>Why it matters:</strong> {strip_html(hearing.get("why",""))}</div>' if strip_html(hearing.get("why","")) else ""}

      <div style="margin-top:10px;">
        {tags_html}
      </div>

      <div style="margin-top:10px;" class="cp-muted">
        <strong>Witnesses:</strong> {", ".join(witnesses)}
        {"<br><strong>Bills:</strong> " + ", ".join(bills) if bills else ""}
      </div>

      <div style="margin-top:12px; display: flex; gap: 16px; flex-wrap: wrap; align-items:center;">
        {f'<a href="{hearing.get("url","")}" target="_blank" style="color:{CP_NAVY}; font-weight:700;">View hearing details →</a>' if hearing.get("url") else ""}
        {f'<a href="{hearing.get("congress_url","")}" target="_blank" style="color:{CP_GREY_DARK}; font-weight:500;">Congress.gov record</a>' if hearing.get("congress_url") and hearing.get("congress_url") != hearing.get("url") else ""}
        {f'<a href="{hearing.get("transcript_url","")}" target="_blank" style="color:#28a745; font-weight:600;">📄 Transcript available</a>' if hearing.get("transcript_url") else ""}
      </div>
    </div>
    """
    st.markdown(textwrap.dedent(card_html), unsafe_allow_html=True)

    with st.expander("Details, sources, and documents"):
        colA, colB = st.columns([2, 1])

        with colA:
            # --- Transcript links (prominent) ---
            transcript_docs = [d for d in docs if d.get("type") in ("transcript", "transcript_html")]
            other_docs = [d for d in docs if d.get("type") not in ("transcript", "transcript_html")]

            if transcript_docs:
                st.markdown("**📄 Transcript**")
                for d in transcript_docs:
                    st.markdown(f"- [{d['label']}]({d['url']})")

            if sources:
                st.markdown("**Sources**")
                for s in sources:
                    st.markdown(f"- [{s['label']}]({s['url']})")
            if other_docs:
                st.markdown("**Documents**")
                for d in other_docs:
                    st.markdown(f"- [{d['label']}]({d['url']})")
            if hearing.get("stream"):
                st.markdown(f"**Livestream:** {hearing['stream']}")

        # --- Committee Members ---
        members = hearing.get("committee_members") or []
        if members:
            st.markdown("---")
            st.markdown("**🏛️ Committee Members**")

            # Separate leadership from regular members
            leaders = [m for m in members if m.get("is_leadership") or m.get("role", "").lower() in ("chair", "ranking member", "vice chair")]
            regulars = [m for m in members if m not in leaders]

            # Color by party
            def party_color(party: str) -> str:
                p = (party or "").upper()
                if p == "R":
                    return "#CC3333"
                elif p == "D":
                    return "#1155AA"
                return CP_GREY_DARK

            def party_label(party: str) -> str:
                p = (party or "").upper()
                if p == "R": return "R"
                if p == "D": return "D"
                return party or "?"

            if leaders:
                leader_html = ""
                for m in leaders:
                    pc = party_color(m.get("party", ""))
                    pl = party_label(m.get("party", ""))
                    role = m.get("role", "")
                    name = m.get("name", "")
                    state = m.get("state", "")
                    leader_html += f"""
                    <span style="display:inline-flex;align-items:center;gap:6px;margin-right:14px;margin-bottom:6px;">
                      <span style="background:{pc};color:white;font-size:11px;font-weight:700;padding:2px 7px;border-radius:4px;">{pl}</span>
                      <strong>{name}</strong>
                      {f'<span style="color:{CP_GREY_DARK};font-size:12px;">({state}) — {role}</span>' if role else f'<span style="color:{CP_GREY_DARK};font-size:12px;">({state})</span>'}
                    </span>"""
                st.markdown(leader_html, unsafe_allow_html=True)

            if regulars:
                # Show members in a compact two-column grid
                cols = st.columns(2)
                for i, m in enumerate(regulars[:20]):  # cap at 20 for readability
                    pc = party_color(m.get("party", ""))
                    pl = party_label(m.get("party", ""))
                    name = m.get("name", "")
                    state = m.get("state", "")
                    with cols[i % 2]:
                        st.markdown(
                            f'<span style="background:{pc};color:white;font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px;">{pl}</span> '
                            f'{name} <span style="color:{CP_GREY_DARK};font-size:12px;">({state})</span>',
                            unsafe_allow_html=True
                        )
                if len(regulars) > 20:
                    st.caption(f"+ {len(regulars) - 20} more members")

        with colB:
            ics = build_ics(hearing)
            st.download_button(
                label="Add to calendar (.ics)",
                data=ics,
                file_name=f"clearpath_hearing_{hearing['date'].strftime('%Y%m%d')}_{idx}.ics",
                mime="text/calendar",
                use_container_width=True,
                key=f"ics_{tab_prefix}_{idx}"
            )

        # Notes section
        st.markdown("---")
        st.markdown("**📝 Notes**")
        hearing_id = hearing.get("hearing_id", "")
        notes_key = f"notes_{tab_prefix}_{hearing_id}"
        current_notes = hearing.get("notes", "")

        new_notes = st.text_area(
            "Add internal notes about this hearing",
            value=current_notes,
            key=notes_key,
            height=100,
            label_visibility="collapsed"
        )

        if st.button("Save notes", key=f"save_notes_{tab_prefix}_{hearing_id}"):
            if hearing_id:
                update_hearing_field(hearing_id, "notes", new_notes)
                st.success("Notes saved!")
                st.rerun()



# Duplicate filters section removed - using consolidated section above

# ======================
# Filter logic
# ======================

# FOCUS_COMMITTEES and FOCUS_SET already defined in sidebar section above
ALLOWLIST = FOCUS_SET

def matches(h: dict) -> bool:
    if not isinstance(h, dict):
        return False

    committee = h.get("committee", "")
    subcommittee = h.get("subcommittee", "")
    tags = h.get("tags", []) or []

    # (Optional) Only tracked committees (fuzzy match)
    if tracked_only and ALLOWLIST:
        if not any(committee_matches(committee, allowed) for allowed in ALLOWLIST):
            return False

    # Committee multiselect (fuzzy match)
    if selected_committees:
        if not any(committee_matches(committee, sel) for sel in selected_committees):
            return False

    # Subcommittee filter
    if selected_subcommittee and selected_subcommittee != "(any)":
        if subcommittee != selected_subcommittee:
            return False

    # (Optional) Priority-only means: matches priority keywords
    if priority_only and not is_priority_hearing(h):
        return False

    # Tag filter
    if selected_tags and not any(t in tags for t in selected_tags):
        return False

    # Search filter
    if search_text:
        blob = " ".join([
            committee,
            subcommittee,
            h.get("topic", ""),
            " ".join(tags),
            h.get("location", ""),
        ]).lower()
        if search_text not in blob:
            return False

    return True


filtered = [h for h in hearings if matches(h)]

# Group by date safely
by_date = defaultdict(list)
for h in filtered:
    d = h.get("date")
    if isinstance(d, date):
        by_date[d].append(h)

today_list = by_date.get(selected_date, [])
week_list = [h for h in filtered if isinstance(h.get("date"), date) and within_week(h["date"], selected_date)]

# ======================
# KPI Row
# ======================
c1, c2, c3, c4 = st.columns(4)
c1.metric("Hearings (selected date)", str(len(today_list)))
c2.metric("Hearings (this week)", str(len(week_list)))
c3.metric("Priority (this week)", str(sum(1 for h in week_list if is_priority_hearing(h))))
c4.metric("Committees tracked", str(len(FOCUS_SET)))

# ======================
# Tabs
# ======================
tab_today, tab_week, tab_past, tab_table, tab_updates = st.tabs(["📍 Selected date", "🗓️ This week", "📜 Past Hearings", "📊 Table view", "🧭 Updates"])


with tab_today:
    st.subheader(f"📍 Hearings on {selected_date.strftime('%b %d, %Y')}")
    if not today_list:
        # Check if there are ANY hearings for this date before filtering
        all_on_date = [h for h in hearings if isinstance(h.get("date"), date) and h.get("date") == selected_date]

        if not all_on_date:
            st.info(f"No hearings in database for {selected_date.strftime('%b %d, %Y')}. Use the sidebar to add hearings for this date.")
        else:
            st.info("No hearings match your current filters on this date. Try adjusting the filters above.")
    else:
        for i, hearing in enumerate(sorted(today_list, key=lambda x: (parse_time_for_sort(x.get("time", "")), x.get("committee","")))):
            render_card(hearing, i, "today")

with tab_week:
    st.subheader("🗓️ Hearings this week")
    if not week_list:
        st.info("No hearings match your filters this week.")
    else:
        # Weekly summary
        priority_hearings = [h for h in week_list if is_priority_hearing(h)]
        days = sorted(set(h["date"] for h in week_list if h.get("date")))

        # Collect top tags this week
        tag_counts = {}
        for h in week_list:
            for t in h.get("tags", []):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:5]

        # Summary box
        week_start = days[0].strftime("%b %d") if days else ""
        week_end = days[-1].strftime("%b %d") if days else ""

        st.markdown(f"""
        <div style="background: linear-gradient(135deg, #193D69 0%, #2a5a8c 100%); padding: 20px; border-radius: 10px; margin-bottom: 20px; color: white;">
            <h3 style="margin: 0 0 12px 0; color: white;">Week of {week_start} - {week_end}</h3>
            <div style="display: flex; gap: 30px; flex-wrap: wrap;">
                <div>
                    <div style="font-size: 28px; font-weight: bold;">{len(week_list)}</div>
                    <div style="font-size: 14px; opacity: 0.9;">Total Hearings</div>
                </div>
                <div>
                    <div style="font-size: 28px; font-weight: bold; color: #ffcc00;">{len(priority_hearings)}</div>
                    <div style="font-size: 14px; opacity: 0.9;">Priority Hearings</div>
                </div>
                <div>
                    <div style="font-size: 28px; font-weight: bold;">{len(days)}</div>
                    <div style="font-size: 14px; opacity: 0.9;">Active Days</div>
                </div>
            </div>
            {"<div style='margin-top: 12px; font-size: 13px;'><strong>Top topics:</strong> " + ", ".join(t[0] for t in top_tags) + "</div>" if top_tags else ""}
        </div>
        """, unsafe_allow_html=True)

        # Priority hearings callout
        if priority_hearings:
            with st.expander(f"⭐ {len(priority_hearings)} Priority Hearings This Week", expanded=True):
                for h in sorted(priority_hearings, key=lambda x: (x.get("date") or date.max, parse_time_for_sort(x.get("time", "")))):
                    d = h.get("date")
                    date_str = d.strftime("%a %b %d") if d else "TBD"
                    st.markdown(f"- **{date_str}** | {h.get('committee', '')} | *{h.get('topic', '')}*")

        st.markdown("---")

        # Daily breakdown
        for day_idx, d in enumerate(days):
            st.markdown(f"### {d.strftime('%A, %b %d')}")
            day_hearings = sorted(
                [h for h in week_list if h["date"] == d],
                key=lambda x: (parse_time_for_sort(x.get("time", "")), x.get("committee",""))
            )
            for i, hearing in enumerate(day_hearings):
                render_card(hearing, i, f"week_{day_idx}")

SUMMARIES_PATH = DATA_DIR / "summaries.json"

def load_summaries():
    if SUMMARIES_PATH.exists():
        return json.loads(SUMMARIES_PATH.read_text())
    return {}

with tab_past:
    st.subheader("📜 Past Priority Hearings")
    st.caption("Priority hearings that have already occurred - potential candidates for transcript analysis")

    # Load summaries
    summaries = load_summaries()

    # Get past priority hearings (date < today, is_priority)
    today = date.today()
    past_priority = [
        h for h in filtered
        if isinstance(h.get("date"), date)
        and h["date"] < today
        and is_priority_hearing(h)
    ]

    # Sort by most recent first
    past_priority = sorted(past_priority, key=lambda x: x.get("date") or date.min, reverse=True)

    if not past_priority:
        st.info("No past priority hearings found. Priority hearings that have already occurred will appear here.")
    else:
        # Count summaries
        summarized_count = sum(1 for h in past_priority if summaries.get(h.get("hearing_id", ""), {}).get("status") == "success")
        pending_count = len(past_priority) - summarized_count
        # Summary stats
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Past Priority Hearings", len(past_priority))

        # Get unique committees
        past_committees = set(h.get("committee", "") for h in past_priority)
        col2.metric("Committees", len(past_committees))

        col3.metric("AI Summaries", summarized_count)
        col4.metric("Pending", pending_count)

        # Generate summaries button
        if pending_count > 0:
            st.markdown("---")
            col_btn, col_info = st.columns([1, 3])
            with col_btn:
                if st.button("🤖 Generate AI Summaries", type="secondary"):
                    st.info("To generate AI summaries, run: `python summarizer.py`")
                    st.code("""
# Generate summaries for up to 5 hearings
python summarizer.py --limit 5

# Or summarize a specific hearing
python summarizer.py --hearing-id <hearing_id>

# List pending hearings
python summarizer.py --list
                    """)
            with col_info:
                st.caption(f"{pending_count} hearings ready for AI summary generation. Requires Anthropic API key.")

        st.markdown("---")

        # Option to filter by time period
        time_filter = st.selectbox(
            "Show hearings from:",
            ["Last 7 days", "Last 30 days", "Last 90 days", "All time"],
            key="past_time_filter"
        )

        # Apply time filter
        if time_filter == "Last 7 days":
            cutoff = today - timedelta(days=7)
        elif time_filter == "Last 30 days":
            cutoff = today - timedelta(days=30)
        elif time_filter == "Last 90 days":
            cutoff = today - timedelta(days=90)
        else:
            cutoff = date.min

        filtered_past = [h for h in past_priority if h["date"] >= cutoff]

        if not filtered_past:
            st.info(f"No priority hearings in the selected time period.")
        else:
            st.markdown(f"**{len(filtered_past)} hearings** in selected period")

            # Group by date
            past_by_date = defaultdict(list)
            for h in filtered_past:
                past_by_date[h["date"]].append(h)

            for day_idx, d in enumerate(sorted(past_by_date.keys(), reverse=True)):
                days_ago = (today - d).days
                days_label = f"({days_ago} day{'s' if days_ago != 1 else ''} ago)"
                st.markdown(f"### {d.strftime('%A, %b %d')} {days_label}")

                day_hearings = sorted(
                    past_by_date[d],
                    key=lambda x: (parse_time_for_sort(x.get("time", "")), x.get("committee",""))
                )
                for i, hearing in enumerate(day_hearings):
                    # Render a simplified card for past hearings
                    topic = hearing.get("topic", "")
                    committee = hearing.get("committee", "")
                    url = hearing.get("url", "")
                    tags = hearing.get("tags", [])
                    hearing_id = hearing.get("hearing_id", "")
                    summary_data = summaries.get(hearing_id, {})
                    has_summary = summary_data.get("status") == "success"

                    tags_html = "".join([f"<span class='cp-chip'>{t}</span>" for t in tags])
                    summary_badge = "✅ AI Summary" if has_summary else "📝 Pending"
                    summary_color = "#28a745" if has_summary else CP_GREY_DARK

                    st.markdown(f"""
                    <div class="cp-card" style="border-left: 7px solid {CP_RED}; opacity: 0.9;">
                      <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:14px;">
                        <div>
                          <div style="font-size:14px; font-weight:700; color:{CP_NAVY}; margin-bottom:4px;">
                            {committee}
                          </div>
                          <div style="font-size:16px; font-weight:700; color:{TEXT_DARK};">
                            {topic}
                          </div>
                          <div class="cp-muted" style="margin-top:4px;">
                            <strong>Time:</strong> {hearing.get("time","TBD")} &nbsp; | &nbsp;
                            <strong>Location:</strong> {hearing.get("location", "TBD")}
                          </div>
                        </div>
                        <span class="cp-badge" style="background:{CP_RED_LIGHT}; color:{CP_RED};">
                          Completed
                        </span>
                      </div>
                      <div style="margin-top:10px;">{tags_html}</div>
                      <div style="margin-top:12px; display: flex; gap: 16px; flex-wrap: wrap;">
                        {f'<a href="{url}" target="_blank" style="color:{CP_NAVY}; font-weight:700;">View hearing page →</a>' if url else ""}
                        <span style="color:{summary_color}; font-weight: 500;">{summary_badge}</span>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)

                    # Show AI summary if available
                    if has_summary:
                        with st.expander("📊 AI Summary", expanded=False):
                            st.markdown(summary_data.get("summary", ""))
                            st.caption(f"Generated: {summary_data.get('generated_utc', '')[:10]} | Model: {summary_data.get('model', 'unknown')}")

with tab_table:
    st.subheader("📊 All hearings (filtered)")
    if not filtered:
        st.info("Nothing matches your filters.")
    else:
        table_rows = []
        for h in sorted(filtered, key=lambda x: (x.get("date") or date.max, x.get("committee",""))):
            d = h.get("date")
            table_rows.append({
                "Date": d.strftime("%Y-%m-%d") if isinstance(d, date) else "",
                "Time": h.get("time", "TBD"),
                "Location": h.get("location", "TBD"),
                "Committee": h.get("committee", ""),
                "Subcommittee": h.get("subcommittee", ""),
                "Topic": h.get("topic", ""),
                "Priority": "Yes" if is_priority_hearing(h) else "No",
                "Tags": ", ".join(h.get("tags", []) or []),
                "Link": h.get("url", ""),
            })

        st.dataframe(table_rows, use_container_width=True, hide_index=True)

        csv_data = export_to_csv(filtered)
        st.download_button(
            label="Export to CSV",
            data=csv_data,
            file_name=f"clearpath_hearings_{date.today().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=False,
            key=f"export_{date.today().strftime('%Y%m%d')}"
        )
from pathlib import Path
import json

REVIEW_QUEUE_PATH = Path("data") / "review_queue.json"
HEALTH_PATH = Path("data") / "source_health.json"
SCHEDULER_LOG_PATH = Path("data") / "scheduler_log.json"

def load_json_file(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default

with tab_updates:
    st.subheader("🧭 Updates since last scout run")

    # Run scout button
    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("🔄 Run Scout Now", type="primary"):
            with st.spinner("Scanning Congress.gov for new hearings..."):
                import subprocess
                result = subprocess.run(
                    ["python3", "scout.py"],
                    capture_output=True,
                    text=True,
                    cwd=str(Path(__file__).parent)
                )
                if result.returncode == 0:
                    st.success("Scout completed! Refresh the page to see updates.")
                    st.code(result.stdout)
                else:
                    st.error("Scout failed")
                    st.code(result.stderr)

    rq = load_json_file(REVIEW_QUEUE_PATH, {})
    health = load_json_file(HEALTH_PATH, {})

    if not rq:
        st.info("No scout run data yet. Click 'Run Scout Now' above or run: `python scout.py`")
    else:
        st.caption(f"Last run: {rq.get('run_utc','(unknown)')}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("New", len(rq.get("new", [])))
        c2.metric("Changed", len(rq.get("changed", [])))
        c3.metric("Needs review", len(rq.get("needs_review", [])))
        c4.metric("Missing", len(rq.get("missing", [])))

        failed = rq.get("failed_sources", [])
        if failed:
            st.warning("⚠️ Failed sources: " + ", ".join(failed))

        st.markdown("### New")
        for hid in rq.get("new", [])[:50]:
            st.write("-", hid)

        st.markdown("### Changed")
        for item in rq.get("changed", [])[:50]:
            st.write(f"- {item['id']} (fields: {', '.join(item['fields'])})")

        st.markdown("### Needs review")
        for hid in rq.get("needs_review", [])[:50]:
            st.write("-", hid)

        st.markdown("### Missing (maybe removed/canceled)")
        for hid in rq.get("missing", [])[:50]:
            st.write("-", hid)

    with st.expander("Source health"):
        if not health:
            st.write("No health report yet.")
        else:
            for committee, h in health.items():
                st.write(f"- **{committee}**: {h.get('status')} ({h.get('error','')})")

    st.markdown("---")
    st.markdown("### ⏰ Scheduled Scout")
    st.markdown("""
    Run the scheduler to automatically fetch new hearings on a schedule.

    **Quick start:**
    ```bash
    # Run scheduler in background (every 6 hours)
    nohup python scheduler.py &

    # Or run every 4 hours
    python scheduler.py --interval 4

    # Or use cron (recommended for production)
    # Add to crontab: 0 */6 * * * cd ~/Desktop/hearings-dashboard && python scout.py
    ```
    """)

    scheduler_log = load_json_file(SCHEDULER_LOG_PATH, [])
    if scheduler_log:
        with st.expander(f"Scheduler log ({len(scheduler_log)} runs)"):
            for entry in reversed(scheduler_log[-10:]):
                status_icon = "✅" if entry.get("status") == "success" else "❌"
                ts = entry.get("timestamp_utc", "")[:19].replace("T", " ")
                st.write(f"{status_icon} **{ts}** - {entry.get('status')} ({entry.get('hearings_updated', 0)} hearings)")
