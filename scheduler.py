#!/usr/bin/env python3
"""
Scheduled scout runner for the hearings dashboard.

Runs scout.py at configurable intervals to keep hearing data fresh.
Can be run as a background service or via cron.

Usage:
    python scheduler.py              # Run with default settings (every 6 hours)
    python scheduler.py --interval 4 # Run every 4 hours
    python scheduler.py --once       # Run once and exit
"""

import argparse
import subprocess
import sys
import time
import json
from datetime import datetime, timezone
from pathlib import Path

# Config
SCRIPT_DIR = Path(__file__).parent
SCOUT_SCRIPT = SCRIPT_DIR / "scout.py"
SCHEDULER_LOG = SCRIPT_DIR / "data" / "scheduler_log.json"

def log_run(status: str, message: str = "", hearings_updated: int = 0):
    """Log a scheduler run to JSON file."""
    log_entry = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "message": message,
        "hearings_updated": hearings_updated
    }

    # Load existing log
    log_data = []
    if SCHEDULER_LOG.exists():
        try:
            log_data = json.loads(SCHEDULER_LOG.read_text())
        except:
            log_data = []

    # Keep last 100 entries
    log_data.append(log_entry)
    log_data = log_data[-100:]

    SCHEDULER_LOG.write_text(json.dumps(log_data, indent=2))
    return log_entry

def run_scout() -> dict:
    """Run the scout script and return results."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running scout...")

    try:
        result = subprocess.run(
            [sys.executable, str(SCOUT_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            cwd=str(SCRIPT_DIR)
        )

        if result.returncode == 0:
            # Parse output for stats
            output = result.stdout
            hearings_count = 0
            if "hearings" in output.lower():
                # Try to extract number from output
                import re
                match = re.search(r'(\d+)\s*(?:new|total)?\s*hearings?', output.lower())
                if match:
                    hearings_count = int(match.group(1))

            log_entry = log_run("success", output[:500], hearings_count)
            print(f"  Scout completed successfully. {hearings_count} hearings processed.")
            return log_entry
        else:
            log_entry = log_run("error", result.stderr[:500])
            print(f"  Scout failed: {result.stderr[:200]}")
            return log_entry

    except subprocess.TimeoutExpired:
        log_entry = log_run("timeout", "Scout timed out after 5 minutes")
        print("  Scout timed out.")
        return log_entry
    except Exception as e:
        log_entry = log_run("error", str(e)[:500])
        print(f"  Scout error: {e}")
        return log_entry

def main():
    parser = argparse.ArgumentParser(description="Scheduled scout runner")
    parser.add_argument("--interval", type=float, default=6,
                        help="Hours between scout runs (default: 6)")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit")
    args = parser.parse_args()

    print("=" * 50)
    print("ClearPath Hearings Dashboard - Scheduler")
    print("=" * 50)

    if args.once:
        print("Running scout once...")
        run_scout()
        return

    interval_seconds = args.interval * 3600
    print(f"Running scout every {args.interval} hours")
    print(f"Press Ctrl+C to stop")
    print("-" * 50)

    # Run immediately on start
    run_scout()

    try:
        while True:
            next_run = datetime.now().timestamp() + interval_seconds
            next_run_str = datetime.fromtimestamp(next_run).strftime('%Y-%m-%d %H:%M:%S')
            print(f"Next run at: {next_run_str}")

            time.sleep(interval_seconds)
            run_scout()

    except KeyboardInterrupt:
        print("\nScheduler stopped.")

if __name__ == "__main__":
    main()
