#!/bin/bash
# =============================================================
# ClearPath Hearings Dashboard — Daily Pipeline
# Runs automatically via cron each morning.
#
# Pipeline:
#   1. fetch_all_hearings.py  → pull all hearings from Congress.gov API
#   2. digest.py              → generate and send Amanda's hearing alert
#      (digest is skipped automatically until Gmail credentials are added
#       to data/config.json — no errors, just a warning in the log)
#
# To install (run once in your terminal):
#   crontab -e
#   Add this line to run at 7:00 AM Mon–Fri:
#   0 7 * * 1-5 /bin/bash ~/Desktop/hearings-dashboard/run_daily.sh
# =============================================================

# --- Config ---
DASHBOARD_DIR="$HOME/Desktop/hearings-dashboard"
LOG_DIR="$DASHBOARD_DIR/logs"
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/pipeline_$DATE.log"
PYTHON=$(which python3)

# --- Setup ---
mkdir -p "$LOG_DIR"
cd "$DASHBOARD_DIR" || { echo "ERROR: Dashboard directory not found at $DASHBOARD_DIR"; exit 1; }

echo "============================================" >> "$LOG_FILE"
echo "ClearPath Daily Pipeline — $DATE $(date +%H:%M:%S)" >> "$LOG_FILE"
echo "============================================" >> "$LOG_FILE"

# --- Step 1: Fetch all hearings from Congress.gov API ---
echo "" >> "$LOG_FILE"
echo "── Step 1: Fetch Hearings (Congress.gov API) ──" >> "$LOG_FILE"
echo "[$(date +%H:%M:%S)] Starting fetch..." >> "$LOG_FILE"

$PYTHON fetch_all_hearings.py >> "$LOG_FILE" 2>&1
FETCH_EXIT=$?

if [ $FETCH_EXIT -eq 0 ]; then
    echo "[$(date +%H:%M:%S)] ✓ Fetch completed" >> "$LOG_FILE"
else
    echo "[$(date +%H:%M:%S)] ✗ Fetch failed (exit $FETCH_EXIT) — continuing with existing data" >> "$LOG_FILE"
fi

# --- Step 2: Digest (Amanda's alert) ---
# NOTE: This step will log a warning and skip gracefully if Gmail credentials
# are not yet configured in data/config.json. No crash, no broken pipeline.
echo "" >> "$LOG_FILE"
echo "── Step 2: Digest ──" >> "$LOG_FILE"
echo "[$(date +%H:%M:%S)] Running digest..." >> "$LOG_FILE"

if [ -f "$DASHBOARD_DIR/digest.py" ]; then
    $PYTHON digest.py >> "$LOG_FILE" 2>&1
    DIGEST_EXIT=$?
    if [ $DIGEST_EXIT -eq 0 ]; then
        echo "[$(date +%H:%M:%S)] ✓ Digest sent" >> "$LOG_FILE"
    else
        echo "[$(date +%H:%M:%S)] ✗ Digest failed (exit $DIGEST_EXIT)" >> "$LOG_FILE"
    fi
else
    echo "[$(date +%H:%M:%S)] — digest.py not found, skipping" >> "$LOG_FILE"
fi

# --- Cleanup old logs (keep last 30 days) ---
find "$LOG_DIR" -name "pipeline_*.log" -mtime +30 -delete 2>/dev/null

echo "" >> "$LOG_FILE"
echo "Pipeline done — $(date +%H:%M:%S)" >> "$LOG_FILE"

cat "$LOG_FILE"