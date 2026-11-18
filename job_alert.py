#!/usr/bin/env python3
"""
Job Alert Bot — watches company career boards and pings you on WhatsApp
the moment a matching posting appears.

Usage:
  python job_alert.py               # normal run: fetch, filter, notify, save state
  python job_alert.py --dry-run     # print matches, send nothing, save nothing
  python job_alert.py --check       # confirm every board in config.yaml responds
  python job_alert.py --test-notify # send a test message to WhatsApp/Telegram

Secrets come from environment variables (GitHub Actions secrets):
  WHATSAPP_PHONE      your number with country code, e.g. +16035551234
  CALLMEBOT_APIKEY    the key CallMeBot sent you on WhatsApp
  TELEGRAM_BOT_TOKEN  (optional backup channel)
  TELEGRAM_CHAT_ID    (optional backup channel)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state.json"
TIMEOUT = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job-alert bot)", "Accept": "application/json"}
ET = timezone(timedelta(hours=-5))  # close enough for heartbeat timing


# ─────────────────────────── data model ───────────────────────────

@dataclass
class Job:
    company: str
    source: str
    job_id: str
    title: str
    location: str
    url: str
    posted: str = ""  # human-readable, best effort

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company}:{self.job_id}"

    @property
    def sig(self) -> str:
        """Same role seen via two sources (e.g. Coinbase feed + Jobright) → same signature."""
        norm = lambda x: re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
        loc = re.sub(r"^\s*(hybrid|remote|on[- ]?site|in[- ]office)\s*[-–:]\s*", "", self.location or "", flags=re.I)
        city = norm(loc.split(",")[0].split(";")[0])
        return f"sig:{norm(self.company)}|{norm(self.title)}|{city}"


# ─────────────────────────── fetchers ───────────────────────────

def _get(url: str, **kw) -> dict | list:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


def _iso_age(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return _age(dt)
    except ValueError:
        return ""


def _age(dt: datetime) -> str:
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def fetch_greenhouse(c: dict) -> list[Job]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs")
    return [
        Job(c["name"], "greenhouse", str(j["id"]), j["title"].strip(),
            (j.get("location") or {}).get("name", ""), j["absolute_url"],
            _iso_age(j.get("first_published") or j.get("updated_at")))
        for j in data.get("jobs", [])
    ]


def fetch_lever(c: dict) -> list[Job]:
    data = _get(f"https://api.lever.co/v0/postings/{c['board']}", params={"mode": "json"})
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        posted = _age(datetime.fromtimestamp(created / 1000, tz=timezone.utc)) if created else ""
        jobs.append(Job(c["name"], "lever", j["id"], j["text"].strip(),
                        cats.get("location", ""), j["hostedUrl"], posted))
    return jobs


def fetch_ashby(c: dict) -> list[Job]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{c['board']}")
    return [
        Job(c["name"], "ashby", j["id"], j["title"].strip(), j.get("location", ""),
            j["jobUrl"], _iso_age(j.get("publishedAt")))
        for j in data.get("jobs", []) if j.get("isListed", True)
    ]


def fetch_workday(c: dict, search_terms: list[str]) -> list[Job]:
    host, tenant, site = c["host"], c["tenant"], c["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    seen, jobs = set(), []
    for term in search_terms:
        body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term}
        r = requests.post(api, json=body, timeout=TIMEOUT, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
            "Accept": "application/json", "Content-Type": "application/json",
            "Accept-Language": "en-US", "Origin": f"https://{host}",
            "Referer": f"https://{host}/{site}"})
        r.raise_for_status()
        for j in r.json().get("jobPostings", []):
            path = j.get("externalPath", "")
            if not path or path in seen:
                continue
            seen.add(path)
            req_id = (j.get("bulletFields") or [path])[0]
            jobs.append(Job(c["name"], "workday", str(req_id), j.get("title", "").strip(),
                            j.get("locationsText", ""), f"https://{host}/{site}{path}",
                            j.get("postedOn", "")))
    return jobs


def fetch_amazon(c: dict) -> list[Job]:
    seen, jobs = set(), []
    for q in c.get("queries", ["product manager intern"]):
        data = _get("https://www.amazon.jobs/en/search.json",
                    params={"base_query": q, "sort": "recent", "result_limit": 100})
        for j in data.get("jobs", []):
            jid = str(j.get("id_icims") or j.get("id"))
            if jid in seen:
                continue
            seen.add(jid)
            jobs.append(Job(c["name"], "amazon", jid, j.get("title", "").strip(),
                            j.get("normalized_location") or j.get("location", ""),
                            "https://www.amazon.jobs" + j.get("job_path", ""),
                            j.get("posted_date", "")))
    return jobs


_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def fetch_jobright(c: dict) -> list[Job]:
    """Jobright's public daily internship list on GitHub (a Markdown table in the README)."""
    repo = c.get("repo", "jobright-ai/2026-Product-Management-Internship")
    branch = c.get("branch", "master")
    r = requests.get(f"https://raw.githubusercontent.com/{repo}/{branch}/README.md",
                     headers={"User-Agent": HEADERS["User-Agent"]}, timeout=TIMEOUT)
    r.raise_for_status()
    jobs, last_company = [], ""
    for line in r.text.splitlines():
        if not line.startswith("| "):
            continue
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("Company",) or cells[0].startswith("---"):
            continue
        comp_cell, title_cell, location, _model, posted = cells[:5]
        if comp_cell == "↳":
            company = last_company
        else:
            m = _MD_LINK.search(comp_cell)
            company = (m.group(1) if m else comp_cell).strip("* ")
            last_company = company
        m = _MD_LINK.search(title_cell)
        if not m:
            continue
        title, url = m.group(1).strip(), m.group(2).split("?")[0]
        jid = url.rstrip("/").rsplit("/", 1)[-1]
        jobs.append(Job(company, "jobright", jid, title, location, url, posted))
    return jobs


def workday_search_terms(cfg: dict) -> list[str]:
    # Workday has no "list everything" call, so we search a few plain-English terms.
    return cfg.get("workday_search_terms") or [
        "product manager intern", "product management intern",
        "technical program manager intern", "MBA intern",
    ]


def fetch_company(c: dict, cfg: dict) -> list[Job]:
    src = c["source"]
    if src == "greenhouse":
        return fetch_greenhouse(c)
    if src == "lever":
        return fetch_lever(c)
    if src == "ashby":
        return fetch_ashby(c)
    if src == "workday":
        return fetch_workday(c, workday_search_terms(cfg))
    if src == "amazon":
        return fetch_amazon(c)
    if src == "jobright":
        return fetch_jobright(c)
    raise ValueError(f"Unknown source '{src}' for {c.get('name')}")


# ─────────────────────────── filtering ───────────────────────────

class Matcher:
    def __init__(self, f: dict):
        comp = lambda pats: [re.compile(p, re.I) for p in (pats or [])]
        self.roles = comp(f.get("roles"))
        self.levels = comp(f.get("levels"))
        self.excl_titles = comp(f.get("exclude_titles"))
        self.excl_locs = [s.lower() for s in (f.get("exclude_locations") or [])]
        self.us_only = f.get("us_only", False)

    def __call__(self, job: Job) -> bool:
        t = job.title
        if self.roles and not any(p.search(t) for p in self.roles):
            return False
        if self.levels and not any(p.search(t) for p in self.levels):
            return False
        if any(p.search(t) for p in self.excl_titles):
            return False
        loc = (job.location or "").lower()
        # Titles often carry the location too, e.g. "Intern (London)".
        if any(x in loc or x in t.lower() for x in self.excl_locs):
            return False
        if self.us_only and job.location and not is_us(job.location):
            return False
        return True


_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    "hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
    "michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    "new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    "rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
    "west virginia|wisconsin|wyoming|district of columbia"
)
_US_NAMES_RE = re.compile(rf"\b({_US_STATES})\b|\b(usa|united states|remote)\b|d\.c\.", re.I)
# State codes and "US" are matched case-sensitively so words like ", in" don't count.
_US_CODES_RE = re.compile(
    r",\s*(A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI"
    r"|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b|\bU\.?S\.?A?\b"
)


def is_us(location: str) -> bool:
    """True if any location in the string is in the US (or remote)."""
    return bool(_US_NAMES_RE.search(location) or _US_CODES_RE.search(location))


# ─────────────────────────── notifications ───────────────────────────

def _chunks(text: str, limit: int = 1500) -> list[str]:
    out, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > limit and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        out.append(cur)
    return out


def send_whatsapp(text: str) -> bool:
    phone, key = os.getenv("WHATSAPP_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if not (phone and key):
        return False
    ok = True
    for part in _chunks(text):
        url = (f"https://api.callmebot.com/whatsapp.php?phone={quote_plus(phone)}"
               f"&text={quote_plus(part)}&apikey={quote_plus(key)}")
        try:
            r = requests.get(url, timeout=TIMEOUT)
            if r.status_code != 200 or "error" in r.text.lower()[:300]:
                print(f"  ! WhatsApp send failed ({r.status_code}): {r.text[:200]}")
                ok = False
        except requests.RequestException as e:
            print(f"  ! WhatsApp send error: {e}")
            ok = False
        time.sleep(4)  # CallMeBot is a shared free service; don't hammer it
    return ok


def send_telegram(text: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    ok = True
    for part in _chunks(text, 3800):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": part.replace("*", ""),
                                    "disable_web_page_preview": True}, timeout=TIMEOUT)
            ok &= r.ok
        except requests.RequestException as e:
            print(f"  ! Telegram send error: {e}")
            ok = False
    return ok


def notify(text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print("\n----- would send -----\n" + text + "\n-----------------------")
        return True
    wa = send_whatsapp(text)
    tg = send_telegram(text)
    if not (os.getenv("WHATSAPP_PHONE") or os.getenv("TELEGRAM_BOT_TOKEN")):
        print("  ! No notification channel configured (set WHATSAPP_PHONE + CALLMEBOT_APIKEY).")
    return wa or tg


def _is_stale(posted: str, max_days: int = 60) -> bool:
    """Evergreen postings keep their original date (e.g. "1188d ago"); hide those."""
    m = re.fullmatch(r"(\d+)d ago", posted)
    return bool(m and int(m.group(1)) > max_days)


def format_job(j: Job) -> str:
    where = f"{j.company} · {j.location}" if j.location else j.company
    lines = [f"*{j.title}*", where]
    if j.posted and not _is_stale(j.posted):
        lines.append(f"Posted {j.posted}")
    lines.append(j.url)
    return "\n".join(lines)


def build_messages(new_jobs: list[Job], max_individual: int) -> list[str]:
    if not new_jobs:
        return []
    if len(new_jobs) <= max_individual:
        return [f"🚨 New role\n\n{format_job(j)}" for j in new_jobs]
    body = "\n\n".join(format_job(j) for j in new_jobs)
    return [f"🚨 {len(new_jobs)} new roles\n\n{body}"]


# ─────────────────────────── state ───────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        s = json.loads(STATE_PATH.read_text())
    else:
        s = {}
    s.setdefault("seen", {})
    s.setdefault("initialized", [])
    s.setdefault("last_heartbeat", "")
    s.setdefault("errors", {})
    return s


def save_state(s: dict) -> None:
    STATE_PATH.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")


def company_id(c: dict) -> str:
    return f"{c['source']}:{c['name']}"


# ─────────────────────────── main flows ───────────────────────────

def run(cfg: dict, dry_run: bool) -> int:
    state = load_state()
    match = Matcher(cfg.get("filters", {}))
    ncfg = cfg.get("notify", {})
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_jobs: list[Job] = []
    first_seen_jobs: list[Job] = []
    errors: list[str] = []
    checked = 0

    for c in cfg.get("companies", []):
        cid = company_id(c)
        try:
            jobs = fetch_company(c, cfg)
            checked += 1
            state["errors"].pop(cid, None)
            state.setdefault("error_detail", {}).pop(cid, None)
        except Exception as e:  # one broken board must never stop the rest
            errors.append(f"{c.get('name')}: {type(e).__name__}")
            state["errors"][cid] = state["errors"].get(cid, 0) + 1
            detail = f"{type(e).__name__}: {e}"
            resp = getattr(e, "response", None)
            if resp is not None:
                detail += f" | body: {resp.text[:200]}"
            state.setdefault("error_detail", {})[cid] = detail[:400]
            print(f"  ! {c.get('name')}: {e}")
            continue

        matches = [j for j in jobs if match(j)]
        is_new_company = cid not in state["initialized"]
        fresh = []
        for j in matches:
            if j.key in state["seen"]:
                state["seen"].setdefault(j.sig, state["seen"][j.key])  # backfill older state
                continue
            state["seen"][j.key] = now_iso
            if j.sig in state["seen"]:      # already sent via another source
                continue
            state["seen"][j.sig] = now_iso
            fresh.append(j)
        print(f"{c['name']:<14} {len(jobs):>4} jobs  {len(matches):>3} match  {len(fresh):>3} new"
              + ("  (first run: baseline)" if is_new_company else ""))

        (first_seen_jobs if is_new_company else new_jobs).extend(fresh)
        if is_new_company:
            state["initialized"].append(cid)

    # Already-open roles at newly added companies: one digest, so you can catch up without a flood.
    if first_seen_jobs:
        body = "\n\n".join(format_job(j) for j in first_seen_jobs)
        notify(f"📋 Now watching new companies. {len(first_seen_jobs)} matching roles already open:\n\n{body}",
               dry_run)

    for msg in build_messages(new_jobs, ncfg.get("max_individual_messages", 5)):
        notify(msg, dry_run)

    # Daily heartbeat so silence never means "broken".
    today_et = datetime.now(ET)
    if (ncfg.get("daily_heartbeat", True)
            and today_et.hour >= ncfg.get("heartbeat_after_hour_et", 9)
            and state["last_heartbeat"] != today_et.date().isoformat()):
        failing = sorted(k.split(":", 1)[1] for k in state["errors"])
        hb = (f"✅ Job bot is running. Checked {checked} companies. "
              f"{sum(1 for k in state['seen'] if k.startswith('sig:'))} matching roles tracked so far.")
        if failing:
            hb += f"\n⚠️ Couldn't check: {', '.join(failing)} (fix or remove in config.yaml)"
        notify(hb, dry_run)
        if not dry_run:
            state["last_heartbeat"] = today_et.date().isoformat()

    if not dry_run:
        save_state(state)
    print(f"\nDone: {checked} companies checked, {len(new_jobs)} new, "
          f"{len(first_seen_jobs)} baseline, {len(errors)} errors.")
    return 0


def check(cfg: dict) -> int:
    bad = 0
    for c in cfg.get("companies", []):
        try:
            n = len(fetch_company(c, cfg))
            print(f"  OK    {c['name']:<14} ({c['source']}) {n} jobs")
        except Exception as e:
            bad += 1
            print(f"  FAIL  {c['name']:<14} ({c['source']}) {type(e).__name__}: {str(e)[:120]}")
    print(f"\n{bad} board(s) failing." if bad else "\nAll boards OK.")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(CONFIG_PATH.read_text())

    if args.check:
        return check(cfg)
    if args.test_notify:
        ok = notify("👋 Test from your job alert bot. If you can read this, WhatsApp alerts work.")
        print("Sent." if ok else "Failed: check WHATSAPP_PHONE / CALLMEBOT_APIKEY.")
        return 0 if ok else 1
    return run(cfg, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
