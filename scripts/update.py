#!/usr/bin/env python3
"""Daily research pass for the Give Me Shelter.

Reads the news and city feeds in feeds.txt, then asks Claude (with web search)
to confirm leads and look for anything new since the last update: site
announcements, openings, closures, capacity changes, and Wilson administration
promises or policy moves on shelter, emergency housing and RV safe lots.
Proposed changes are merged into data.json and summarized in CHANGES.md, which
the GitHub workflow turns into a pull request for human review.

Standard library only. Signs in to the Claude API without a stored key, using GitHub Actions
workload identity federation (ANTHROPIC_FEDERATION_RULE_ID, ANTHROPIC_ORGANIZATION_ID,
ANTHROPIC_SERVICE_ACCOUNT_ID, optional ANTHROPIC_WORKSPACE_ID). ANTHROPIC_API_KEY, if set, is used
instead. Optional env:
  LEDGER_MODEL       (default claude-sonnet-5-5)
  LEDGER_SEARCH_TOOL (default web_search_20250305)
  LEDGER_MAX_SEARCHES (default 10)
  LEDGER_SKIP_FEEDS  set to skip reading feeds.txt
  LEDGER_FAKE_RESPONSE path to a saved model reply, for testing without the API
"""
import datetime as dt
import json
import os
import re
import sys
import email.utils
import html
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data.json")
CHANGES = os.path.join(ROOT, "CHANGES.md")
FEEDS = os.path.join(ROOT, "feeds.txt")
KEYWORDS = re.compile(r"shelter|homeless|unhoused|tiny.?house|tiny.?home|village|safe (?:lot|parking)|\bRVs?\b|encampment|KCRHA|LIHI|Nickelsville|SHARE|DESC|emergency housing|vehicle resident", re.I)

SITE_STATUSES = {"announced", "construction", "open", "closed"}
PROGRAMS = {"initiative", "accelerated", "other"}
EVENT_TYPES = {"campaign", "goal", "policy", "site", "context"}
PROMISE_STATUSES = {"missed", "revised", "in_progress", "delivered"}
UNIT_KEYS = {"shelter", "pallet", "tiny_house", "emergency_housing", "rv"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

INSTRUCTIONS = """You maintain a public, neutral tracker of shelter capacity in the City of Seattle
and of Mayor Katie Wilson's commitments on shelter, emergency housing and RV safe lots.

Today is {today}. The tracker was last updated {updated}. Search for anything new or changed
since about {since}. Check, at minimum:
- Mayor's office and city releases: wilson.seattle.gov, news.seattle.gov, content.govdelivery.com (WASEATTLE), council.seattle.gov
- Providers: LIHI (lihihousing.org), Nickelsville, SHARE/WHEEL, DESC, Everyone Deserves Housing, Chief Seattle Club, KCRHA (kcrha.org)
- Local news: Seattle Times, KUOW, KOMO, KING 5, KIRO, FOX 13, PubliCola, The Urbanist, The Stranger,
  Real Change, South Seattle Emerald, West Seattle Blog, Capitol Hill Seattle Blog, My Ballard, Axios Seattle
Look specifically for: new sites announced; sites opening; sites closing, losing leases, pausing or
cutting beds (closures matter as much as openings); changed unit counts or opening dates for sites
already tracked; new promises, goals, goal changes, executive orders, legislation, budget actions and
vetoes or signatures on shelter, emergency housing or RV policy.

Rules:
- Seattle city limits only. Skip sites elsewhere in King County.
- Every item needs at least one source URL you actually retrieved. Report only what sources state.
  Never estimate unit counts or dates. If a date is only known to the month, use the 15th and set
  "date_precision": "month".
  For a site's expected opening given only as a vague period ("first half of 2027", "spring"), set
  "expected" to the last day of that period and "expected_label" to the period in the source's words
  (the chart draws it as approximate). "End of November" is specific enough: use 2026-11-30, no label.
- "program" for a site: "initiative" if the city or operator says it is part of Wilson's shelter
  acceleration / Shelter Accelerator / Neighbor by Neighbor effort or funded by it; "accelerated" if it
  was already in development and the city says it expedited it; otherwise "other". Put the stated basis
  in program_note in plain words. Do not judge whether the claim of credit is fair.
- A closure you cannot confirm with a date goes in "watch_adds", not as a site status change.
- A project the city announces without a location or unit count goes in "unnamed_adds". When one of
  those gets a name and numbers, add it as a site and list the unnamed id in "unnamed_remove".
- Neutral, plain language. No adjectives of praise or blame.
- Every text field (notes, detail, status_note, program_note, statement, titles) is published on the
  public site. Write it for readers: state what is known, plainly, as findings. Don't narrate your
  research or reasoning ("the retrieved sources", "the tracker keeps", "I left", "only the month is
  known so"), don't list which outlet said what unless sources genuinely conflict on a fact that
  matters, and don't write notes to the reviewer there. Put uncertainty, conflicts and judgment calls
  for the reviewer in "summary" instead, which is not published. Attribute claims of credit or
  intent to whoever made them ("the mayor's office says").
- Unit categories: "shelter" = beds in a congregate shelter building (rare in this tracker; most sites are microshelter); "pallet" = pallet-style or other
  manufactured micro-units; "tiny_house" = houses in a tiny house village; "emergency_housing" = rooms or apartments in a building leased or master-leased whole
  (hotels, apartment buildings); "rv" = vehicle safe lot spaces. A site can have more than one.
- Any time the city, mayor's office or HSD states how many units or beds it has added or opened so far,
  add it to "city_count_adds" with kind "count", even if it doesn't match our list.
- Forward-looking numbers from the city ("we expect 400 units by year end") are goals: add them to
  "promise_updates" with a target, the date they were stated as "made", and status "in_progress".
  When a goal's date passes, set status "missed" or "delivered" by comparing the target with the
  units open on that date in the tracker.
- Paywalled stories (Seattle Times especially): if a story you can't read in full seems to hold a fact
  the tracker needs (a site, unit count, date, closure or goal) that no other source confirms, add it to
  "read_requests" with what you need from it. The editor will read it and reply with what it says.
  Don't request stories whose facts you've confirmed elsewhere. Three at most.
- Editor notes (below) come from the editor, who read those stories. Treat a note as a source: cite the
  story's own URL, with "(read by editor)" after its title.
- If nothing is new, return empty lists.

Editor notes on stories you asked to have read:
{notes}

Leads from news and city feeds published since {since} that mention shelter-related terms.
Use them as starting points: confirm details by searching before you rely on one. Many are irrelevant.
{leads}

Current tracker contents (ids, so you update rather than duplicate):
{snapshot}

Reply with exactly one fenced ```json block and nothing after it, shaped like:
{{
  "site_upserts": [ {{ "id": "kebab-id", "name": "...", "neighborhood": "...", "address": "...",
      "operator": "...", "units": {{"shelter": 0, "pallet": 0, "tiny_house": 0, "emergency_housing": 0, "rv": 0}},
      "status": "announced|construction|open|closed", "program": "initiative|accelerated|other",
      "program_note": "...", "dates": {{"announced": "YYYY-MM-DD|null", "opened": "YYYY-MM-DD|null",
      "closed": "YYYY-MM-DD|null", "expected": "YYYY-MM-DD|null",
      "construction": "YYYY-MM-DD construction or installation began, or null",
      "expected_label": "only for a vague period, e.g. first half of 2027"}}, "notes": "...",
      "sources": [{{"title": "...", "url": "https://..."}}] }} ],
  "event_adds": [ {{ "id": "e-YYYY-short-slug", "date": "YYYY-MM-DD", "date_precision": "day|month",
      "type": "campaign|goal|policy|site|context", "site": "site-id or omit", "title": "...",
      "short": "chart label, 22 characters max",
      "detail": "...", "sources": [...] }} ],
  "promise_updates": [ {{ "id": "existing or new id", "made": "YYYY-MM-DD", "made_label": "...",
      "text": "...", "short": "chart label, 22 characters max",
      "target": {{"units": 0, "by": "YYYY-MM-DD"}} or omit,
      "status": "missed|revised|in_progress|delivered", "status_note": "...", "sources": [...] }} ],
  "watch_adds": [ {{ "id": "kebab-id", "title": "...", "detail": "...", "sources": [...] }} ],
  "unnamed_adds": [ {{ "id": "u-short-slug", "title": "...", "timing": "...", "detail": "...", "sources": [...] }} ],
  "unnamed_remove": [ "u-..." ],
  "city_count_adds": [ {{ "id": "cc-YYYY-MM-short", "date": "YYYY-MM-DD", "kind": "count",
      "units": 0, "by": "YYYY-MM-DD the projection names, or omit", "short": "chart label like '303 by end of summer', 30 characters max",
      "statement": "what the city said, in plain words", "sources": [...] }} ],
  "read_requests": [ {{ "title": "...", "url": "https://...", "need": "the specific fact you need from it" }} ],
  "summary": "Two or three sentences for the reviewer on what changed and anything uncertain."
}}
For site_upserts on an existing id, send the full updated record."""


def snapshot(data):
    lines = ["SITES:"]
    for s in data["sites"]:
        lines.append(f"- {s['id']}: {s['name']} | {s['status']} | units {s['units']} | dates {s['dates']}")
    lines.append("PROMISES:")
    for p in data["promises"]:
        lines.append(f"- {p['id']}: {p['text']} | {p['status']}")
    lines.append("RECENT EVENTS (latest 15):")
    for e in sorted(data["events"], key=lambda e: e["date"])[-15:]:
        lines.append(f"- {e['id']} {e['date']}: {e['title']}")
    lines.append("CITY COUNTS ALREADY RECORDED:")
    for c in data.get("city_counts", []):
        lines.append(f"- {c['id']} {c['date']}: {c['kind']} {c['units']}")
    lines.append("ANNOUNCED WITHOUT DETAILS:")
    for u in data.get("unnamed", []):
        lines.append(f"- {u['id']}: {u['title']} ({u.get('timing','')})")
    lines.append("WATCH LIST:")
    for w in data.get("watch", []):
        lines.append(f"- {w['id']}: {w['title']}")
    return "\n".join(lines)


def _text(el, *names):
    for n in names:
        x = el.find(n)
        if x is not None and (x.text or x.get("href")):
            return (x.text or x.get("href") or "").strip()
    return ""


def _date(s):
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        pass
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


# Some sites (Real Change) refuse requests that don't start like a browser's.
FEED_UA = "Mozilla/5.0 (compatible; GiveMeShelter/1.0; +https://github.com/swilsonthomas-progeny/give-me-shelter)"


def parse_feed(raw):
    """Return [(date, title, link, summary)] from RSS or Atom bytes."""
    root = ET.fromstring(raw)
    A = "{http://www.w3.org/2005/Atom}"
    out = []
    for it in root.iter("item"):
        out.append((_date(_text(it, "pubDate", "{http://purl.org/dc/elements/1.1/}date")),
                    _text(it, "title"), _text(it, "link"), _text(it, "description")))
    for it in root.iter(A + "entry"):
        link = it.find(A + "link")
        out.append((_date(_text(it, A + "published", A + "updated")), _text(it, A + "title"),
                    link.get("href", "") if link is not None else "", _text(it, A + "summary", A + "content")))
    return out


def read_feeds(since):
    """Fetch every feed in feeds.txt; return (leads, failed_urls)."""
    if not os.path.exists(FEEDS):
        return [], []
    urls = [l.strip() for l in open(FEEDS, encoding="utf-8") if l.strip() and not l.lstrip().startswith("#")]
    leads, failed, seen = [], [], set()
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": FEED_UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                items = parse_feed(r.read())
        except Exception as exc:  # network errors, bad XML: report and move on
            failed.append(f"{url} ({type(exc).__name__})")
            continue
        for d, title, link, summary in items:
            if d is not None and d < since:
                continue
            clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(summary or ""))).strip()
            if not KEYWORDS.search(f"{title} {clean}") or link in seen:
                continue
            seen.add(link)
            leads.append(f"- {d or 'undated'} | {title.strip()} | {link} | {clean[:240]}")
    return leads[:80], failed


API_BASE = os.environ.get("LEDGER_API_BASE", "https://api.anthropic.com")
AUDIENCE = "https://api.anthropic.com"


def _post_json(url, payload, headers, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:2000]
        raise SystemExit(f"{url} answered {e.code}: {detail}") from None


def github_identity_token():
    """Fresh OIDC token from GitHub Actions (needs `permissions: id-token: write`)."""
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + urllib.parse.quote(AUDIENCE, safe="")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["value"]


def auth_headers():
    """Keyless by default: trade the workflow's GitHub identity token for a short-lived
    Anthropic token (workload identity federation). An API key is used only if one is set."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return {"x-api-key": key}
    needed = ["ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_SERVICE_ACCOUNT_ID"]
    missing = [n for n in needed if not os.environ.get(n)]
    if missing or not os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"):
        raise SystemExit("No credentials: set repository variables " + ", ".join(needed)
                         + " and give the workflow `id-token: write` permission (see README).")
    jwt = github_identity_token()
    try:  # log the identity claims (not the token) so a rule mismatch is easy to spot
        import base64
        seg = jwt.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        print("GitHub identity:", json.dumps({k: claims.get(k) for k in
              ("iss", "aud", "sub", "repository", "repository_owner", "repository_owner_id", "ref", "event_name")}))
    except Exception:
        pass
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
        "federation_rule_id": os.environ["ANTHROPIC_FEDERATION_RULE_ID"],
        "organization_id": os.environ["ANTHROPIC_ORGANIZATION_ID"],
        "service_account_id": os.environ["ANTHROPIC_SERVICE_ACCOUNT_ID"],
    }
    if os.environ.get("ANTHROPIC_WORKSPACE_ID"):
        payload["workspace_id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
    tok = _post_json(API_BASE + "/v1/oauth/token", payload, {})
    return {"authorization": "Bearer " + tok["access_token"]}


def call_claude(prompt):
    fake = os.environ.get("LEDGER_FAKE_RESPONSE")
    if fake:
        return open(fake, encoding="utf-8").read()
    body = {
        "model": os.environ.get("LEDGER_MODEL", "claude-sonnet-5-5"),
        "max_tokens": 8000,
        "tools": [{
            "type": os.environ.get("LEDGER_SEARCH_TOOL", "web_search_20250305"),
            "name": "web_search",
            "max_uses": int(os.environ.get("LEDGER_MAX_SEARCHES", "10")),
        }],
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {**auth_headers(), "anthropic-version": os.environ.get("LEDGER_API_VERSION", "2023-06-01")}
    resp = _post_json(API_BASE + "/v1/messages", body, headers, timeout=600)
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")


def extract_json(text):
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not blocks:
        raise ValueError("No ```json block in the reply")
    return json.loads(blocks[-1])


def ok_sources(item):
    srcs = item.get("sources") or []
    return bool(srcs) and all(str(s.get("url", "")).startswith("http") for s in srcs)


def ok_date(v):
    return v is None or (isinstance(v, str) and DATE_RE.match(v))


def check_site(s):
    errs = []
    if not re.match(r"^[a-z0-9-]+$", s.get("id", "")): errs.append("bad id")
    if s.get("status") not in SITE_STATUSES: errs.append("bad status")
    if s.get("program") not in PROGRAMS: errs.append("bad program")
    u = s.get("units") or {}
    if set(u) - UNIT_KEYS or not all(isinstance(v, int) and v >= 0 for v in u.values()): errs.append("bad units")
    d = s.get("dates") or {}
    if not all(ok_date(d.get(k)) for k in ("announced", "construction", "opened", "closed", "expected")): errs.append("bad dates")
    if s.get("status") == "open" and not d.get("opened"): errs.append("open without opened date")
    if s.get("status") == "closed" and not d.get("closed"): errs.append("closed without closed date")
    if not ok_sources(s): errs.append("no sources")
    return errs


def check_event(e):
    errs = []
    if not e.get("id"): errs.append("no id")
    if not ok_date(e.get("date")) or not e.get("date"): errs.append("bad date")
    if e.get("type") not in EVENT_TYPES: errs.append("bad type")
    if not ok_sources(e): errs.append("no sources")
    return errs


def check_promise(p):
    errs = []
    if p.get("status") not in PROMISE_STATUSES: errs.append("bad status")
    if not p.get("id") or not p.get("text"): errs.append("missing id/text")
    if not ok_sources(p): errs.append("no sources")
    return errs


def merge(data, patch):
    log, rejected = [], []
    sites = {s["id"]: s for s in data["sites"]}
    for s in patch.get("site_upserts", []):
        s.setdefault("units", {})
        for k in UNIT_KEYS:
            s["units"].setdefault(k, 0)
        errs = check_site(s)
        if errs:
            rejected.append(f"site {s.get('id')}: {', '.join(errs)}"); continue
        old = sites.get(s["id"])
        if old:
            # keep sources we already had alongside the new ones
            urls = {x["url"] for x in s["sources"]}
            s["sources"] += [x for x in old.get("sources", []) if x["url"] not in urls]
            diffs = [k for k in ("status", "units", "dates", "program", "name") if old.get(k) != s.get(k)]
            if not diffs and old.get("notes") == s.get("notes"):
                continue
            log.append(f"Updated site **{s['name']}** ({', '.join(diffs) or 'notes/sources'})")
        else:
            log.append(f"New site **{s['name']}**: {s['status']}, {sum(s['units'].values())} units")
        sites[s["id"]] = s
    data["sites"] = list(sites.values())

    have = {e["id"] for e in data["events"]}
    have_keys = {(e["date"], e["title"].lower()) for e in data["events"]}
    for e in patch.get("event_adds", []):
        errs = check_event(e)
        if errs:
            rejected.append(f"event {e.get('id')}: {', '.join(errs)}"); continue
        if e["id"] in have or (e["date"], e["title"].lower()) in have_keys:
            continue
        data["events"].append(e); have.add(e["id"])
        log.append(f"New timeline entry {e['date']}: {e['title']}")

    proms = {p["id"]: p for p in data["promises"]}
    for p in patch.get("promise_updates", []):
        errs = check_promise(p)
        if errs:
            rejected.append(f"promise {p.get('id')}: {', '.join(errs)}"); continue
        old = proms.get(p["id"])
        if old:
            merged = {**old, **p}
            if merged == old:
                continue
            if old.get("status") != merged["status"]:
                log.append(f"Promise **{p['id']}**: {old.get('status')} → {merged['status']}")
            else:
                log.append(f"Promise **{p['id']}**: note or sources updated")
            proms[p["id"]] = merged
        else:
            proms[p["id"]] = p
            log.append(f"New promise: {p['text']}")
    data["promises"] = list(proms.values())

    watch = {w["id"]: w for w in data.get("watch", [])}
    for w in patch.get("watch_adds", []):
        if not w.get("id") or not ok_sources(w):
            rejected.append(f"watch {w.get('id')}: missing id or sources"); continue
        if w["id"] not in watch:
            log.append(f"Added to closures watch list: {w.get('title')}")
        watch[w["id"]] = w
    data["watch"] = list(watch.values())

    unnamed = {u["id"]: u for u in data.get("unnamed", [])}
    for u in patch.get("unnamed_adds", []):
        if not u.get("id") or not u.get("title") or not ok_sources(u):
            rejected.append(f"unnamed {u.get('id')}: missing id, title or sources"); continue
        if u["id"] not in unnamed:
            log.append(f"Announced without details: {u['title']}")
        unnamed[u["id"]] = u
    for uid in patch.get("unnamed_remove", []):
        if unnamed.pop(uid, None):
            log.append(f"Removed from 'announced, not yet named': {uid}")
    data["unnamed"] = list(unnamed.values())

    counts = {c["id"]: c for c in data.get("city_counts", [])}
    seen = {(c["date"], c["units"], c["kind"]) for c in counts.values()}
    for c in patch.get("city_count_adds", []):
        if (not c.get("id") or c.get("kind") not in {"count", "projection"} or not isinstance(c.get("units"), int)
                or not ok_date(c.get("date")) or not c.get("date") or not ok_sources(c)):
            rejected.append(f"city count {c.get('id')}: bad id, kind, units, date or sources"); continue
        if c["id"] in counts or (c["date"], c["units"], c["kind"]) in seen:
            continue
        counts[c["id"]] = c
        log.append(f"City {c['kind']} on {c['date']}: {c['units']} units")
    data["city_counts"] = list(counts.values())
    return log, rejected


def main():
    data = json.load(open(DATA, encoding="utf-8"))
    today = dt.date.today()
    since_d = dt.date.fromisoformat(data["updated"]) - dt.timedelta(days=2)
    since = since_d.isoformat()
    leads, failed = ([], []) if os.environ.get("LEDGER_SKIP_FEEDS") else read_feeds(since_d)
    notes_path = os.environ.get("LEDGER_EDITOR_NOTES")
    notes = open(notes_path, encoding="utf-8").read().strip() if notes_path and os.path.exists(notes_path) else ""
    prompt = INSTRUCTIONS.format(today=today.isoformat(), updated=data["updated"], since=since,
                                 leads="\n".join(leads) or "(no feed items matched)", snapshot=snapshot(data),
                                 notes=notes or "(none)")
    reply = call_claude(prompt)
    patch = extract_json(reply)
    log, rejected = merge(data, patch)
    asks = [r for r in patch.get("read_requests", []) if isinstance(r, dict) and str(r.get("url", "")).startswith("http")][:3]

    if failed:
        print("Feeds that didn't load:\n" + "\n".join(failed))
    if not log and asks:
        # Nothing to change yet, but a story needs reading: still open a review to ask.
        log_only_asks = True
    else:
        log_only_asks = False
    if not log and not asks:
        print("No changes.")
        if os.path.exists(CHANGES):
            os.remove(CHANGES)
        return 0

    if not log_only_asks:
        data["updated"] = today.isoformat()
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False); f.write("\n")

    md = [f"## Proposed ledger update, {today.isoformat()}", "", patch.get("summary", "").strip(), "", "### Changes"]
    md += [f"- {line}" for line in log] or ["- None yet"]
    if asks:
        md += ["", "### Stories to read for me",
               "I couldn't read these in full. Reply in a comment on this pull request with what each one says; the next run will use your notes."]
        md += [f"- [ ] [{a.get('title') or a['url']}]({a['url']}): {a.get('need', '').strip()}" for a in asks]
    if rejected:
        md += ["", "### Dropped automatically (failed checks)"] + [f"- {r}" for r in rejected]
    if failed:
        md += ["", "### Feeds that didn't load (fix or remove in feeds.txt)"] + [f"- {f}" for f in failed]
    md += ["", "Check each change against its linked source, edit data.json in this PR if needed, then merge to publish."]
    open(CHANGES, "w", encoding="utf-8").write("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
