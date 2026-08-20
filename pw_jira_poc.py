#!/usr/bin/env python3
"""
Story points -> Projectworks time entries.  Truefit POC.

Proves Truefit can run Projectworks off Jira story points without asking
engineers to track hours.

No third-party dependencies.  Python 3.9+.

    python3 pw_jira_poc.py --demo --report --verbose      # offline, fixture data
    python3 pw_jira_poc.py --config config.json           # live read, dry run
    python3 pw_jira_poc.py --config config.json --emit-mcp plan.json
    python3 pw_jira_poc.py --config config.json --apply   # REST write

Nothing writes unless --emit-mcp or --apply is passed.
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

MARKER = "PWJIRA"
STATE_FILE = ".pw_jira_state.json"
QUARTER = 0.25


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def log(msg, verbose=True):
    if verbose:
        print(msg, file=sys.stderr)


def parse_date(s):
    """Jira hands back 2026-07-14T09:12:33.000-0400.  We only want the date."""
    if not s:
        return None
    return dt.date.fromisoformat(s[:10])


def is_workday(d):
    return d.weekday() < 5


def workdays_between(start, end, workdays_only=True):
    """Inclusive on both ends.  Always returns at least one day."""
    if start is None or start > end:
        start = end
    days, cur = [], start
    while cur <= end:
        if not workdays_only or is_workday(cur):
            days.append(cur)
        cur += dt.timedelta(days=1)
    if not days:
        days = [end]
    return days


def round_quarter(h):
    return round(h / QUARTER) * QUARTER


def week_start(d):
    """Monday of the week containing d."""
    return d - dt.timedelta(days=d.weekday())


# --------------------------------------------------------------------------
# 1. conversion: points -> hours
# --------------------------------------------------------------------------

def points_to_hours(points, conv):
    """
    Lookup table, not a flat multiplier.

    Truefit gave us two numbers on the call that disagree: 2.67 hours per point,
    and "a 3 is about a day, a 5 is about a day and a half".  3 * 2.67 = 8h,
    which matches.  5 * 2.67 = 13.35h, which does not -- their own rule of thumb
    says 12.  An 8 comes out at 21.4h against a table value of 19.  A flat rate
    silently overstates the large stories, and large stories are where the
    budget actually goes.  Hence the table, with the multiplier as fallback for
    point values nobody agreed on.
    """
    table = conv.get("point_hours", {})
    key = str(int(points)) if float(points).is_integer() else str(points)
    if key in table:
        return float(table[key]), "table"
    return round_quarter(points * conv.get("fallback_rate", 2.67)), "fallback"


# --------------------------------------------------------------------------
# 2. allocation: hours -> days
# --------------------------------------------------------------------------

def allocate(total_hours, start, end, cap, workdays_only=True):
    """
    Spread hours across the working days the story was actually open.

    If that produces a day over the cap, walk the window backwards a day at a
    time until it fits.  Walking forwards would log time after the story closed,
    which is worse.  The extension is flagged so it shows up in the plan rather
    than quietly changing someone's timesheet.
    """
    extended = False
    days = workdays_between(start, end, workdays_only)

    guard = 0
    while total_hours / len(days) > cap + 1e-9 and guard < 400:
        cursor = days[0] - dt.timedelta(days=1)
        while workdays_only and not is_workday(cursor):
            cursor -= dt.timedelta(days=1)
        days.insert(0, cursor)
        extended = True
        guard += 1

    # Floor to the quarter hour, then hand the remainder back a quarter at a
    # time from the last day backwards.  Dumping the whole remainder on one day
    # makes that day look like someone pulled a long shift, which is exactly the
    # kind of artefact a finance manager will pick out of a timesheet.
    per = (total_hours / len(days)) // QUARTER * QUARTER
    alloc = [[d, per] for d in days]

    remaining = round(total_hours - per * len(days), 4)
    i = len(alloc) - 1
    while remaining >= QUARTER - 1e-9:
        alloc[i][1] = round(alloc[i][1] + QUARTER, 4)
        remaining = round(remaining - QUARTER, 4)
        i = i - 1 if i > 0 else len(alloc) - 1
    if remaining > 1e-9:
        alloc[-1][1] = round(alloc[-1][1] + remaining, 4)

    alloc = [(d, round(h, 4)) for d, h in alloc if h > 0]
    return alloc, extended


# --------------------------------------------------------------------------
# 3. resolution: Jira identities -> Projectworks IDs
# --------------------------------------------------------------------------

def resolve_person(fields, mapping):
    a = fields.get("assignee") or {}
    email = (a.get("emailAddress") or "").lower()
    acct = a.get("accountId") or ""
    name = a.get("displayName") or email or acct or "unassigned"

    if not (email or acct):
        return None, name, "no assignee on the issue"

    by_acct = mapping.get("people_by_account_id", {})
    if acct and acct in by_acct:
        return by_acct[acct], name, None

    by_email = {k.lower(): v for k, v in mapping.get("people_map", {}).items()}
    if email and email in by_email:
        return by_email[email], name, None

    return None, name, f"no Projectworks user mapped to {email or acct}"


def resolve_timecode(fields, mapping, source="parent"):
    """
    Truefit budgets by practice -- Design, Product, Engineering, Quality -- and
    those practices are their Jira epics.  So parent epic is the timecode.
    """
    label = None
    if source == "parent":
        parent = fields.get("parent") or {}
        label = (parent.get("fields") or {}).get("summary") or parent.get("key")
    elif source == "component":
        comps = fields.get("components") or []
        label = comps[0].get("name") if comps else None
    elif source == "labels":
        labels = fields.get("labels") or []
        label = labels[0] if labels else None

    tmap = mapping.get("timecode_map", {})
    if label and label in tmap:
        return tmap[label], label, None

    # case-insensitive second pass
    if label:
        lower = {k.lower(): (v, k) for k, v in tmap.items()}
        if label.lower() in lower:
            tid, orig = lower[label.lower()]
            return tid, orig, None

    if "_default" in tmap:
        return tmap["_default"], f"{label or 'no epic'} -> _default", None

    return None, label or "no epic", f"no timecode mapped to {label or 'missing epic'}"


def first_in_progress(issue):
    """Earliest transition into an in-flight status, from the changelog."""
    inflight = {"in progress", "in development", "doing", "in review"}
    best = None
    histories = ((issue.get("changelog") or {}).get("histories")) or []
    for h in histories:
        for item in h.get("items", []):
            if item.get("field", "").lower() != "status":
                continue
            if (item.get("toString") or "").lower() in inflight:
                d = parse_date(h.get("created"))
                if d and (best is None or d < best):
                    best = d
    return best


# --------------------------------------------------------------------------
# 4. plan construction
# --------------------------------------------------------------------------

def fingerprint(points, user_id, timecode_id, alloc):
    payload = json.dumps(
        [points, user_id, timecode_id, [[d.isoformat(), h] for d, h in alloc]],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def build_plan(issues, cfg, state, locked_weeks=None):
    """
    One action per Jira issue.  Verbs:
      CREATE   first time we've seen this issue
      UPDATE   issue changed since last run (re-pointed, reassigned, moved)
      SKIP     nothing changed -- safe to re-run
      BLOCKED  cannot be converted, with a reason
    """
    conv = cfg["conversion"]
    mapping = cfg["mapping"]
    jira_cfg = cfg["jira"]
    sp_field = jira_cfg.get("story_points_field", "customfield_10016")
    tc_source = jira_cfg.get("timecode_source", "parent")
    cap = conv.get("max_hours_per_day", 8)
    workdays_only = conv.get("workdays_only", True)
    locked_weeks = locked_weeks or {}

    actions = []
    for issue in issues:
        key = issue.get("key")
        f = issue.get("fields", {}) or {}
        summary = f.get("summary", "")
        prior = state.get(key)

        def blocked(reason):
            actions.append({
                "key": key, "summary": summary, "verb": "BLOCKED",
                "reason": reason, "points": f.get(sp_field),
                "person": None, "user_id": None,
                "timecode": None, "timecode_id": None,
                "hours": 0, "days": [], "stale_dates": [],
                "extended": False, "rate_source": None,
            })

        points = f.get(sp_field)
        if points in (None, 0):
            blocked("no story points -- nothing to convert")
            continue

        user_id, person, err = resolve_person(f, mapping)
        if err:
            blocked(err)
            continue

        timecode_id, timecode, err = resolve_timecode(f, mapping, tc_source)
        if err:
            blocked(err)
            continue

        hours, rate_source = points_to_hours(points, conv)

        end = parse_date(f.get("resolutiondate")) or parse_date(f.get("updated"))
        if end is None:
            blocked("no resolution date -- cannot place the time on a calendar")
            continue
        start = first_in_progress(issue)

        if conv.get("date_strategy", "spread") == "resolution":
            alloc, extended = [(end, hours)], False
        else:
            alloc, extended = allocate(hours, start, end, cap, workdays_only)

        lw = sorted({week_start(d).isoformat() for d, _ in alloc})
        hit = [w for w in lw if locked_weeks.get((user_id, w))]
        if hit:
            blocked(f"timesheet locked for week starting {hit[0]}")
            continue

        h = fingerprint(points, user_id, timecode_id, alloc)
        if prior and prior.get("hash") == h:
            verb = "SKIP"
        elif prior:
            verb = "UPDATE"
        else:
            verb = "CREATE"

        prior_days = (prior or {}).get("days", [])
        new_dates = {d.isoformat() for d, _ in alloc}
        stale = [d for d, _ in prior_days if d not in new_dates]

        actions.append({
            "key": key, "summary": summary, "verb": verb, "reason": None,
            "points": points, "person": person, "user_id": user_id,
            "timecode": timecode, "timecode_id": timecode_id,
            "hours": round(hours, 2), "days": alloc, "stale_dates": stale,
            "extended": extended, "rate_source": rate_source, "hash": h,
        })

    return actions


# --------------------------------------------------------------------------
# 5. output: MCP plan payload
# --------------------------------------------------------------------------

def to_mcp_changes(actions):
    """
    Payload for plan_time_entry_changes.  The Projectworks MCP server enforces
    plan-then-apply: this goes to plan_time_entry_changes, which returns a
    planId, and only then does apply_time_entry_changes commit it.  Nothing can
    write without a human seeing the plan first -- that is the answer to
    "synthetic time entries are about to touch our invoicing".
    """
    changes = []
    for a in actions:
        if a["verb"] in ("BLOCKED", "SKIP"):
            continue
        # A re-point can shorten the span.  Clear the dates we no longer own
        # before writing, or the old hours linger and the totals double up.
        for d in a.get("stale_dates", []):
            changes.append({
                "operation": "clear",
                "userId": a["user_id"],
                "timecodeId": a["timecode_id"],
                "date": f"{d}T00:00:00Z",
            })
        for d, h in a["days"]:
            changes.append({
                "operation": "log",
                "userId": a["user_id"],
                "timecodeId": a["timecode_id"],
                "date": f"{d.isoformat()}T00:00:00Z",
                "hours": h,
                "comment": f"{a['key']} {a['summary']} [{MARKER}:{a['key']}]",
            })
    return {"changes": changes}


# --------------------------------------------------------------------------
# 6. reports
# --------------------------------------------------------------------------

def report(actions, cfg, out=sys.stdout):
    conv = cfg["conversion"]
    cap = conv.get("max_hours_per_day", 8)
    std_week = conv.get("standard_week_hours", 40)
    expected_points = conv.get("expected_points_per_week", 12)
    w = out.write

    counts = {}
    for a in actions:
        counts[a["verb"]] = counts.get(a["verb"], 0) + 1

    w("\n" + "=" * 74 + "\n")
    w("PLAN\n")
    w("=" * 74 + "\n")
    w(f"{'issue':<9} {'pts':>4} {'hrs':>6} {'days':>5}  {'person':<16} "
      f"{'timecode':<13} verb\n")
    w("-" * 74 + "\n")
    for a in actions:
        if a["verb"] == "BLOCKED":
            w(f"{a['key']:<9} {'-':>4} {'-':>6} {'-':>5}  {'-':<16} {'-':<13} "
              f"BLOCKED  {a['reason']}\n")
            continue
        flag = ""
        if a["extended"]:
            flag += " [span extended to respect daily cap]"
        if a["rate_source"] == "fallback":
            flag += " [fallback rate -- no agreed table value]"
        if a["stale_dates"]:
            flag += f" [clears {len(a['stale_dates'])} stale date(s)]"
        w(f"{a['key']:<9} {a['points']:>4} {a['hours']:>6.2f} "
          f"{len(a['days']):>5}  {(a['person'] or '')[:16]:<16} "
          f"{(a['timecode'] or '')[:13]:<13} {a['verb']}{flag}\n")

    w("\n" + " ".join(f"{k}={v}" for k, v in sorted(counts.items())) + "\n")

    # ---- daily load ----
    daily = {}
    for a in actions:
        if a["verb"] in ("BLOCKED", "SKIP"):
            continue
        for d, h in a["days"]:
            daily[(a["person"], d)] = daily.get((a["person"], d), 0) + h

    over = {k: v for k, v in daily.items() if v > cap + 1e-9}
    w("\n" + "=" * 74 + "\n")
    w(f"DAILY LOAD CHECK (cap {cap}h)\n")
    w("=" * 74 + "\n")
    if not over:
        w(f"No person exceeds {cap}h on any single day.\n")
    else:
        w("Stories overlap in time, so a per-story cap does not guarantee a\n"
          "per-day cap.  These days need attention:\n\n")
        for (p, d), v in sorted(over.items(), key=lambda x: (-x[1], x[0][0])):
            w(f"  {p:<18} {d}  {v:>6.2f}h\n")

    # ---- utilisation ceiling ----
    weekly = {}
    for a in actions:
        if a["verb"] in ("BLOCKED", "SKIP"):
            continue
        for d, h in a["days"]:
            k = (a["person"], week_start(d).isoformat())
            weekly[k] = weekly.get(k, 0) + h

    ceiling, _ = points_to_hours(expected_points, conv)
    w("\n" + "=" * 74 + "\n")
    w("UTILISATION CEILING\n")
    w("=" * 74 + "\n")
    w(f"Truefit expects {expected_points} points per person per week.\n")
    w(f"At the agreed conversion that is {ceiling:.2f} hours.\n")
    w(f"A standard week is {std_week}h, so points-derived time can never show\n"
      f"more than {ceiling / std_week * 100:.1f}% utilisation.  If Jira is the only\n"
      f"source of time, utilisation and capacity reporting are capped by the\n"
      f"conversion, not measured from the work.  Budget vs Actuals is unaffected --\n"
      f"it reads hours against budget and both sides come from the same place.\n\n")
    w(f"{'person':<18} {'week of':<12} {'hours':>7}  {'vs ' + str(std_week) + 'h':>8}\n")
    w("-" * 50 + "\n")
    for (p, wk), v in sorted(weekly.items(), key=lambda x: (x[0][1], x[0][0])):
        w(f"{p[:18]:<18} {wk:<12} {v:>7.2f}  {v / std_week * 100:>7.0f}%\n")

    # ---- timecode rollup ----
    by_tc = {}
    for a in actions:
        if a["verb"] in ("BLOCKED", "SKIP"):
            continue
        by_tc[a["timecode"]] = by_tc.get(a["timecode"], 0) + a["hours"]

    w("\n" + "=" * 74 + "\n")
    w("HOURS BY TIMECODE  (this is what lands in Budget vs Actuals)\n")
    w("=" * 74 + "\n")
    for tc, v in sorted(by_tc.items(), key=lambda x: -x[1]):
        w(f"  {tc:<28} {v:>8.2f}h\n")
    w(f"  {'TOTAL':<28} {sum(by_tc.values()):>8.2f}h\n")

    blocked = [a for a in actions if a["verb"] == "BLOCKED"]
    if blocked:
        w("\n" + "=" * 74 + "\n")
        w("BLOCKED -- needs a decision from Truefit\n")
        w("=" * 74 + "\n")
        for a in blocked:
            w(f"  {a['key']:<9} {a['reason']}\n")
    w("\n")


# --------------------------------------------------------------------------
# 7. live Jira read
# --------------------------------------------------------------------------

def http_json(url, headers=None, method="GET", body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} on {url}\n{e.read().decode()[:800]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach {url}: {e.reason}")


def fetch_jira(jira_cfg, verbose=False):
    base = jira_cfg["base_url"].rstrip("/")
    token = base64.b64encode(
        f"{jira_cfg['email']}:{jira_cfg['api_token']}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
    fields = ",".join([
        "summary", "assignee", "parent", "resolutiondate", "updated",
        "status", "components", "labels",
        jira_cfg.get("story_points_field", "customfield_10016"),
    ])

    issues, start = [], 0
    while True:
        qs = urllib.parse.urlencode({
            "jql": jira_cfg["jql"], "maxResults": 100,
            "fields": fields, "expand": "changelog",
        })
        page = http_json(f"{base}/rest/api/3/search/jql?{qs}", headers)
        batch = page.get("issues", [])
        issues.extend(batch)
        log(f"  fetched {len(issues)}/{page.get('total', '?')} issues", verbose)
        start += len(batch)
        if not batch or start >= page.get("total", 0):
            break
    return issues


# --------------------------------------------------------------------------
# 8. live Projectworks write  (REST fallback -- MCP path is preferred)
# --------------------------------------------------------------------------

def pw_apply(actions, pw_cfg, verbose=False):
    """
    Direct REST write against the Projectworks Open API.

    VERIFY THE ENDPOINT AND PAYLOAD SHAPE against the Open API docs for the
    target tenant before running this live.  The MCP path (--emit-mcp) is the
    one to demo and the one to ship: it is plan-then-apply, so a human approves
    the batch before anything is committed.  This exists for the case where a
    customer wants the middleware to run headless on a schedule.
    """
    base = pw_cfg["base_url"].rstrip("/")
    headers = {
        "Authorization": f"Bearer {pw_cfg['api_key']}",
        "Accept": "application/json",
    }
    written = 0
    for a in actions:
        if a["verb"] in ("BLOCKED", "SKIP"):
            continue
        for d, h in a["days"]:
            body = {
                "userId": a["user_id"],
                "timecodeId": a["timecode_id"],
                "date": d.isoformat(),
                "hours": h,
                "notes": f"{a['key']} {a['summary']} [{MARKER}:{a['key']}]",
            }
            http_json(f"{base}/api/v1.0/timeentries", headers,
                      method="POST", body=body)
            written += 1
            log(f"  wrote {a['key']} {d} {h}h", verbose)
    return written


# --------------------------------------------------------------------------
# 9. fixtures -- shaped exactly like Truefit's Jira
# --------------------------------------------------------------------------

def demo_config():
    return {
        "jira": {
            "story_points_field": "customfield_10016",
            "timecode_source": "parent",
            "jql": "project = TRU AND status = Done AND resolutiondate >= -14d",
        },
        "projectworks": {"base_url": "https://projectworksappstage.com",
                         "api_key": "DEMO"},
        "conversion": {
            "point_hours": {"1": 2.67, "2": 5.33, "3": 8, "5": 12, "8": 19,
                            "13": 32},
            "fallback_rate": 2.67,
            "date_strategy": "spread",
            "max_hours_per_day": 8,
            "workdays_only": True,
            "standard_week_hours": 40,
            "expected_points_per_week": 12,
        },
        "mapping": {
            "project_map": {"TRU": 4101},
            "timecode_map": {
                "Design": 9001, "Product": 9002, "Engineering": 9003,
                "Quality": 9004, "_default": 9003,
            },
            "people_map": {
                "rmartel@truefit.io": 5501,
                "dkoenig@truefit.io": 5502,
                "aokafor@truefit.io": 5503,
            },
            "people_by_account_id": {},
        },
    }


def _issue(key, summary, points, email, name, epic, in_progress, resolved,
           account_id=None):
    changelog = {"histories": []}
    if in_progress:
        changelog["histories"].append({
            "created": f"{in_progress}T09:12:00.000-0400",
            "items": [{"field": "status", "toString": "In Progress"}],
        })
    assignee = None
    if email or account_id:
        assignee = {"emailAddress": email, "displayName": name,
                    "accountId": account_id or f"acct:{(email or '')[:6]}"}
    fields = {
        "summary": summary,
        "customfield_10016": points,
        "assignee": assignee,
        "resolutiondate": f"{resolved}T16:40:00.000-0400" if resolved else None,
        "updated": f"{resolved or in_progress}T16:40:00.000-0400",
        "status": {"name": "Done"},
    }
    if epic:
        fields["parent"] = {"key": "TRU-1", "fields": {"summary": epic}}
    return {"key": key, "fields": fields, "changelog": changelog}


def demo_issues():
    """
    Eleven issues covering the clean cases and the awkward ones.  The awkward
    ones are the point: they are the decisions Truefit has to make, and it is
    better they surface in a demo than in week three of onboarding.
    """
    return [
        # clean, single day
        _issue("TRU-101", "Fix rate card rounding on invoice preview", 3,
               "rmartel@truefit.io", "Rachel Martel", "Engineering",
               "2026-07-13", "2026-07-13"),
        # spans most of a week
        _issue("TRU-102", "Rebuild client onboarding wizard", 8,
               "rmartel@truefit.io", "Rachel Martel", "Engineering",
               "2026-07-14", "2026-07-20"),
        # large story, tight window -> daily cap forces a span extension
        _issue("TRU-103", "Migrate legacy reporting warehouse", 13,
               "dkoenig@truefit.io", "Dan Koenig", "Engineering",
               "2026-07-20", "2026-07-22"),
        # no points -- estimation was skipped
        _issue("TRU-104", "Spike: evaluate feature flag vendors", None,
               "dkoenig@truefit.io", "Dan Koenig", "Product",
               "2026-07-15", "2026-07-16"),
        # unassigned at close
        _issue("TRU-105", "Copy tweaks on pricing page", 1,
               None, None, "Design", "2026-07-15", "2026-07-15"),
        # assignee is real but not mapped to a Projectworks user
        _issue("TRU-106", "Accessibility pass on nav", 2,
               "contractor@partner.io", "Sam Whitfield", "Design",
               "2026-07-16", "2026-07-17"),
        # sub-day story
        _issue("TRU-107", "Add loading state to dashboard cards", 1,
               "aokafor@truefit.io", "Ada Okafor", "Design",
               "2026-07-17", "2026-07-17"),
        # different practice
        _issue("TRU-108", "Regression suite for billing exports", 5,
               "aokafor@truefit.io", "Ada Okafor", "Quality",
               "2026-07-15", "2026-07-17"),
        # point value nobody agreed a table row for
        _issue("TRU-109", "Replatform auth service", 21,
               "dkoenig@truefit.io", "Dan Koenig", "Engineering",
               "2026-07-06", "2026-07-17"),
        # resolved on a Saturday, opened Friday
        _issue("TRU-110", "Hotfix: timesheet export encoding", 2,
               "rmartel@truefit.io", "Rachel Martel", "Quality",
               "2026-07-17", "2026-07-18"),
        # no parent epic -> falls through to _default
        _issue("TRU-111", "Bump dependencies", 2,
               "aokafor@truefit.io", "Ada Okafor", None,
               "2026-07-20", "2026-07-21"),
    ]


def demo_locked_weeks():
    """Ada's week of 13 July is already submitted and approved in PW."""
    return {(5503, "2026-07-13"): True}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def load_state(path):
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        raw = json.load(fh)
    for v in raw.values():
        v["days"] = [(d, h) for d, h in v.get("days", [])]
    return raw


def save_state(path, state):
    with open(path, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def commit_state(actions, state):
    for a in actions:
        if a["verb"] in ("CREATE", "UPDATE"):
            state[a["key"]] = {
                "hash": a["hash"],
                "hours": a["hours"],
                "days": [[d.isoformat(), h] for d, h in a["days"]],
            }
    return state


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Convert Jira story points into Projectworks time entries.")
    p.add_argument("--config", help="path to config.json")
    p.add_argument("--demo", action="store_true",
                   help="run offline against fixture data")
    p.add_argument("--report", action="store_true",
                   help="print the plan and the sanity checks")
    p.add_argument("--emit-mcp", metavar="PATH",
                   help="write the plan_time_entry_changes payload to PATH")
    p.add_argument("--apply", action="store_true",
                   help="write time entries via the Projectworks REST API")
    p.add_argument("--state", default=STATE_FILE,
                   help=f"idempotency state file (default {STATE_FILE})")
    p.add_argument("--no-state", action="store_true",
                   help="ignore and do not update the state file")
    p.add_argument("--json", action="store_true",
                   help="dump the raw plan as JSON")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if not args.demo and not args.config:
        p.error("pass --demo or --config")

    if args.demo:
        cfg, issues = demo_config(), demo_issues()
        locked = demo_locked_weeks()
        log(f"demo mode: {len(issues)} fixture issues", args.verbose)
    else:
        with open(args.config) as fh:
            cfg = json.load(fh)
        log(f"querying Jira: {cfg['jira']['jql']}", args.verbose)
        issues = fetch_jira(cfg["jira"], args.verbose)
        locked = {}  # populate from get_timesheet before a live write

    state = {} if args.no_state else load_state(args.state)
    actions = build_plan(issues, cfg, state, locked)

    if args.report:
        report(actions, cfg)

    if args.json:
        print(json.dumps([
            {**a, "days": [[d.isoformat(), h] for d, h in a["days"]]}
            for a in actions
        ], indent=2, default=str))

    wrote = False
    if args.emit_mcp:
        payload = to_mcp_changes(actions)
        with open(args.emit_mcp, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"wrote {len(payload['changes'])} change(s) -> {args.emit_mcp}")
        print("feed this to plan_time_entry_changes, review the plan, then "
              "apply_time_entry_changes")
        wrote = True

    if args.apply:
        if args.demo:
            raise SystemExit("--apply is not available in --demo mode")
        n = pw_apply(actions, cfg["projectworks"], args.verbose)
        print(f"wrote {n} time entr{'y' if n == 1 else 'ies'} to Projectworks")
        wrote = True

    if wrote and not args.no_state:
        save_state(args.state, commit_state(actions, state))
        log(f"state updated -> {args.state}", args.verbose)
    elif not wrote:
        counts = {}
        for a in actions:
            counts[a["verb"]] = counts.get(a["verb"], 0) + 1
        print("dry run -- nothing written.  " +
              " ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    return 0


if __name__ == "__main__":
    sys.exit(main())
