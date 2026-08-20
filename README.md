# Story points to Projectworks time — Truefit POC

Answers the question Tyler called the linchpin on the 20 July call: can Truefit
run Projectworks off Jira story points without asking engineers to track hours?

**Yes — with four decisions to make.** Points convert cleanly, entries land
against the right person, project and timecode, and Budget vs Actuals populates.
The POC also shows that a naive conversion produces timesheets that won't
survive scrutiny, which is the more useful half of the finding.

## Run it

No dependencies. Python 3.9+.

```bash
python3 pw_jira_poc.py --demo --report --verbose
```

That's the one to screen-share. Eleven representative issues, including the
awkward ones, with the plan, the daily load check, the utilisation ceiling and
the timecode rollup.

Live against Truefit's Jira, once they hand over a token:

```bash
cp config.example.json config.json      # fill in tokens and the three ID maps
python3 pw_jira_poc.py --config config.json --report          # dry run
python3 pw_jira_poc.py --config config.json --emit-mcp plan.json
python3 pw_jira_poc.py --config config.json --apply           # REST write
```

Nothing writes without `--emit-mcp` or `--apply`.

## How it works

1. **Read** — JQL pulls closed issues with points, assignee, parent epic,
   resolution date and changelog.
2. **Convert** — points to hours via a lookup table, not a flat multiplier.
3. **Resolve** — Jira assignee to Projectworks `userId`, parent epic to
   `timecodeId`. Truefit budgets by practice and their practices are their
   epics, so the mapping is one-to-one out of the box.
4. **Allocate** — hours spread across the working days between first
   *In Progress* and resolution, capped per day.
5. **Write** — `plan_time_entry_changes` → review → `apply_time_entry_changes`.

Step 5 is the part to lead with. The MCP server won't let anything commit
without a plan being produced and approved first. Truefit's real anxiety is
synthetic time entries landing in a system that touches invoicing; plan-then-
apply answers that before Tyler raises it.

## Findings

**1. The flat multiplier doesn't match their own rule of thumb.** Tom gave two
numbers that disagree. 2.67 hours per point puts a 3 at 8 hours, which matches
"about a day." But a 5 comes out at 13.35 against his "day and a half," and an 8
lands at 21.4. A flat rate overstates the large stories, and large stories are
where the budget goes. The POC uses a table instead. Truefit needs to sign off
on the rows.

**2. Utilisation is structurally capped at 80%.** 12 points a week at the agreed
conversion is 32.04 hours. If Jira is the only source of time, nobody can ever
log more than 32 hours, so utilisation and capacity reporting are bounded by the
conversion ratio rather than measured from the work. Budget vs Actuals is
unaffected — hours and budget both come from the same place — but capacity
planning and utilisation dashboards will read low by a fixed 20% forever. Worth
Tom knowing on day one rather than discovering it in October when he's trying to
run the business off the numbers.

**3. Per-story caps don't produce per-day caps.** Stories overlap, so two
concurrent 8-pointers put 13+ hours on a single Tuesday. The daily load check
surfaces this. Options: cap at the day level and spill forward, accept the
overage, or widen spans.

**4. Locked weeks block writes.** Once a timesheet is submitted and approved, a
late re-point can't reach back into it. `get_timesheet` returns lock status per
week; the POC checks before it plans and blocks rather than failing silently
mid-batch.

## Safety properties

- **Idempotent.** Every entry carries a `[PWJIRA:KEY]` marker and the run keeps
  a state file fingerprinted on points, person, timecode and dates. Re-running
  with nothing changed produces all SKIPs. Safe on a schedule.
- **Handles re-points.** A story re-pointed after close becomes an UPDATE, not a
  duplicate. If the re-point shortens the span, the dates it no longer owns get
  explicit `clear` operations first — otherwise old hours linger and the totals
  quietly double.
- **Blocks rather than guesses.** No points, no assignee, unmapped assignee,
  missing resolution date, locked week — each blocks with a stated reason and
  shows up in the report as a decision for Truefit, not a silent skip.
- **Totals reconcile.** Allocated hours sum exactly to converted hours, to the
  quarter hour, with the remainder spread rather than dumped on one day.

## Open decisions for Truefit

| | Decision | Default in the POC |
|---|---|---|
| 1 | Sign off the point→hour table | 1:2.67, 2:5.33, 3:8, 5:12, 8:19, 13:32 |
| 2 | Points with no agreed row (a 21) | fall back to ×2.67, flagged in the plan |
| 3 | Unpointed or unassigned issues | blocked, reported, no time written |
| 4 | Contractors not in Projectworks | blocked until mapped |
| 5 | Date placement | spread across the open window, 8h/day cap |
| 6 | Do these entries feed billing | **recommend internal capacity only** |

Decision 6 is the one to raise deliberately. Story-point-derived hours are an
estimate wearing the clothes of an actual. Fine for utilisation and Budget vs
Actuals; risky under a T&M invoice a client could dispute. Truefit are fixed-fee
by the sound of the call, which makes this low-stakes for them — but say it out
loud so it's on the record.

## Notes

- `--apply` writes via the Projectworks Open API. **Verify the endpoint and
  payload shape against the docs for the target tenant before running it live.**
  The MCP path is the one to demo and the one to ship; the REST path exists for
  a customer who wants this running headless on a schedule.
- Point everything at Stage first. Prod, Stage and Stage-delta are all
  available; there is no reason to test middleware logic against real data.
- The fixture data is synthetic and shaped like Truefit's Jira. Same code path
  as live — only the data source swaps. Say that on the call, because it's the
  difference between a mockup and an integration.
