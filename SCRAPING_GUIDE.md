# Congressional Hearings Scraping Guide

This guide explains how to automatically update your hearings dashboard with real data from Congress.gov.

## Overview

The system uses **Congress.gov weekly schedule** as the "north star" for hearing information:
- URL format: `https://www.congress.gov/committee-schedule/weekly/YYYY/MM/DD`
- This page lists all committee hearings and meetings for a given week
- It's the most authoritative source for upcoming congressional activity

## Quick Start

### Option 1: Manual Update Script

```bash
# Install dependencies
pip install requests beautifulsoup4

# Run the update script
python update_hearings.py
```

This will:
1. Fetch the weekly schedule from Congress.gov for the next 3 weeks
2. Parse hearing information (committee, topic, date, time, location)
3. Auto-tag hearings based on ClearPath topic keywords
4. Generate "why it matters" summaries
5. Save to `data/hearings_seed.json`
6. Your Streamlit app will automatically load the updated data

### Option 2: Automated Updates

Set up a cron job to run the update script daily:

```bash
# Open crontab editor
crontab -e

# Add this line to run every day at 6 AM
0 6 * * * cd /Users/mackenziedubrule/Desktop/hearings-dashboard && python3 update_hearings.py

# Or run every weekday at 8 AM
0 8 * * 1-5 cd /Users/mackenziedubrule/Desktop/hearings-dashboard && python3 update_hearings.py
```

## How It Works

### 1. Fetching from Congress.gov

The scraper fetches the weekly schedule page and parses the HTML to extract:
- Committee name
- Hearing topic/title
- Date and time
- Location (room number)
- Links to committee websites

### 2. Auto-Tagging

Hearings are automatically tagged based on keyword matching:

```python
Keywords → Tags:
- "nuclear", "reactor", "smr" → Nuclear
- "carbon capture", "ccs" → Carbon Capture
- "critical mineral", "rare earth" → Critical Minerals
- "permitting", "nepa" → Permitting, NEPA
- "hydrogen" → Hydrogen
- "battery", "storage" → Energy Storage
- etc.
```

### 3. Generating Summaries

The system generates "why it matters" explanations based on the topic:

```python
Topic contains "nuclear" →
  "Reviews nuclear energy policy and advanced reactor development
   critical for baseload clean energy."

Topic contains "permitting" →
  "Discusses regulatory reforms affecting clean energy
   infrastructure deployment timelines."
```

### 4. Committee-Specific Enhancement (Future)

For even richer data, you can extend the scraper to:
- Visit each committee's hearing page
- Extract witness lists
- Download hearing documents
- Find livestream links
- Identify related bills

## Important Notes

### HTML Structure Changes

⚠️ **Congress.gov may change their HTML structure at any time!**

If the scraper stops working:

1. Visit https://www.congress.gov/committee-schedule/weekly
2. Right-click → "Inspect Element"
3. Find the HTML structure for hearing entries
4. Update the CSS selectors in `update_hearings.py`:

```python
# Current selectors (line ~66):
events = soup.select('.expanded')  # ← Update this

# And in parse_single_event():
committee_elem = event_element.select_one('.committee-name')  # ← Update these
topic_elem = event_element.select_one('.event-title')
# etc.
```

### Manual Review Recommended

While auto-tagging and summaries are helpful, you should:
- Review auto-generated content for accuracy
- Add specific witness information when available
- Link to actual hearing notices from committee websites
- Update "why it matters" with more specific policy context

## Advanced: Using Claude API for Better Summaries

For production use, you can integrate Claude API to generate better summaries:

```python
import anthropic

def generate_ai_summary(topic, committee):
    client = anthropic.Anthropic(api_key="your-api-key")

    message = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Given this congressional hearing:
Committee: {committee}
Topic: {topic}

Write a 1-2 sentence summary explaining why this hearing matters
for clean energy policy and ClearPath's focus areas
(nuclear, carbon capture, critical minerals, permitting, hydrogen, etc.).
"""
        }]
    )

    return message.content[0].text
```

## Workflow Recommendation

### Daily/Weekly Workflow:

1. **Monday morning**: Run `python update_hearings.py`
2. **Review**: Open the Streamlit app and review new hearings
3. **Enhance**: Click "Edit" on priority hearings to add:
   - Specific witness names (check committee websites)
   - Related bills (check committee notices)
   - More detailed "why it matters" context
   - Livestream links (usually posted day-of)
4. **Share**: Export to CSV or share the dashboard URL with your team

### Before Important Hearings:

1. Visit the committee website for the specific hearing
2. Download witness testimony PDFs
3. Add witness names and document links to the hearing card
4. Update the summary with specific policy angles

## Committee Website Patterns

Each committee has their own website structure. Here are the key pages:

### House Energy & Commerce
- Calendar: https://energycommerce.house.gov/calendar/
- Hearings: https://energycommerce.house.gov/hearings/
- Pattern: Each hearing has a dedicated page with witnesses, documents

### Senate Energy & Natural Resources
- Hearings: https://www.energy.senate.gov/hearings
- Pattern: Hearings list with links to individual hearing pages

### House Natural Resources
- Calendar: https://naturalresources.house.gov/calendar/
- Pattern: Calendar view with hearing details

### Senate Environment & Public Works
- Hearings: https://www.epw.senate.gov/public/index.cfm/hearings
- Pattern: Chronological list with hearing details

## Future Enhancements

Possible improvements to the scraping system:

1. **Real-time updates**: Check Congress.gov every hour for changes
2. **Witness scraping**: Automatically extract witness lists from committee pages
3. **Document parsing**: Download and summarize witness testimony PDFs
4. **Bill tracking**: Link hearings to specific bills using Congress.gov API
5. **Alert system**: Email/Slack notifications for new priority hearings
6. **Historical archive**: Keep all past hearings for trend analysis

## Troubleshooting

### "No hearings found"
- Congress.gov HTML structure may have changed
- Check your internet connection
- Visit the URL manually to verify it's working

### "Tags not accurate"
- Update keyword mapping in `auto_tag_topic()` function
- Add domain-specific keywords for ClearPath topics

### "Summaries too generic"
- Implement Claude API integration for AI-generated summaries
- Add more specific rules in `generate_summary()` function

## Questions?

This is a starting framework. You'll need to:
1. Inspect Congress.gov HTML and update selectors
2. Test the scraper and fix any parsing issues
3. Customize the auto-tagging and summary logic for ClearPath's needs
4. Set up automation (cron jobs or GitHub Actions)

The key principle: **Congress.gov weekly schedule is the source of truth**,
then enhance with committee-specific details as needed.
