import os
import re
import ssl
import json
import difflib
import hashlib
import smtplib
import requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- CONFIGURATION ---
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")  # free key at serpapi.com - 100 searches/month
RECIPIENT_EMAIL = GMAIL_USER

# Tightened to match Eric's actual background (15 yrs RevOps / Sales Ops
# leadership, Salesforce administration & architecture, business/GTM systems)
# rather than engineering-leaning titles like "Solutions Engineer" or
# "Forward Deployed Engineer" that don't reflect his resume.
TARGET_TITLES = [
    "revenue operations",
    "sales operations",
    "revops",
    "salesforce administrator",
    "salesforce architect",
    "business systems administrator",
    "business systems analyst",
    "business systems manager",
    "business systems architect",
    "gtm systems",
    "sales systems",
    "crm administrator",
    "crm architect",
    "applications administrator",
]

SALARY_FLOOR = 170000

# A job's location text must contain at least one of these (case-insensitive)
# to be kept - this is what was missing before, which is why fully on-site
# listings in cities like Austin, London, or Bangalore were getting through.
LOCATION_KEYWORDS = ["remote", "portland"]

# Ratio threshold for treating two words as the same via fuzzy match (used
# only as a fallback for genuine typos, e.g. "architet"). This is deliberately
# strict - a loose threshold is how "Senior Staff Machine Learning Scientist,
# Assets" once wrongly matched "systems manager" ("systems"/"assets" scored
# 0.615, "manager"/"machine" scored 0.571 under the old 0.55 cutoff).
TYPO_THRESHOLD = 0.86

# Minimum word length before prefix matching kicks in (catches "admin" vs
# "administrator", "architect" vs "architecture") - short words are excluded
# so it doesn't start matching unrelated short words against each other.
MIN_PREFIX_LEN = 4

# Greenhouse and Lever don't offer a cross-company keyword search - you can
# only pull a specific company's own board. Each slug below was verified live
# against boards-api.greenhouse.io / api.lever.co before being added (not
# guessed), so every one of these actually returns postings today. Add more
# by taking the slug from a company's careers URL (e.g.
# "https://boards.greenhouse.io/figma" -> "figma",
# "https://jobs.lever.co/clickup" -> "clickup") - but note many companies,
# especially frontier AI labs (OpenAI, Perplexity, Cohere, Mistral, etc.),
# use other ATS platforms (Ashby, Rippling, Workday, custom) with no public
# API, so they can't be added this way.
GREENHOUSE_COMPANIES = [
    # Core SaaS / dev tools / infra
    "figma", "airtable", "asana", "webflow", "databricks", "elastic",
    "mongodb", "gitlab", "pagerduty", "fastly", "netlify", "cloudflare",
    "vercel", "datadog",
    # GTM / RevOps / Sales tooling - high-density for RevOps, Sales Ops,
    # Salesforce, and Business Systems roles
    "salesloft", "attentive", "postscript", "braze", "iterable", "klaviyo",
    "launchdarkly", "amplitude", "calendly",
    # AI / data platform
    "anthropic", "scaleai", "labelbox", "dataiku", "fivetran", "hightouch",
    # Fintech / marketplaces
    "affirm", "brex", "carta", "gusto", "checkr", "coinbase",
    # Other large tech / marketplaces
    "apolloio", "instacart", "pinterest", "reddit",
]
LEVER_COMPANIES = ["aircall", "clari"]

# --- STATUS TRACKING ---
# tracker.json lives next to this script and persists across runs (in the
# GitHub Actions setup, the workflow commits it back to the repo after every
# run). A job you've already triaged with triage.py won't be re-sent the
# next day - only brand-new postings, or ones still sitting at "new" because
# you haven't triaged them yet, go out in the digest.
TRACKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracker.json")
STATUS_NEW = "new"


def _words(text):
    """Extracts lowercase alphabetic word tokens, splitting on any
    punctuation (/, -, commas, parens) as well as whitespace."""
    return re.findall(r"[a-z]+", text.lower())


def _word_matches(phrase_word, title_word):
    if phrase_word == title_word:
        return True
    if len(phrase_word) >= MIN_PREFIX_LEN and len(title_word) >= MIN_PREFIX_LEN:
        if title_word.startswith(phrase_word) or phrase_word.startswith(title_word):
            return True
    return difflib.SequenceMatcher(None, phrase_word, title_word).ratio() >= TYPO_THRESHOLD


def fuzzy_title_match(title, phrase):
    """
    True if every word in `phrase` has a matching word somewhere in `title`
    (exact, a shared prefix of at least MIN_PREFIX_LEN chars - e.g. "admin"
    vs "administrator" - or a near-exact fuzzy match for typos). Words don't
    need to be adjacent or in order.
    """
    title_words = _words(title)
    phrase_words = _words(phrase)
    return all(
        any(_word_matches(pw, tw) for tw in title_words)
        for pw in phrase_words
    )


def title_matches_targets(title):
    return any(fuzzy_title_match(title, term) for term in TARGET_TITLES)


def location_matches_target(location):
    """True if the listing is remote or based in/around Portland, OR."""
    if not location:
        return False
    loc = location.lower()
    return any(keyword in loc for keyword in LOCATION_KEYWORDS)


def job_key(job):
    """
    A stable ID for a posting so it can be tracked across runs. Uses the
    source's own job/posting id when available (set as "source_id" by the
    fetch_* functions); falls back to a hash of title+company+url for
    sources that don't give us one (e.g. a Google Jobs result with no id).
    """
    if job.get("source_id"):
        return job["source_id"]
    raw = f"{job.get('title','')}|{job.get('company','')}|{job.get('url','')}"
    return "hash:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load_tracker():
    if os.path.exists(TRACKER_PATH):
        try:
            with open(TRACKER_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Could not read tracker.json ({e}) - starting a fresh one.")
    return {}


def save_tracker(tracker):
    with open(TRACKER_PATH, "w") as f:
        json.dump(tracker, f, indent=2, sort_keys=True)


def apply_tracker(jobs, tracker):
    """
    Cross-references today's matches against tracker.json:
      - A job seen for the first time is recorded with status "new" and
        goes in today's digest.
      - A job already tracked but still "new" (you haven't triaged it with
        triage.py yet) goes in today's digest again - you haven't made a
        call on it.
      - A job you've already triaged (status "applied", "reviewing", or
        "not_fit") is left out, so it stops cluttering future digests.
    Mutates `tracker` in place; returns the list of jobs to actually send.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    to_send = []
    for job in jobs:
        key = job_key(job)
        entry = tracker.get(key)
        if entry is None:
            tracker[key] = {
                "title": job["title"],
                "company": job["company"],
                "url": job["url"],
                "status": STATUS_NEW,
                "first_seen": today,
                "last_seen": today,
            }
            to_send.append(job)
        else:
            entry["last_seen"] = today
            entry["title"] = job["title"]  # keep in sync if a title gets edited
            entry["url"] = job["url"]
            if entry.get("status", STATUS_NEW) == STATUS_NEW:
                to_send.append(job)
    return to_send


def parse_salary_estimate(text):
    """
    Pulls the highest dollar figure out of a free-text salary string, e.g.
    "$180,000 - $210,000 a year" -> 210000, "$150K - $180K" -> 180000.
    Returns None if no number is found.
    """
    if not text:
        return None
    matches = re.findall(r"\$?([\d,]+(?:\.\d+)?)\s*[kK]?", text)
    values = []
    for raw in matches:
        cleaned = raw.replace(",", "")
        if not cleaned or cleaned == ".":
            continue
        value = float(cleaned)
        if "k" in text.lower() and value < 1000:
            value *= 1000
        if value >= 1000:  # ignore stray small numbers (years, counts, etc.)
            values.append(value)
    return max(values) if values else None


def fetch_live_google_jobs():
    """Queries SerpAPI's Google Jobs engine (aggregates LinkedIn, Indeed, and more)."""
    if not SERPAPI_KEY:
        print("SERPAPI_KEY not set - skipping Google Jobs search.")
        return []

    print("Fetching live jobs via Google Jobs (SerpAPI)...")
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_jobs",
        "q": (
            '("Solutions Architect" OR "GTM Engineer" OR "Salesforce Administrator" '
            'OR "Revenue Operations Engineer") ("Remote" OR "Portland")'
        ),
        "location": "United States",
        "api_key": SERPAPI_KEY,
    }

    try:
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"Error fetching from Google Jobs API: {e}")
        return []

    jobs = []
    for job in data.get("jobs_results", []):
        title = job.get("title", "")
        company = job.get("company_name", "Unknown Company")
        location = job.get("location", "Remote")
        salary_text = job.get("detected_extensions", {}).get("salary")

        apply_options = job.get("apply_options", [])
        job_url = apply_options[0].get("link") if apply_options else None
        if not job_url:
            query = requests.utils.quote(f"{title} {company}")
            job_url = f"https://www.google.com/search?q={query}"

        google_job_id = job.get("job_id")

        jobs.append({
            "title": title,
            "company": company,
            "location": location,
            "salary_range": salary_text or "Not listed",
            "base_salary_est": parse_salary_estimate(salary_text),
            "url": job_url,
            "description": (job.get("description") or job.get("snippet") or "")[:280],
            "recruiter_hint": (
                f"Search LinkedIn for '{company}' + 'Recruiter' or "
                f"'Director of Business Systems' for a warm intro."
            ),
            "source": "Google Jobs",
            "source_id": f"googlejobs:{google_job_id}" if google_job_id else None,
        })
    return jobs


def fetch_greenhouse_jobs(companies):
    """Pulls open roles directly from each company's public Greenhouse board API."""
    jobs = []
    for slug in companies:
        try:
            resp = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                timeout=15,
            )
            resp.raise_for_status()
            for job in resp.json().get("jobs", []):
                jobs.append({
                    "title": job.get("title", ""),
                    "company": slug,
                    "location": job.get("location", {}).get("name", "Remote"),
                    "salary_range": "Not listed",
                    "base_salary_est": None,
                    "url": job.get("absolute_url", ""),
                    "description": "",
                    "recruiter_hint": f"Check {slug}'s LinkedIn for a GTM/Business Systems recruiter.",
                    "source": "Greenhouse",
                    "source_id": f"greenhouse:{slug}:{job.get('id')}",
                })
        except Exception as e:
            print(f"Greenhouse lookup failed for '{slug}': {e}")
    return jobs


def fetch_lever_jobs(companies):
    """Pulls open roles directly from each company's public Lever board API."""
    jobs = []
    for slug in companies:
        try:
            resp = requests.get(
                f"https://api.lever.co/v0/postings/{slug}?mode=json",
                timeout=15,
            )
            resp.raise_for_status()
            for job in resp.json():
                jobs.append({
                    "title": job.get("text", ""),
                    "company": slug,
                    "location": (job.get("categories") or {}).get("location", "Remote"),
                    "salary_range": "Not listed",
                    "base_salary_est": None,
                    "url": job.get("hostedUrl", ""),
                    "description": "",
                    "recruiter_hint": f"Check {slug}'s LinkedIn for a GTM/Business Systems recruiter.",
                    "source": "Lever",
                    "source_id": f"lever:{slug}:{job.get('id')}",
                })
        except Exception as e:
            print(f"Lever lookup failed for '{slug}': {e}")
    return jobs


def filter_jobs(listings):
    """
    Keeps jobs whose title fuzzy-matches TARGET_TITLES, whose location is
    remote or Portland-based, and whose salary (if known) clears SALARY_FLOOR.
    Greenhouse/Lever almost never publish salary, so an unknown salary is let
    through rather than silently dropped - you'll just see "Not listed" in
    the email and can judge for yourself.
    """
    filtered = []
    seen = set()
    for job in listings:
        key = (job["title"].strip().lower(), job["company"].strip().lower())
        if key in seen:
            continue
        if not title_matches_targets(job["title"]):
            continue
        if not location_matches_target(job.get("location", "")):
            continue
        salary_est = job.get("base_salary_est")
        if salary_est is not None and salary_est < SALARY_FLOOR:
            continue
        seen.add(key)
        filtered.append(job)
    return filtered


def generate_html_digest(jobs):
    date_str = datetime.now().strftime("%B %d, %Y")

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #222;">
        <h1>Live GTM &amp; Systems Job Digest ({date_str})</h1>
        <p>New roles matching your $170k+ salary target, location, and stack
        that you haven't triaged yet (jobs you've already marked applied,
        reviewing, or not a fit are left out):</p>
        <hr>
    """

    for job in jobs:
        source = job.get("source", "")
        source_tag = f" <span style=\"color:#888; font-size: 12px;\">[{source}]</span>" if source else ""
        html += f"""
        <div style="margin-bottom: 24px; padding: 16px; border: 1px solid #ddd; border-radius: 8px;">
            <h2 style="margin: 0 0 8px 0;">{job['title']} &mdash; {job['company']}{source_tag}</h2>
            <p style="margin: 4px 0;">Location: {job['location']} | Compensation: {job['salary_range']}</p>
            <p style="margin: 4px 0;">Overview: {job['description'] or 'No description provided.'}</p>
            <p style="margin: 4px 0;">Warm Outreach Strategy: {job['recruiter_hint']}</p>
            <a href="{job['url']}" style="display: inline-block; margin-top: 8px; padding: 8px 16px; background: #0b5cff; color: white; text-decoration: none; border-radius: 4px;">View &amp; Apply for Role &rarr;</a>
        </div>
        """

    html += """
    </body>
    </html>
    """

    return html


def send_email_digest(html_content, job_count):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{job_count} New Roles Found ($170k-$200k+ Remote/Portland)"
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL

    msg.attach(MIMEText(html_content, "html"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())
        print("Success: live job digest delivered to your Gmail inbox!")
    except Exception as e:
        print(f"Error sending email: {e}")


if __name__ == "__main__":
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("Missing Gmail environment variables! Set GMAIL_USER and GMAIL_APP_PASSWORD.")
    else:
        raw_jobs = (
            fetch_live_google_jobs()
            + fetch_greenhouse_jobs(GREENHOUSE_COMPANIES)
            + fetch_lever_jobs(LEVER_COMPANIES)
        )
        matched_jobs = filter_jobs(raw_jobs)

        tracker = load_tracker()
        new_jobs = apply_tracker(matched_jobs, tracker)
        save_tracker(tracker)
        print(f"{len(matched_jobs)} matched today's criteria; {len(new_jobs)} are new/untriaged.")

        if new_jobs:
            digest_html = generate_html_digest(new_jobs)
            send_email_digest(digest_html, len(new_jobs))
        else:
            print("Nothing new to send - everything matching today has already been seen or triaged.")