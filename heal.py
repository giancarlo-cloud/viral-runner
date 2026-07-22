#!/usr/bin/env python3
"""Self-healing dispatcher for the daily viral pipeline.

GitHub cron on this account skips slots unpredictably, and Instagram
intermittently blocks runner IPs — so coverage is judged by OUTCOMES, not by
"a run happened":

  scan     covered when today's scan log is clean (no [SCAN BLOCKED]);
           while blocked, each heal pass re-rolls a fresh runner IP (cap 6/day)
  fetch    covered when a fetch run exists after the last clean scan
  produce  covered when >=TARGET archive entries have replicated == today
           (cap 4 runs/day; produce.py itself stops at the daily target)

Stages dispatch in order; heal waits for scan/fetch to finish before the next
stage so the shared "viral-state" concurrency group never cancels a queued
sibling. Runs from: pulse workflow (GH_TOKEN env), Mac launchd watchdog
(token file), or by hand. Telegrams whenever it takes an action.
"""
import base64, json, os, time, urllib.request
from datetime import datetime, timezone

RUNNER = "giancarlo-cloud/viral-runner"
STATE = "giancarlo-cloud/viral-agent"
TARGET = 3
SCAN_CAP, FETCH_CAP, PRODUCE_CAP = 6, 6, 4
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


def _file(repo, path):
    try:
        d = _api("GET", f"/repos/{repo}/contents/{path}")
        return base64.b64decode(d["content"]).decode()
    except Exception:
        return None


def runs_today(wf, day):
    d = _api("GET", f"/repos/{RUNNER}/actions/workflows/{wf}/runs?created=%3E%3D{day}&per_page=20")
    return d.get("workflow_runs", [])


def dispatch(wf):
    _api("POST", f"/repos/{RUNNER}/actions/workflows/{wf}/dispatches")


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


def shipped_today(day):
    raw = _file(STATE, "data/archive.json")
    if not raw:
        return 0
    return sum(1 for e in json.loads(raw).values() if e.get("replicated") == day)


def latest_end(runs):
    return max((r.get("updated_at", "") for r in runs), default="")


def main():
    now = datetime.now(timezone.utc)
    day, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    actions = []

    # --- scan: clean log or re-roll ---
    if hhmm < "04:20":
        print("before first slot — nothing to do")
        return
    scan_runs = runs_today("scan.yml", day)
    scan_log = _file(STATE, f"logs/scan-{day}.log") or ""
    scan_clean = bool(scan_log) and "[SCAN BLOCKED]" not in scan_log
    active = any(r["status"] != "completed" for r in scan_runs)
    if scan_clean:
        print(f"scan: clean ({len(scan_runs)} runs)")
    elif active:
        print("scan: run in progress — waiting")
        wait_done("scan.yml", day)
        scan_clean = "[SCAN BLOCKED]" not in (_file(STATE, f"logs/scan-{day}.log") or "")
    elif len(scan_runs) >= SCAN_CAP:
        print(f"scan: blocked all {len(scan_runs)} attempts — cap reached")
    else:
        dispatch("scan.yml")
        actions.append(f"scan re-roll #{len(scan_runs) + 1} (IP blocked)"
                       if scan_runs else "scan (missed slot)")
        print(f"scan: dispatched (attempt {len(scan_runs) + 1})")
        if wait_done("scan.yml", day):
            scan_clean = "[SCAN BLOCKED]" not in (_file(STATE, f"logs/scan-{day}.log") or "")
            print(f"scan: finished, clean={scan_clean}")

    # --- fetch: must postdate the last clean scan ---
    if hhmm >= "04:50":
        fetch_runs = runs_today("fetchreels.yml", day)
        scan_runs = runs_today("scan.yml", day)
        need_fetch = not fetch_runs or (
            scan_clean and latest_end(fetch_runs) < latest_end(scan_runs))
        if need_fetch and len(fetch_runs) < FETCH_CAP:
            if any(r["status"] != "completed" for r in scan_runs):
                print("fetch: scan still active — next pulse handles it")
            else:
                dispatch("fetchreels.yml")
                actions.append("fetch-reels")
                print("fetch: dispatched")
                wait_done("fetchreels.yml", day)
        else:
            print(f"fetch: covered ({len(fetch_runs)} runs)")

    # --- produce: judged by shipped videos, not runs ---
    if hhmm >= "05:20":
        done = shipped_today(day)
        produce_runs = runs_today("produce.yml", day)
        if done >= TARGET:
            print(f"produce: target met ({done}/{TARGET})")
        elif any(r["status"] != "completed" for r in produce_runs):
            print("produce: run in progress")
        elif len(produce_runs) >= PRODUCE_CAP:
            print(f"produce: {done}/{TARGET} shipped, cap {PRODUCE_CAP} reached")
        else:
            dispatch("produce.yml")
            actions.append(f"produce ({done}/{TARGET} shipped so far)")
            print(f"produce: dispatched (run #{len(produce_runs) + 1}, {done}/{TARGET} shipped)")

    if actions:
        telegram(f"Watchdog {day} {hhmm}Z: " + "; ".join(actions))
    print(f"actions: {actions or 'none — all covered'}")


if __name__ == "__main__":
    main()
