"""Run this to test if Gmail SMTP is working: python test_email.py"""
import smtplib, json
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

cfg        = json.loads(Path("data/config.json").read_text())
digest     = cfg.get("digest", {})
gmail_user = digest.get("gmail_user", "").strip()
app_pw     = digest.get("gmail_app_password", "").strip()

print(f"Sending from: {gmail_user}")
print(f"App password set: {'yes' if app_pw else 'NO'}")

# Send to yourself so you can verify receipt
TO = input("Send test email to (your own email): ").strip()

msg            = MIMEMultipart("alternative")
msg["Subject"] = "✅ ClearPath Dashboard — email test"
msg["From"]    = gmail_user
msg["To"]      = TO
msg.attach(MIMEText("<p>If you're reading this, email is working!</p>", "html"))

try:
    print("Connecting to smtp.gmail.com:465 ...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        print("Connected. Logging in...")
        server.login(gmail_user, app_pw)
        print("Logged in. Sending...")
        server.sendmail(gmail_user, [TO], msg.as_string())
    print(f"\n✅ Email sent to {TO} — check your inbox (and spam).")
except Exception as e:
    print(f"\n❌ Failed: {e}")
    print("\nCommon fixes:")
    print("  - App password must be from the SAME Google account as gmail_user")
    print("  - Go to myaccount.google.com/apppasswords to create a new one")
    print("  - Make sure 2FA is enabled on that Google account")
