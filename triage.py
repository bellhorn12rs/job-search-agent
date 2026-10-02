#!/usr/bin/env python3
"""
Mark statuses on jobs tracked by job_agent_digest.py (applied / reviewing /
not a fit), so they stop showing up in your daily email.

Run this locally in the same folder as tracker.json:

    python3 triage.py

Then sync your changes back so the next scheduled run (on GitHub) sees them:

    git add tracker.json
    git commit -m "Triage jobs"
    git push
"""
import json
import os

TRACKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracker.json")

# letter you type -> status stored in tracker.json
VALID_STATUSES = {
    "a": "applied",
    "r": "reviewing",
    "n": "not_fit",
    "k": "new",  # "keep" - leave it as new so it shows up again tomorrow
}

STATUS_LABELS = {
    "applied": "Applied",
    "reviewing": "Reviewing",
    "not_fit": "Not a fit",
    "new": "New",
}


def load_tracker():
    if not os.path.exists(TRACKER_PATH):
        print("No tracker.json found yet - run job_agent_digest.py at least once first.")
        return {}
    with open(TRACKER_PATH) as f:
        return json.load(f)


def save_tracker(tracker):
    with open(TRACKER_PATH, "w") as f:
        json.dump(tracker, f, indent=2, sort_keys=True)


def main():
    tracker = load_tracker()
    untriaged = {k: v for k, v in tracker.items() if v.get("status", "new") == "new"}

    if not untriaged:
        print("Nothing to triage right now - every tracked job already has a status.")
        return

    items = list(untriaged.items())
    print(f"\n{len(items)} untriaged job(s):\n")
    for i, (key, job) in enumerate(items, 1):
        print(f"[{i}] {job['title']} \u2014 {job['company']}")
        print(f"    {job['url']}")

    print("\nFor each one you want to update, type its number followed by a letter:")
    print("  a = applied    r = reviewing    n = not a fit    k = keep as new")
    print("Example: 3a      (one at a time; press Enter on a blank line when done)\n")

    changed = 0
    while True:
        raw = input("> ").strip().lower()
        if not raw:
            break
        num_part = "".join(ch for ch in raw if ch.isdigit())
        letter_part = "".join(ch for ch in raw if ch.isalpha())
        if not num_part or letter_part not in VALID_STATUSES:
            print("  Couldn't parse that - format is <number><letter>, e.g. 3a")
            continue
        idx = int(num_part) - 1
        if idx < 0 or idx >= len(items):
            print(f"  No item #{num_part}")
            continue
        key, job = items[idx]
        new_status = VALID_STATUSES[letter_part]
        tracker[key]["status"] = new_status
        changed += 1
        print(f"  -> marked \"{job['title']}\" as {STATUS_LABELS[new_status]}")

    if changed:
        save_tracker(tracker)
        print(f"\nSaved {changed} update(s). Now run:")
        print('  git add tracker.json')
        print('  git commit -m "Triage jobs"')
        print('  git push')
    else:
        print("\nNo changes made.")


if __name__ == "__main__":
    main()