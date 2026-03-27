#!/usr/bin/env python3
"""
digest.py — ClearPath Congressional Hearings Digest
Generates and sends a branded HTML email to Amanda (and others).

Three modes (auto-detected by day of week):
  - Monday   → This week's full schedule (Mon–Fri)
  - Tue–Thu  → Today's priority + tracked committee hearings
  - Friday   → Next week's full preview

Usage:
  python digest.py              # auto-detect mode from today's day of week
  python digest.py --this-week  # force this week's full schedule
  python digest.py --daily      # force today-only digest
  python digest.py --weekly     # force next week's preview
  python digest.py --preview    # save HTML preview to data/digest_preview.html
  python digest.py --date 2026-03-04  # generate for a specific date

Setup — add to data/config.json:
  {
    "congress_api_key": "...",
    "digest": {
      "gmail_user":         "sender@gmail.com",
      "gmail_app_password": "xxxx xxxx xxxx xxxx",
      "from_name":          "ClearPath Hearings Dashboard",
      "to_emails":          ["amanda@clearpath.org"],
      "cc_emails":          ["you@clearpath.org"]
    }
  }

Gmail App Password setup (one-time, ~2 minutes):
  1. Go to myaccount.google.com → Security
  2. Enable 2-Step Verification (if not already on)
  3. Search "App passwords" → create one named "Hearings Digest"
  4. Copy the 16-character password (e.g. "abcd efgh ijkl mnop") into gmail_app_password above
  Note: Use the Gmail account you want the email to come FROM.
        It can be a personal Gmail — the recipient just sees the from_name.
"""

import argparse
import json
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
DATA_DIR       = Path("data")
HEARINGS_PATH  = DATA_DIR / "hearings_seed.json"
CONFIG_PATH    = DATA_DIR / "config.json"
FOCUS_PATH     = DATA_DIR / "committees_focus.json"

def load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default

cfg        = load_json(CONFIG_PATH, {})
focus_cfg  = load_json(FOCUS_PATH, {})
digest_cfg = cfg.get("digest", {})

TRACKED_COMMITTEES = focus_cfg.get("house", []) + focus_cfg.get("senate", [])
TOPIC_KEYWORDS     = [k.lower() for k in focus_cfg.get("topic_keywords", [])]

# ---------------------------------------------------------------------------
# AI relevance scoring (ClearPath-specific)
# ---------------------------------------------------------------------------
import os as _os
try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

_CLEARPATH_TOPICS = """
ClearPath is a clean energy innovation and policy organization. Relevant topics:
- Nuclear energy: advanced reactors, SMRs, fission, fusion
- Energy storage: batteries, grid storage, pumped hydro, long-duration storage
- Natural gas: infrastructure, LNG, methane, gas turbines
- Carbon capture, utilization and storage (CCUS / CCS)
- Hydropower and pumped storage hydropower
- Geothermal energy
- Hydrogen: clean hydrogen, electrolysis, fuel cells, hydrogen hubs
- Carbon dioxide removal (CDR), direct air capture (DAC)
- Critical minerals and metals: supply chains, mining, processing, permitting
- Clean manufacturing: industrial decarbonization, cement, steel, concrete, aluminum
- Energy permitting reform: NEPA, siting, transmission, federal lands
- Agriculture and rural energy
- Global energy leadership, competitiveness with China
- Clean energy R&D, DOE programs, national labs
- Grid reliability, grid infrastructure, transmission
- IRA, energy tax credits, energy incentives
- Energy innovation, technology deployment
"""

def score_hearings_batch(hearings: list) -> dict:
    """
    Score a batch of hearings for ClearPath relevance using Claude.
    Returns dict mapping hearing_id -> (score: int, reason: str).
    Gracefully returns {} if API key missing or call fails.
    """
    if not _ANTHROPIC_AVAILABLE or not _os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    if not hearings:
        return {}

    lines = [
        f"{i+1}. [COMMITTEE: {h.get('committee','')}] {(h.get('topic') or '')[:200]}"
        for i, h in enumerate(hearings)
    ]

    prompt = (
        "You are a policy analyst at ClearPath, a clean energy innovation organization.\n\n"
        f"ClearPath focus areas:{_CLEARPATH_TOPICS}\n"
        "Rate each congressional hearing 1-10 for relevance to ClearPath's mission:\n"
        "  8-10 = directly on-topic (energy tech, permitting, critical minerals, clean manufacturing)\n"
        "  5-7  = adjacent or possibly relevant (supply chains, infrastructure, competitiveness)\n"
        "  1-4  = not relevant\n\n"
        "Hearings to rate:\n" + "\n".join(lines) + "\n\n"
        'Respond ONLY with a JSON object, no extra text. Format: '
        '{"1": {"score": 8, "reason": "Addresses nuclear licensing"}, "2": {"score": 2, "reason": "Unrelated"}, ...}'
    )

    try:
        import json as _json
        client = _anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = _json.loads(msg.content[0].text)
        result = {}
        for i, h in enumerate(hearings):
            key = str(i + 1)
            if key in raw:
                result[h.get("hearing_id", str(i))] = (
                    int(raw[key].get("score", 0)),
                    raw[key].get("reason", "")
                )
        return result
    except Exception as e:
        print(f"  ⚠️  AI scoring skipped: {e}")
        return {}


# ---------------------------------------------------------------------------
# ClearPath brand colors
# ---------------------------------------------------------------------------
CP_NAVY      = "#193D69"
CP_RED       = "#9D1C20"
CP_GREY_LIGHT = "#F4F6F9"
CP_GREY_MED  = "#E8ECF0"
CP_GREY_DARK = "#6B7280"
CP_TEXT      = "#1F2937"
CP_WHITE     = "#FFFFFF"

# ---------------------------------------------------------------------------
# Hearing helpers
# ---------------------------------------------------------------------------
def parse_date(raw) -> Optional[date]:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except Exception:
            pass
    return None

def normalize(s: str) -> str:
    return s.lower().replace("&", "and").replace(",", "").strip()

def is_tracked_committee(h: dict) -> bool:
    hc = normalize(h.get("committee", ""))
    return any(normalize(c) in hc or hc in normalize(c) for c in TRACKED_COMMITTEES)

def is_priority(h: dict) -> bool:
    topic = (h.get("topic") or "").lower()
    tags  = " ".join(t.lower() for t in (h.get("tags") or []))
    text  = topic + " " + tags
    return any(re.search(r'\b' + re.escape(kw) + r'\b', text) for kw in TOPIC_KEYWORDS)

def get_hearings_for_range(start: date, end: date) -> List[dict]:
    data = load_json(HEARINGS_PATH, [])
    seen_ids: set = set()
    all_in_range = []

    for h in data:
        d = parse_date(h.get("date"))
        if not d or not (start <= d <= end):
            continue
        # Deduplicate by hearing_id (fixes duplicate entries)
        hid = h.get("hearing_id") or (
            f"{h.get('committee','')}-{h.get('date','')}-{(h.get('topic') or '')[:50]}"
        )
        if hid in seen_ids:
            continue
        seen_ids.add(hid)

        h["_date"]          = d
        h["_is_tracked"]    = is_tracked_committee(h)
        h["_is_priority"]   = is_priority(h)   # keyword match
        h["_is_ai_flagged"] = False
        h["_ai_reason"]     = ""
        all_in_range.append(h)

    # AI-score hearings not caught by tracked list or keyword match
    unscored = [h for h in all_in_range
                if not h["_is_tracked"] and not h["_is_priority"]]
    if unscored:
        print(f"  🤖 AI-scoring {len(unscored)} non-tracked hearing(s)…")
        ai_scores = score_hearings_batch(unscored)
        for h in unscored:
            hid = h.get("hearing_id", "")
            if hid in ai_scores:
                score, reason = ai_scores[hid]
                if score >= 6:
                    h["_is_ai_flagged"] = True
                    h["_ai_reason"]     = reason
                    h["_is_priority"]   = True   # surfaces in priority section

    # Include tracked, keyword-priority, or AI-flagged hearings
    results = [h for h in all_in_range
               if h["_is_tracked"] or h["_is_priority"] or h["_is_ai_flagged"]]
    results.sort(key=lambda x: (x["_date"], x.get("time", "ZZ"), x.get("committee", "")))
    return results

def week_of(anchor: date):
    mon = anchor - timedelta(days=anchor.weekday())
    return mon, mon + timedelta(days=4)

# ---------------------------------------------------------------------------
# HTML builders
# ---------------------------------------------------------------------------
def hearing_card_html(h: dict, show_date: bool = False) -> str:
    committee   = h.get("committee", "")
    topic       = h.get("topic", "No title")
    time_str    = h.get("time", "TBD")
    location    = h.get("location", "TBD")
    why         = re.sub(r'<[^>]+>', ' ', h.get("why", "")).strip()
    why         = re.sub(r'\s+', ' ', why)
    if why.lower() in {"search congress.gov", "search", "view details"}:
        why = ""
    url         = h.get("url", "")
    congress_url = h.get("congress_url", "")
    witnesses   = [w for w in (h.get("witnesses") or []) if w and w != "TBD"]
    bills       = h.get("bills") or []
    is_pri      = h.get("_is_priority", False)
    d           = h.get("_date")

    border_color = CP_RED if is_pri else CP_NAVY
    badge_bg     = "#FDECEA" if is_pri else "#EBF2FA"
    badge_color  = CP_RED   if is_pri else CP_NAVY
    badge_text   = "⭐ Priority" if is_pri else "Tracked"

    date_line = ""
    if show_date and d:
        date_line = f"""
        <div style="font-size:12px; color:{CP_GREY_DARK}; font-weight:600;
                    text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px;">
          {d.strftime("%A, %B %-d")}
        </div>"""

    witness_html = ""
    if witnesses:
        witness_list = ", ".join(witnesses[:4])
        if len(witnesses) > 4:
            witness_list += f" +{len(witnesses)-4} more"
        witness_html = f"""
        <div style="margin-top:10px; font-size:13px; color:{CP_GREY_DARK};">
          <strong style="color:{CP_TEXT};">Witnesses:</strong> {witness_list}
        </div>"""

    bills_html = ""
    if bills:
        bills_html = f"""
        <div style="margin-top:6px; font-size:13px; color:{CP_GREY_DARK};">
          <strong style="color:{CP_TEXT};">Bills:</strong> {", ".join(bills[:4])}
        </div>"""

    why_html = ""
    if why:
        why_html = f"""
        <div style="margin-top:10px; padding:10px 14px; background:#F0F4F8;
                    border-radius:6px; font-size:13px; color:{CP_TEXT}; line-height:1.5;">
          <strong>Why it matters:</strong> {why}
        </div>"""

    link_html = ""
    if url:
        secondary = ""
        if congress_url and congress_url != url:
            secondary = f"""
          <a href="{congress_url}" style="font-size:12px; color:{CP_GREY_DARK};
             text-decoration:none; margin-left:14px;">Congress.gov record</a>"""
        link_html = f"""
        <div style="margin-top:14px; display:flex; align-items:center; flex-wrap:wrap; gap:8px;">
          <a href="{url}" style="display:inline-block; background:{CP_NAVY}; color:{CP_WHITE};
             font-size:13px; font-weight:600; padding:8px 18px; border-radius:5px;
             text-decoration:none;">View Hearing Details →</a>{secondary}
        </div>"""

    return f"""
    <div style="margin-bottom:18px; border-left:5px solid {border_color};
                background:{CP_WHITE}; border-radius:0 8px 8px 0;
                padding:16px 20px; box-shadow:0 1px 4px rgba(0,0,0,0.07);">
      {date_line}
      <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px;">
        <div style="flex:1;">
          <div style="font-size:12px; font-weight:700; color:{CP_NAVY};
                      text-transform:uppercase; letter-spacing:0.4px; margin-bottom:5px;">
            {committee}
          </div>
          <div style="font-size:16px; font-weight:700; color:{CP_TEXT}; line-height:1.3;">
            {topic}
          </div>
          <div style="margin-top:6px; font-size:13px; color:{CP_GREY_DARK};">
            🕐 {time_str} &nbsp;&nbsp; 📍 {location}
          </div>
        </div>
        <div style="flex-shrink:0;">
          <span style="display:inline-block; background:{badge_bg}; color:{badge_color};
                       font-size:11px; font-weight:700; padding:4px 10px;
                       border-radius:999px; white-space:nowrap;">
            {badge_text}
          </span>
        </div>
      </div>
      {witness_html}
      {bills_html}
      {why_html}
      {link_html}
    </div>"""


def build_daily_html(target_date: date, hearings: List[dict]) -> str:
    date_str  = target_date.strftime("%A, %B %-d, %Y")
    priority  = [h for h in hearings if h.get("_is_priority")]
    other     = [h for h in hearings if not h.get("_is_priority")]
    total     = len(hearings)

    if not hearings:
        body_content = f"""
        <div style="text-align:center; padding:40px 20px; color:{CP_GREY_DARK};">
          <div style="font-size:40px; margin-bottom:12px;">📋</div>
          <div style="font-size:16px;">No priority or tracked committee hearings scheduled for today.</div>
        </div>"""
    else:
        priority_section = ""
        if priority:
            cards = "".join(hearing_card_html(h) for h in priority)
            priority_section = f"""
            <div style="margin-bottom:30px;">
              <div style="font-size:13px; font-weight:700; color:{CP_RED};
                          text-transform:uppercase; letter-spacing:0.8px;
                          border-bottom:2px solid {CP_RED}; padding-bottom:8px; margin-bottom:16px;">
                ⭐ Priority Hearings ({len(priority)})
              </div>
              {cards}
            </div>"""

        other_section = ""
        if other:
            cards = "".join(hearing_card_html(h) for h in other)
            other_section = f"""
            <div style="margin-bottom:30px;">
              <div style="font-size:13px; font-weight:700; color:{CP_NAVY};
                          text-transform:uppercase; letter-spacing:0.8px;
                          border-bottom:2px solid {CP_NAVY}; padding-bottom:8px; margin-bottom:16px;">
                Tracked Committee Hearings ({len(other)})
              </div>
              {cards}
            </div>"""

        body_content = priority_section + other_section

    summary_color = CP_RED if priority else CP_NAVY
    summary_text  = (
        f"<strong>{len(priority)} priority hearing{'s' if len(priority)!=1 else ''}</strong>"
        f" and {len(other)} tracked hearing{'s' if len(other)!=1 else ''} today."
        if hearings else "No relevant hearings today."
    )

    return _wrap_email(
        title=f"Congressional Hearings — {date_str}",
        subtitle=f"Daily briefing for {date_str}",
        summary_text=summary_text,
        summary_color=summary_color,
        body=body_content,
    )


def build_weekly_html(week_start: date, hearings: List[dict], this_week: bool = False) -> str:
    week_end   = week_start + timedelta(days=4)
    week_label = f"{week_start.strftime('%B %-d')}–{week_end.strftime('%-d, %Y')}"
    priority   = [h for h in hearings if h.get("_is_priority")]
    week_word  = "This Week's" if this_week else "Next Week's"
    no_hearings_msg = "this week" if this_week else "next week"

    if not hearings:
        body_content = f"""
        <div style="text-align:center; padding:40px 20px; color:{CP_GREY_DARK};">
          <div style="font-size:40px; margin-bottom:12px;">📋</div>
          <div style="font-size:16px;">No priority or tracked committee hearings found for {no_hearings_msg}.</div>
        </div>"""
    else:
        # Group by day
        by_day = {}
        for h in hearings:
            d = h["_date"]
            by_day.setdefault(d, []).append(h)

        day_sections = ""
        for d in sorted(by_day.keys()):
            day_hearings = by_day[d]
            cards = "".join(hearing_card_html(h) for h in day_hearings)
            day_name = d.strftime("%A, %B %-d")
            pri_count = sum(1 for h in day_hearings if h.get("_is_priority"))
            day_badge = f'<span style="background:{CP_RED}; color:white; font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; margin-left:8px;">⭐ {pri_count} priority</span>' if pri_count else ""
            day_sections += f"""
            <div style="margin-bottom:30px;">
              <div style="font-size:15px; font-weight:700; color:{CP_NAVY};
                          padding:10px 14px; background:{CP_GREY_MED};
                          border-radius:6px; margin-bottom:14px;">
                {day_name} {day_badge}
              </div>
              {cards}
            </div>"""

        body_content = day_sections

    summary_text = (
        f"<strong>{len(hearings)} hearing{'s' if len(hearings)!=1 else ''}</strong> scheduled "
        f"{no_hearings_msg} — {len(priority)} priority."
        if hearings else f"No relevant hearings {no_hearings_msg}."
    )

    return _wrap_email(
        title=f"{week_word} Hearings — {week_label}",
        subtitle=f"Weekly schedule for the week of {week_label}",
        summary_text=summary_text,
        summary_color=CP_RED if priority else CP_NAVY,
        body=body_content,
    )


def _wrap_email(title: str, subtitle: str, summary_text: str,
                summary_color: str, body: str) -> str:
    logo_url = "https://clearpath.org/wp-content/uploads/sites/44/2023/08/clearpath-logo.png"
    today_str = date.today().strftime("%B %-d, %Y")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
</head>
<body style="margin:0; padding:0; background:{CP_GREY_LIGHT}; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">

  <!-- Wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:{CP_GREY_LIGHT}; padding:30px 0;">
    <tr><td align="center">
      <table width="640" cellpadding="0" cellspacing="0" style="max-width:640px; width:100%;">

        <!-- Header -->
        <tr>
          <td style="background:{CP_NAVY}; padding:24px 32px; border-radius:10px 10px 0 0;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td>
                  <img src="{logo_url}" height="36" alt="ClearPath"
                style="display:block; height:36px; background:white; padding:4px 8px; border-radius:4px;">
                </td>
                <td align="right">
                  <span style="color:rgba(255,255,255,0.7); font-size:12px;">
                    {today_str}
                  </span>
                </td>
              </tr>
            </table>
            <div style="margin-top:16px; color:{CP_WHITE}; font-size:22px; font-weight:700;
                        line-height:1.2;">
              {title}
            </div>
            <div style="margin-top:4px; color:rgba(255,255,255,0.75); font-size:13px;">
              {subtitle}
            </div>
          </td>
        </tr>

        <!-- Summary bar -->
        <tr>
          <td style="background:{summary_color}; padding:12px 32px;">
            <span style="color:{CP_WHITE}; font-size:13px;">{summary_text}</span>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="background:{CP_WHITE}; padding:28px 32px;">
            {body}
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:{CP_GREY_MED}; padding:18px 32px;
                     border-radius:0 0 10px 10px; border-top:1px solid #D1D5DB;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-size:12px; color:{CP_GREY_DARK};">
                  Sent by the ClearPath Hearings Dashboard &nbsp;·&nbsp;
                  Data from Congress.gov API
                </td>
                <td align="right">
                  <a href="http://localhost:8501"
                     style="font-size:12px; color:{CP_NAVY}; text-decoration:none; font-weight:600;">
                    Open Dashboard →
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Sending via Gmail SMTP (App Password)
# ---------------------------------------------------------------------------
def send_email(subject: str, html: str) -> bool:
    to_emails   = digest_cfg.get("to_emails", [])
    cc_emails   = digest_cfg.get("cc_emails", [])
    gmail_user  = digest_cfg.get("gmail_user", "").strip()
    app_pw      = digest_cfg.get("gmail_app_password", "").strip()
    from_name   = digest_cfg.get("from_name", "ClearPath Hearings Dashboard")

    if not gmail_user or not app_pw:
        print("⚠️  Gmail credentials missing in data/config.json → digest section.")
        print("   Add 'gmail_user' and 'gmail_app_password'. See docstring for setup steps.")
        return False

    if not to_emails:
        print("⚠️  No recipients in digest.to_emails")
        return False

    # Build the message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{from_name} <{gmail_user}>"
    msg["To"]      = ", ".join(to_emails)
    if cc_emails:
        msg["Cc"]  = ", ".join(cc_emails)

    msg.attach(MIMEText(html, "html"))

    all_recipients = to_emails + cc_emails

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, app_pw)
            server.sendmail(gmail_user, all_recipients, msg.as_string())
        print(f"✅ Email sent → {', '.join(to_emails)}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("✗ Gmail auth failed. Check gmail_user and gmail_app_password in config.json.")
        print("  Make sure you're using an App Password, not your regular Gmail password.")
        return False
    except Exception as e:
        print(f"✗ Failed to send email: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(
    mode: str = "auto",           # "auto", "daily", "weekly", "this_week"
    preview: bool = False,
    target_date: Optional[date] = None,
):
    today = target_date or date.today()
    is_monday = today.weekday() == 0
    is_friday = today.weekday() == 4

    # Auto-detect mode based on day of week:
    #   Monday    → this week's full schedule
    #   Tue–Thu   → today only (daily)
    #   Friday    → next week's preview
    if mode == "auto":
        if is_monday:
            mode = "this_week"
        elif is_friday:
            mode = "weekly"
        else:
            mode = "daily"

    print(f"Mode: {mode} | Date: {today}")

    if mode == "daily":
        hearings = get_hearings_for_range(today, today)
        html     = build_daily_html(today, hearings)
        date_str = today.strftime("%b %-d")
        pri      = sum(1 for h in hearings if h.get("_is_priority"))
        subject  = f"Hearings Today ({date_str})" + (f" — {pri} Priority" if pri else "")
        print(f"Found {len(hearings)} hearings for {today} ({pri} priority)")

    elif mode == "this_week":
        # This week: Mon through Fri of the current week
        week_start = today - timedelta(days=today.weekday())  # back to Monday
        week_end   = week_start + timedelta(days=4)
        hearings   = get_hearings_for_range(week_start, week_end)
        html       = build_weekly_html(week_start, hearings, this_week=True)
        pri        = sum(1 for h in hearings if h.get("_is_priority"))
        week_label = f"{week_start.strftime('%b %-d')}–{week_end.strftime('%-d')}"
        subject    = f"This Week's Hearings ({week_label})" + (f" — {pri} Priority" if pri else "")
        print(f"Found {len(hearings)} hearings for this week ({week_start} → {week_end}, {pri} priority)")

    else:  # "weekly" — next week's preview (Fridays)
        # Always go to next Monday from today
        week_start = today + timedelta(days=(7 - today.weekday()))
        week_end   = week_start + timedelta(days=4)
        hearings   = get_hearings_for_range(week_start, week_end)
        html       = build_weekly_html(week_start, hearings, this_week=False)
        pri        = sum(1 for h in hearings if h.get("_is_priority"))
        week_label = f"{week_start.strftime('%b %-d')}–{week_end.strftime('%-d')}"
        subject    = f"Next Week's Hearings ({week_label})" + (f" — {pri} Priority" if pri else "")
        print(f"Found {len(hearings)} hearings for week of {week_start} ({pri} priority)")

    if preview:
        preview_path = Path("data/digest_preview.html")
        preview_path.write_text(html)
        print(f"\n📄 Preview saved: data/digest_preview.html")
        print("   Open in browser to see the email layout.")
        return True

    return send_email(subject, html)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ClearPath Congressional Hearings Digest")
    group  = parser.add_mutually_exclusive_group()
    group.add_argument("--daily",     action="store_true", help="Force daily digest (today)")
    group.add_argument("--this-week", action="store_true", help="Force this week's full schedule")
    group.add_argument("--weekly",    action="store_true", help="Force next week's preview")
    parser.add_argument("--preview", action="store_true", help="Save HTML preview, don't send")
    parser.add_argument("--date",    type=str, help="Target date (YYYY-MM-DD)")
    args = parser.parse_args()

    target = None
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()

    mode = "auto"
    if args.daily:      mode = "daily"
    if args.this_week:  mode = "this_week"
    if args.weekly:     mode = "weekly"

    success = run(mode=mode, preview=args.preview, target_date=target)
    sys.exit(0 if success else 1)
