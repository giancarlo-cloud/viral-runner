#!/usr/bin/env python3
"""Self-healing dispatcher for the daily viral pipeline.

GitHub cron on this account skips slots unpredictably (2026-07-22: pulse fired,
scan/fetch/produce all silently missed). This script makes any surviving
trigger repair the whole day: for each stage past its UTC slot with no run yet
today, dispatch it — waiting for upstream stages to finish first so the shared
"viral-state" concurrency group never cancels a queued sibling.

Runs from: pulse workflow (GH_TOKEN env), Mac launchd watchdog (token file),
or by hand. Idempotent — a stage that already ran today is never re-dispatched.
"""
import json, os, time, urllib.request
from datetime import datetime, timezone

REPO = "giancarlo-cloud/viral-runner"
STAGES = [  # (workflow file, UTC slot, wait for completion before next stage)
    ("scan.yml", "04:20", True),
    ("fetchreels.yml", "04:50", True),
    ("produce.yml", "05:20", False),
]
WAIT_TIMEOUT = 1500


def _token():
    t = os.environ.get("GH_TOKEN", "").strip()
    return t or open(os.path.expanduser("~/.gh-reminder-check/.token")).read().strip()


def _api(method, path):
    req = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=b'{"ref":"main"}' if method == "POST" else None,
        headers={"Authorization": f"token {_token()}",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        return json.loads(body) if body else None


def runs_today(wf, day):
    d = _api("GET", f"/repos/{REPO}/actions/workflows/{wf}/runs?created=%3E%3D{day}&per_page=10")
    return d.get("workflow_runs", [])


def wait_done(wf, day):
    t0 = time.time()
    time.sleep(20)
    while time.time() - t0 < WAIT_TIMEOUT:
        runs = runs_today(wf, day)
        if runs and all(r["status"] == "completed" for r in runs):
            return True
        time.sleep(30)
    return False


def telegram(text):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text}).encode(),
            headers={"Content-Type": "application/json"}), timeout=30)
    except Exception as e:
        print(f"telegram failed: {e}")


def main():
    now = datetime.now(timezone.utc)
    day, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    healed = []
    for wf, slot, wait in STAGES:
        if hhmm < slot:
            print(f"{wf}: slot {slot} not reached yet — stopping")
            break
        runs = runs_today(wf, day)
        if runs:
            active = sum(1 for r in runs if r["status"] != "completed")
            print(f"{wf}: covered today ({len(runs)} runs, {active} active)")
        else:
            _api("POST", f"/repos/{REPO}/actions/workflows/{wf}/dispatches")
            healed.append(wf.replace(".yml", ""))
            print(f"{wf}: MISSED its {slot} slot — dispatched")
        if wait and not wait_done(wf, day):
            print(f"{wf}: still running after {WAIT_TIMEOUT}s — stopping to keep order")
            break
    if healed:
        telegram(f"Watchdog {day}: GitHub cron missed {', '.join(healed)} — "
                 "re-dispatched in order (pulse/Mac self-heal).")
    print(f"healed: {healed or 'nothing — all on time'}")


if __name__ == "__main__":
    main()
