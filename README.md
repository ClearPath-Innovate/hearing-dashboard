# ClearPath Congressional Hearings Dashboard

A Streamlit dashboard for tracking congressional hearings relevant to clean energy policy.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the dashboard
streamlit run app.py
```

## Features

- 📊 **Visual Dashboard** - Track hearings by date, week, or in table view
- 🏛️ **ClearPath Focus Committees** - Highlights priority committees
- 🏷️ **Smart Tagging** - Filter by topic keywords (Nuclear, Carbon Capture, etc.)
- 📅 **Calendar Export** - Download .ics files to add hearings to your calendar
- 📤 **CSV Export** - Export filtered hearings to spreadsheet
- 🔗 **Direct Links** - Quick access to Congress.gov and committee websites

## Workflow: Adding Real Hearings

### Step 1: View the Weekly Schedule

**Option A - Use the viewer script:**
```bash
python view_schedule.py
```

**Option B - Visit Congress.gov directly:**
- https://www.congress.gov/committee-schedule/weekly/

### Step 2: Add Hearings

1. Run `streamlit run app.py`
2. Use the "Add a hearing" form in sidebar
3. Select committee, add topic, date, time, location
4. Add "why it matters" summary and tags
5. Click "Add hearing"

### Step 3: Enhance with Details

Visit committee websites for:
- Witness lists
- Related bills
- Hearing documents
- Livestream links

## ClearPath Focus Committees (10)

**House:** Energy & Commerce, Natural Resources, Transportation & Infrastructure, Ways & Means, Science Space & Technology

**Senate:** Energy & Natural Resources, Environment & Public Works, Agriculture Nutrition & Forestry, Foreign Relations, Commerce Science & Transportation

## Weekly Workflow

1. **Monday:** Check Congress.gov schedule, add relevant hearings
2. **Throughout week:** Update with witnesses, documents, livestream links
3. **After hearings:** Add archived video/transcript links

## Files

- `data/hearings_seed.json` - Hearings database
- `data/committees_focus.json` - Committee configuration
- Example data marked with `(EXAMPLE)` prefix

---

**Note:** Uses Congress.gov as the "north star" - manual curation for quality, no fragile web scraping!
