#!/usr/bin/env python3
"""
AI Summary Generator for Congressional Hearings.

Fetches transcript content from hearing pages and generates AI summaries
using Claude API.

Usage:
    python summarizer.py                    # Summarize all past priority hearings without summaries
    python summarizer.py --hearing-id abc123 # Summarize specific hearing
    python summarizer.py --list             # List hearings pending summarization
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    import requests
    from bs4 import BeautifulSoup
    HAS_SCRAPING = True
except ImportError:
    HAS_SCRAPING = False

# Paths
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
HEARINGS_PATH = DATA_DIR / "hearings_seed.json"
CONFIG_PATH = DATA_DIR / "config.json"
SUMMARIES_PATH = DATA_DIR / "summaries.json"

# Priority keywords (same as app.py)
FOCUS_PATH = DATA_DIR / "committees_focus.json"

def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}

def load_focus():
    if FOCUS_PATH.exists():
        return json.loads(FOCUS_PATH.read_text())
    return {"topic_keywords": []}

def load_hearings():
    if HEARINGS_PATH.exists():
        raw = json.loads(HEARINGS_PATH.read_text())
        for h in raw:
            if isinstance(h.get("date"), str) and h["date"]:
                h["date"] = datetime.strptime(h["date"], "%Y-%m-%d").date()
        return raw
    return []

def save_hearings(hearings):
    serial = []
    for h in hearings:
        hh = dict(h)
        if isinstance(hh.get("date"), date):
            hh["date"] = hh["date"].strftime("%Y-%m-%d")
        serial.append(hh)
    HEARINGS_PATH.write_text(json.dumps(serial, indent=2))

def load_summaries():
    if SUMMARIES_PATH.exists():
        return json.loads(SUMMARIES_PATH.read_text())
    return {}

def save_summaries(summaries):
    SUMMARIES_PATH.write_text(json.dumps(summaries, indent=2))

def is_priority_hearing(h: dict, keywords: set) -> bool:
    """Check if hearing matches priority keywords."""
    topic = (h.get("topic") or "").lower()
    tags = [t.lower() for t in (h.get("tags") or [])]

    for kw in keywords:
        if kw in topic:
            return True
    for tag in tags:
        if tag in keywords:
            return True
    return False

def fetch_transcript_content(url: str) -> str:
    """Attempt to fetch transcript or hearing content from URL."""
    if not HAS_SCRAPING:
        return ""

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove script and style elements
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        # Try to find main content
        content = ""

        # Look for common content containers
        for selector in ["article", "main", ".hearing-content", ".transcript", "#content", ".content"]:
            element = soup.select_one(selector)
            if element:
                content = element.get_text(separator="\n", strip=True)
                break

        if not content:
            # Fallback to body text
            body = soup.find("body")
            if body:
                content = body.get_text(separator="\n", strip=True)

        # Clean up whitespace
        content = re.sub(r'\n\s*\n', '\n\n', content)
        content = content[:15000]  # Limit content length

        return content

    except Exception as e:
        print(f"  Error fetching content: {e}")
        return ""

def generate_summary(hearing: dict, content: str, api_key: str) -> dict:
    """Generate AI summary using Claude API."""
    if not HAS_ANTHROPIC:
        return {"error": "anthropic library not installed"}

    client = anthropic.Anthropic(api_key=api_key)

    # Build context
    hearing_info = f"""
Committee: {hearing.get('committee', 'Unknown')}
Topic: {hearing.get('topic', 'Unknown')}
Date: {hearing.get('date', 'Unknown')}
Witnesses: {', '.join(hearing.get('witnesses', ['Unknown']))}
"""

    prompt = f"""Analyze this congressional hearing and provide a structured summary.

HEARING INFORMATION:
{hearing_info}

CONTENT FROM HEARING PAGE:
{content[:12000] if content else "No transcript content available - summarize based on the hearing information above."}

Please provide:
1. **Key Takeaways** (3-5 bullet points of the most important points)
2. **Policy Implications** (2-3 sentences on what this means for clean energy/climate policy)
3. **Notable Quotes or Positions** (any significant statements from witnesses or members)
4. **Follow-up Items** (any mentioned next steps, pending legislation, or future hearings)

Keep the summary concise and focused on implications for clean energy policy."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )

        summary_text = response.content[0].text

        return {
            "summary": summary_text,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "model": "claude-sonnet-4-20250514",
            "content_length": len(content),
            "status": "success"
        }

    except Exception as e:
        return {
            "error": str(e),
            "status": "failed",
            "generated_utc": datetime.now(timezone.utc).isoformat()
        }

def summarize_hearing(hearing: dict, api_key: str) -> dict:
    """Full pipeline: fetch content and generate summary."""
    hearing_id = hearing.get("hearing_id", "")
    url = hearing.get("url", "")

    print(f"Processing: {hearing.get('topic', '')[:60]}...")

    # Try to fetch content
    content = ""
    if url:
        print(f"  Fetching content from {url[:50]}...")
        content = fetch_transcript_content(url)
        if content:
            print(f"  Got {len(content)} chars of content")
        else:
            print("  No content fetched, will summarize from metadata only")

    # Generate summary
    print("  Generating AI summary...")
    result = generate_summary(hearing, content, api_key)

    if result.get("status") == "success":
        print("  Summary generated successfully!")
    else:
        print(f"  Summary failed: {result.get('error', 'unknown error')}")

    return result

def main():
    parser = argparse.ArgumentParser(description="AI Summary Generator for Hearings")
    parser.add_argument("--hearing-id", type=str, help="Summarize specific hearing by ID")
    parser.add_argument("--list", action="store_true", help="List hearings pending summarization")
    parser.add_argument("--api-key", type=str, help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    parser.add_argument("--limit", type=int, default=5, help="Max hearings to summarize (default: 5)")
    args = parser.parse_args()

    # Load data
    config = load_config()
    focus = load_focus()
    hearings = load_hearings()
    summaries = load_summaries()

    priority_keywords = set(k.lower() for k in focus.get("topic_keywords", []))

    # Get API key
    import os
    api_key = args.api_key or config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")

    # Find past priority hearings
    today = date.today()
    past_priority = [
        h for h in hearings
        if isinstance(h.get("date"), date)
        and h["date"] < today
        and is_priority_hearing(h, priority_keywords)
    ]

    # Filter to those without summaries
    pending = [h for h in past_priority if h.get("hearing_id") not in summaries]

    if args.list:
        print(f"\n{len(pending)} hearings pending summarization:\n")
        for h in sorted(pending, key=lambda x: x.get("date") or date.min, reverse=True)[:20]:
            d = h.get("date")
            date_str = d.strftime("%Y-%m-%d") if d else "N/A"
            print(f"  [{h.get('hearing_id', 'N/A')[:8]}] {date_str} - {h.get('topic', '')[:50]}")
        return

    if not api_key:
        print("Error: No API key found.")
        print("Set ANTHROPIC_API_KEY environment variable, add 'anthropic_api_key' to data/config.json,")
        print("or pass --api-key argument.")
        sys.exit(1)

    if not HAS_ANTHROPIC:
        print("Error: anthropic library not installed. Run: pip install anthropic")
        sys.exit(1)

    # Summarize specific hearing
    if args.hearing_id:
        hearing = next((h for h in hearings if h.get("hearing_id") == args.hearing_id), None)
        if not hearing:
            print(f"Hearing not found: {args.hearing_id}")
            sys.exit(1)

        result = summarize_hearing(hearing, api_key)
        summaries[args.hearing_id] = result
        save_summaries(summaries)

        if result.get("status") == "success":
            print("\n" + "=" * 50)
            print("SUMMARY:")
            print("=" * 50)
            print(result.get("summary", ""))
        return

    # Summarize pending hearings
    if not pending:
        print("No hearings pending summarization.")
        return

    print(f"\nSummarizing {min(len(pending), args.limit)} of {len(pending)} pending hearings...\n")

    for h in sorted(pending, key=lambda x: x.get("date") or date.min, reverse=True)[:args.limit]:
        hearing_id = h.get("hearing_id")
        if not hearing_id:
            continue

        result = summarize_hearing(h, api_key)
        summaries[hearing_id] = result
        save_summaries(summaries)
        print()

    print(f"\nDone! {len(summaries)} total summaries saved to data/summaries.json")

if __name__ == "__main__":
    main()
