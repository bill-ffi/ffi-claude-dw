# Teamwork → BigQuery sync

Pulls Projects, Tasks, Users, and Time logs from Teamwork and loads them into
BigQuery (`radiant-rig-284611.teamwork_data`). Meant to run on a schedule
(twice daily), not inside a chat session — see **Scheduling** below.

## What it does

- **projects**, **tasks**, **users**: full truncate + reload every run.
- **timelogs**: only the current calendar month's rows are deleted and
  reinserted each run (by `log_date`, derived from Teamwork's `timeLogged`
  field). Prior months are left untouched. The delete+insert is wrapped in a
  single BigQuery multi-statement transaction (via a staging table) so a
  mid-run failure can't leave the table half-deleted.
- Projects scope: all projects except deleted ones — every non-archived and
  archived project is included in the **projects** table, per your
  instruction to "pull everything but deleted." (Correction: this account's
  raw `status` field is only ever `'active'`/`'inactive'` — "archived" is
  not a `status` value, it's a separate `archivedAt` timestamp, populated
  whenever a project has been archived.)
- Tasks scope: **all** tasks (open or completed, in any tasklist whether
  active or completed) belonging to an **active** project (`status` in
  `ACTIVE_PROJECT_STATUSES`, set in `sync.py` — currently just `active`;
  on this account that gate is a no-op, see "Known gaps"),
  OR any project archived on or after `ARCHIVED_PROJECT_TASKS_CUTOFF`
  (currently `2026-01-01`, also in `sync.py`), whatever that project's
  status. Everything else — projects archived before the cutoff, and
  never-archived projects that aren't active — is excluded from the
  **tasks** table entirely; the project itself still appears in
  **projects** regardless of status or archive date, only its tasks are
  skipped. Whether a task's own *tasklist* is completed has no bearing on
  inclusion — `showCompletedLists=true` (see
  `teamwork_client.list_tasks()`) makes sure those come through too.
  Run `python sync.py --explain-task-scope` to see the exact task count
  each candidate scope definition produces before syncing — or from GitHub
  Actions: **Actions** tab -> **Teamwork -> BigQuery sync** -> **Run
  workflow** -> check **"Explain task scope only"** -> **Run workflow**.
  It reads only; nothing is written to BigQuery. See
  "Known gaps" below for how the cutoff was chosen and both flags' effect
  on row counts.
- Users scope: everyone on the account, including deactivated/deleted users
  — they're kept (flagged via `is_deleted`) rather than dropped, so
  historical timelogs/tasks referencing them still resolve to a name instead
  of a dangling ID.
- **`projects.client_name`**: resolved from `company_id` via a dedicated
  `companies.json` call (`list_companies()`), the same pattern used for
  `category_name`. `company_id` is Teamwork's internal field for what we
  call a client.
- **`tasks.sequence_id`**: Teamwork's native recurring-task identifier — all
  occurrences of a recurring task share the same `sequence_id`; `NULL` for
  non-recurring tasks. No extra API call needed, it rides on the normal
  tasks pull.
- **`tasks.activity`**: the "Activity" preset-list custom field, resolved to
  its option label. Pulled in bulk at zero extra API cost via
  `tasks.json?includeCustomFields=true` (confirmed real via Teamwork's own
  [public API-Request-Examples repo](https://github.com/Teamwork/Teamwork.com-API-Request-Examples),
  not a guess) — the values ride along on the same tasks pull already
  happening, in `included.customfieldTasks`. If that sideload is ever
  missing on a given account/response, the code automatically falls back to
  one API call per task (concurrent, throttled) — slower but functionally
  equivalent. A failure to resolve a given task's activity (or to find the
  field at all) is non-fatal either way: that row's `activity` just stays
  `NULL` and everything else still loads.
- Logs a `RUN_SUMMARY` JSON line at the end of every run with rows
  pulled/written per table and any errors, for auditability.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in:
   - `TEAMWORK_API_KEY` — your Teamwork API token
   - `TEAMWORK_BASE_URL` — e.g. `https://forwardfinancialintelligenceinc.teamwork.com`
   - `GOOGLE_APPLICATION_CREDENTIALS` — local path to the GCP service account
     JSON key (BigQuery Data Editor + BigQuery Job User on
     `radiant-rig-284611`). **Do not commit this file** — `.gitignore` at
     the repo root already excludes `.env` and `service-account*.json`, but
     double-check before pushing.
   - `SYNC_TIMEZONE` — the timezone "current calendar month" should be
     computed in (e.g. `America/New_York`). Defaults to UTC if unset —
     **pick the right one for your team before scheduling**, since it
     determines exactly when the timelogs window rolls over at month end.
3. Sanity check connectivity before touching BigQuery:
   ```
   python sync.py --dry-run
   ```
   This hits each Teamwork endpoint with a 1-row page and prints the field
   names it got back, without writing anything. See **Known gaps** below —
   run this first and fix `teamwork_client.py`'s `*_PATH` constants if any
   endpoint 404s.
4. Run a full sync manually:
   ```
   python sync.py
   ```
   Exits 0 if all three datasets synced successfully, 1 if any stage failed
   (check the logged `RUN_SUMMARY` / stderr for which one and why).

## Exception reporting views

Five BigQuery views, one per quality-control rule, meant to be the direct
data source for Looker Studio reports for leadership — each one is
filterable by user / project / client / tasklist directly off its columns
(no extra joins needed in Looker). Plus three more that aren't exception
rules — `v_usermins` (see "External reference data" below) and the user
report's `v_user_daily_billable_hours_base` / `v_user_weekly_billable_hours`
(see "User report" below). All eight are defined in `views.py`;
created/updated via:

```
python sync.py --create-views
```

or from GitHub Actions: **Actions** tab → **Teamwork -> BigQuery sync** →
**Run workflow** → check **"(Re)create the exception/QC reporting
views"** → **Run workflow**. This doesn't pull from Teamwork or touch table
data — it only (re)defines the views, so it's safe to run any time and
independent of the normal sync schedule.

| View | Flags | Scope |
|---|---|---|
| `v_exception_missing_activity_with_time` | Tasks with no "Activity" value set AND at least one timelog (`minutes > 0`) posted against them — the more urgent half of the old missing_activity rule, since this is billable work happening with no Activity value | Monitored categories only |
| `v_exception_missing_activty_no_time` | Tasklist-level rollup of **still-open** tasks (`status != 'completed'`) with no "Activity" value set AND no time posted — one row per tasklist, `missing_activity_no_time_task_count`, only surfaced where that count is 3 or more (a single untouched task isn't noteworthy; a cluster is) | Monitored categories only |
| `v_exception_missing_estimate` | Tasks with `estimate_minutes` NULL or 0 | Monitored categories only, minus the Client Management / Client Management v2 / HR Advisory tasklist exception within Non-Monthly |
| `v_exception_billable_time_internal_projects` | Billable timelogs (`minutes > 0`) posted to an internal-category project | `FFI Internal Projects`, `Functional`, or `Individual` category only |
| `v_exception_long_time_entries` | Timelogs over 2 hours | All projects (not category-scoped) |
| `v_exception_recurring_compliance` | Top-level tasks (no `parent_task_id`) in a "Books Maintenance"-category project with no `sequence_id` | Books Maintenance category only; sub-tasks excluded since they inherit recurrence from their parent and don't carry their own `sequence_id` |

**Missing-activity split into two views.** Originally one view
(`v_exception_missing_activity`), split per your instruction into
`v_exception_missing_activity_with_time` (task-level, time already posted —
the more urgent case) and `v_exception_missing_activty_no_time`
(tasklist-level count of untouched tasks, only shown once a tasklist has 3
or more). Note the `no_time` view's name keeps your original spelling
("activty") — this repo does not silently "fix" a name you specified.
**Refinement**: `no_time` is further scoped to `t.status != 'completed'` —
a completed task that never got an Activity value isn't actionable
anymore, so it shouldn't count toward (or pad) a tasklist's cluster.
Assumes the Teamwork status string for a completed task is exactly
`'completed'` (lowercase, as returned by the tasks endpoint) — worth
confirming against a known-completed task's row in BigQuery.

**Assignee names, not IDs.** The task-level views
(missing_activity_with_time, missing_estimate, recurring_compliance)
surface a task's assignees as a single `assignee_names` string column
(e.g. `"Bob, Jane, Mary"`), built via a `STRING_AGG` subquery over
`tasks.assignee_user_ids` — not one row per assignee. This keeps these
views at one row per task (a task with 3 assignees used to show up as 3
duplicate rows before this). Trade-off: in Looker Studio, filtering this
column by one person needs a **"Text contains"** filter condition rather
than an exact-match dropdown, since it's a free-text concatenation, not a
clean dimension value. The two timelog-based views
(billable_time_internal_projects, long_time_entries) are unaffected — a
timelog only ever has one `user_id`. `missing_activty_no_time` doesn't
carry this column either — it's aggregated to one row per tasklist, above
individual task/assignee granularity.

**`proj_owner`** — the project's owner (`projects.owner_id`) resolved to a
name, on all six views (added anywhere project context already appears).

**`has_time_logged`** — boolean, true if any timelog with `minutes > 0`
exists against the task, via a correlated `EXISTS` subquery on `timelogs`.
Added to the two task-level views where it's not already implied by the
view's own filter (missing_estimate, recurring_compliance) — on the two
timelog-based views this would be trivially true for every row (the row
itself is a posted time entry), so it wasn't added there.
`missing_activity_with_time` also skips it: by definition every row in
that view already has time logged, so the column would just be a constant
`TRUE`. `missing_activty_no_time` is aggregated, not per-task, so it
doesn't apply there either — the "no time" condition is what's being
counted, via `NOT EXISTS` in the view's `WHERE`.

**`user_email`** — added to the two timelog-based views
(billable_time_internal_projects, long_time_entries) alongside the existing
`user_name`, specifically so Looker Studio's built-in per-viewer row-level
security (data source setting: restrict rows by the viewer's login email)
can be turned on for these two — each timelog has exactly one `user_id`,
so this is a clean exact-match field. **Deliberately not added** to the
task-based views (missing_activity_with_time, missing_activty_no_time,
missing_estimate, recurring_compliance): a task can have multiple
assignees, and the per-task views among these already collapse to one
`assignee_names` string per task (see above) to avoid duplicate rows — a
concatenated string can't satisfy Looker's exact-match email security.
Confirmed with you: these views stay one-row-per-task (or, for
missing_activty_no_time, one-row-per-tasklist) with no per-assignee
security, rather than reverting to one row per assignee just to enable it.

**"Monitored categories"** = the four project categories confirmed as "all
of the billable projects we are monitoring": Books Maintenance, Monthly
Close, Non-Monthly, and Payroll. (Originally tracked as one combined
"Non-Monthly & Payroll" category — turned out to be two distinct real
Teamwork categories, so the combined string never matched anything and
rules 1, 2, and 5 were silently skipping both entirely until this was
split out.) Defined once as `MONITORED_CATEGORIES` at the top of
`views.py` — change it there (not in the SQL) if the set ever changes, and
re-run `--create-views`.

**"Internal categories"** (rule 3 only) = `FFI Internal Projects`,
`Functional`, `Individual` — confirmed against real `category_name` values
in production (`SELECT category_name, COUNT(*) FROM projects GROUP BY 1`).
This is a deliberate, explicit whitelist, **not** the inverse of
`MONITORED_CATEGORIES` — the account also has several sizable categories
that are neither monitored nor internal (`Books` — 1,056 projects, by far
the largest category in the account — plus `Advisory` [49],
`Tax/Compliance` [253], `Onboarding` [27], `Legacy Projects` [4], and 5
uncategorized projects). Billable time posted to any of those never shows
up in `v_exception_billable_time_internal_projects` — confirmed as the
intended behavior, not an oversight. Defined as `INTERNAL_CATEGORIES` at
the top of `views.py`.

**Assumptions worth verifying against real data before trusting the
numbers** (called out here per usual practice — these are judgment calls
made to turn the rules as discussed into SQL, not confirmed facts):
- Rules 1, 2, and 5 only look at tasks in a monitored-category project.
  Tasks in an uncategorized or other-category project never show up in
  those three views at all, by design.
- Rule 3 ("billable time on internal projects") now uses a confirmed
  explicit whitelist (`INTERNAL_CATEGORIES` — see above), not an inference.
  This one's settled, not an open assumption.
- Rule 4 (long time entries) deliberately is **not** scoped to monitored
  categories — it checks every billable and non-billable timelog site-wide,
  since excess time logged anywhere seemed worth surfacing. Say the word if
  this should be scoped down to match the other rules instead.

### External reference data: `v_usermins`

`v_usermins` is not an exception rule — it's a reference view joining
Teamwork users to per-person weekly minimum billing figures maintained
outside Teamwork, in a Google Sheet ("FFI Compensation Database and
Budget", named range `mins4bq`).

- The sheet is referenced via `gs_minimum_user_info`, a BigQuery **external
  table** backed directly by that named range — created manually through
  the BigQuery console (**Create table → Drive**), not by this pipeline.
  It live-queries the Sheet on every read; nothing is copied or cached.
- It was deliberately set up this way rather than through our GitHub
  Actions service account: the table was created under your own Google
  identity, and Looker Studio's "owner's credentials" mode (see below)
  means reports also read it as you. The service account was NOT given
  Drive access to the sheet, since it has no need to touch this table —
  keeping this comp-adjacent data out of the automated pipeline entirely.
- Join key: `gs_minimum_user_info.tw_userid` (INT64) ↔ `users.user_id`.
  `v_usermins` uses an inner `JOIN`, so only users present in the sheet
  show up — not every Teamwork user.
- `min_bill`/`min_value` in the sheet are **weekly** figures; `v_usermins`
  derives daily versions (`/ 5`) alongside them.
- **Sensitivity note**: `min_bill`/`min_value` are comp-adjacent, same as
  `users.user_cost`/`user_rate` (see "Known gaps" below) — worth
  restricting read access to this view if BigQuery access here is ever
  opened up more broadly.
- If the named range, sheet, or external table is ever renamed, update
  `ANCILLARY_USER_INFO_TABLE` in `views.py` and re-run `--create-views`.

### User report: `v_user_daily_billable_hours_base` / `v_user_weekly_billable_hours`

Two layered views replace what used to be five separate ones
(`v_user_daily_billable_hours_long`/`_wide`,
`v_user_daily_billable_hours_trend_long`/`_wide`, and
`v_user_current_week_hybrid`) — consolidated for efficiency, since all of
them were re-deriving the same "billable timelogs → weekday bucket → week"
logic independently. **Migration note**: `v_user_daily_billable_hours_wide`
was already wired into a live "Team Hours" Combo chart — that chart needs
to be rebuilt against `v_user_weekly_billable_hours` once the old views
are dropped (see "Known gaps" for the drop-order caveat).

**Layer 1 — `v_user_daily_billable_hours_base`**: the shared aggregation.
One row per `(user, day_bucket, week_start)` — real, historical
`SUM(hours)` from billable timelogs, with the weekday-bucketing rule
applied exactly once. Deliberately sparse (no user scaffold, no
`daily_min_bill`, no zero-filling) and unbounded (all history, since it's
a view — cost is incurred at query time, and the one consumer below
filters to whatever range it needs).

**The week now runs Sunday-through-Saturday**, not Monday-through-Sunday
— `week_start` is always a Sunday (BigQuery's default `WEEK` truncation),
per your instruction that "week of" should be a Sunday and the business
week should logically end on Saturday. This also resolves an ambiguity
the old Monday-Sunday week had: Sunday and Monday are now adjacent and
both near the start of the same week, and Friday and Saturday are
adjacent and both near the end — so "Sunday's hours fold into Monday,
Saturday's fold into Friday" no longer has any "hasn't happened yet"
ambiguity about which week a Sunday belongs to.

**Layer 2 — `v_user_weekly_billable_hours`**: actual + projected, combined
via `UNION ALL` (your preferred approach, so a report can show both
together). Grain: one row per `(user, day_bucket, week_start)`, spanning
from the start of the **last completed calendar quarter** through the
current (possibly in-progress) week — a rolling window that's ~13 weeks
right after a quarter turns over, growing to ~26 weeks right before the
next one does (confirmed against 2026-08-30: quarter boundary lands on
2026-04-01, snapped to its Sunday-week of 2026-03-29, giving 23 weeks
through the current week of 2026-08-30 — within the expected range).

Two `UNION ALL` branches, verified mutually exclusive and exhaustive for
every day of the week before building this:
- **`actual`** — every already-elapsed `(week, day_bucket)` combination,
  scaffolded against `v_usermins` × the 5 weekdays × a generated weekly
  date spine, so a real 0-hour day shows as an explicit 0 rather than a
  missing row. All history before the current week is unconditionally
  actual; within the current week, a bucket is actual once its weekday
  has fully passed.
- **`minimum` / `plug`** — only the current week's not-yet-elapsed
  buckets. Monday–Thursday (today included, since today isn't complete
  either) show the flat `daily_min_bill` as a placeholder
  (`value_type = 'minimum'`). Friday shows a catch-up **plug**:
  `(5 × daily_min_bill) − whatever Mon–Thu are currently showing` (actual
  where already past, the minimum placeholder otherwise), clamped at 0
  rather than going negative if someone's already ahead of pace by
  Thursday.

Confirmed for every day of the week, not just the original Wednesday
example:
- **Run on Sunday** (the case you specifically asked to confirm): the
  coming week hasn't started yet, so Monday–Thursday all show `minimum`
  and Friday's plug settles at exactly one day's minimum. "Week of Aug
  30" (a Sunday) appears as the current/last row, fully projected — as
  requested.
- **Run on Monday**: identical to Sunday — nothing's happened yet either
  way.
- Wednesday, Thursday, Friday: progressively more of Mon–Thu becomes
  `actual` as the week goes on; Friday's plug becomes a genuinely useful
  "how much more do I need today" figure once it's actually Friday.
- **Run on Saturday**: the whole Mon-Fri business week has already
  elapsed (Saturday is the *last* day of a Sun-Sat week), so every
  bucket, Friday included, shows `actual` — the projected branch produces
  zero rows for that week.

`value_type` (`'actual'` / `'minimum'` / `'plug'`) rides along on every
row so Looker Studio can visually distinguish real numbers from
assumed/calculated ones (e.g. italicize or footnote non-actual cells)
instead of silently blending them. `pct_of_min` (hours ÷ `daily_min_bill`,
via `SAFE_DIVIDE`) rides along too, same as before.

**The "prior 4-week average" concept is gone entirely** (per your
instruction — no longer needed), superseded by this quarter-to-date
window of individual weeks, current-week projection included.

## Backfilling history

The normal run only ever touches "this calendar month" for timelogs (see
**What it does** above), so it won't populate past months on its own. To
load history — e.g. the last several months before this script started
running — use `--backfill-months`:

```
python sync.py --backfill-months 2026-01,2026-02,2026-03
```

- Give it one or more `YYYY-MM` months, comma-separated, in any order.
- For each month, it deletes and reinserts that month's timelogs rows only
  — every other month (including months not in the list) is untouched.
- It does **not** touch `projects` or `tasks` — those are always a full
  replace on the normal run, so there's nothing to backfill there.
- Safe to re-run for the same month if something looks off — it'll just
  replace it again with a fresh pull.
- From GitHub Actions: **Actions** tab → **Teamwork -> BigQuery sync** →
  **Run workflow** → type the months (e.g. `2026-01,2026-02,2026-03`) into
  the **Backfill months** box → leave **Dry run only** unchecked → **Run
  workflow**. Leave the box blank for a normal run.

## Scheduling

Built as a **GitHub Actions scheduled workflow** (`.github/workflows/teamwork-bigquery-sync.yml`),
twice daily. Cron: `15 11,16 * * *` — **11:15 and 16:15 UTC**.

| Fires (UTC) | US Eastern (EDT, ~Mar-Nov) | US Eastern (EST) |
|---|---|---|
| 11:15 | 07:15 | 06:15 |
| 16:15 | 12:15 | 11:15 |

- **Spacing is deliberately uneven**: 5 hours between the two runs, then 19
  hours to the next morning. Activity logged after ~12:15 ET isn't in
  BigQuery until the next morning. (The schedule was 04:15/16:15 UTC — an
  even 12h/12h — until 2026-09-04; corrected to the intended
  11:15/16:15 UTC.)
- **Cron is UTC and DST-unaware.** The UTC times are fixed; the Eastern
  clock times shift an hour twice a year. The requirement was specified in
  UTC, so UTC is the contract.
- **`schedule:` only fires from the default branch.** A cron edit on a
  feature branch does nothing until merged.
- **The `:15` offset is deliberate** — see "Known gaps" for the measured
  delay data behind it.
- `SYNC_TIMEZONE` (`America/New_York`, a workflow env var) controls what
  "current calendar month" means for the timelogs window. It is
  independent of the cron's UTC timing — the two are not linked
  automatically.
- `timeout-minutes: 60` bounds the job. A hung run would otherwise hold the
  `concurrency` group and silently stall every later scheduled sync.

If GitHub Actions' scheduling reliability continues to be a problem after
the on-the-hour fix, **Cloud Scheduler + Cloud Run** is the documented
fallback: real timing guarantees GitHub's best-effort scheduler doesn't
give, and the service account key never has to leave GCP (Cloud Run can use
the attached service account directly). Say the word and I'll wire it up.

## Known gaps / things to verify before relying on this

- **Cron fired 7 hours early (fixed 2026-09-04).** The workflow ran
  `15 4,16 * * *` — 04:15 and 16:15 UTC — but the intended schedule was
  **11:15 and 16:15 UTC**. The 16:15 slot was always right; the first slot
  was 7 hours early, landing at 00:15 ET in summer instead of 07:15 ET.
  Now `15 11,16 * * *`. Note this also changed the spacing from an even
  12h/12h to 5h/19h; that follows from the requested times, not from the
  fix. **This does not take effect until the branch is merged** — GitHub
  only fires `schedule:` from the default branch.
- **The last ~12 hours of every month are never ingested, and retroactive
  entries to a closed month never arrive at all.** Not a cron bug —
  a consequence of the current-month-only replace window meeting any
  twice-daily schedule, and unchanged by the cron correction above:

  | | last run still covering September | next run (already October) | unsynced tail |
  |---|---|---|---|
  | old 04:15/16:15 UTC | Sep 30, 12:15 ET | Oct 1, 00:15 ET | 11h 45m |
  | new 11:15/16:15 UTC | Sep 30, 12:15 ET | Oct 1, 07:15 ET | 11h 45m |

  Once the calendar month rolls over in `SYNC_TIMEZONE`, `run_full_sync()`
  only ever asks for the *new* month, so the previous month is frozen
  exactly as it stood at its final run. Two consequences:
  1. Time logged on the last day of the month after ~12:15 ET is never
     picked up.
  2. **More significant**: any timelog entered or edited *retroactively*
     against a closed month — routine in timekeeping, where people write
     up last week on Monday — is never ingested either. Those rows are
     invisible in BigQuery until someone runs `--backfill-months` by hand.

  No schedule change fixes this; the window has to widen. The cheap fix is
  a rolling two-month window on the normal run (replace both the previous
  and current month), which roughly doubles only the timelogs pull and
  closes both cases. Not implemented — flagged for a decision.
- **`CURRENT_DATE()` was UTC in the user-hours views (fixed 2026-09-04).**
  `v_user_weekly_billable_hours`'s `bounds` CTE decided "has this weekday
  elapsed yet?" from `CURRENT_DATE()`, which takes no timezone argument in
  BigQuery and so returns the **UTC** date. Eastern is UTC-4/-5, so from
  20:00 ET onward BigQuery already believed it was tomorrow. Simulated
  hour by hour across a full week: **28 of 168 hours (17%) were
  misclassified**, every evening between 20:00 and 23:59 ET.
  - **What it looked like**: Mon-Thu buckets flipped from `minimum` to
    `actual` a day early (showing a real 0 for a day nobody had worked
    yet, which also drags `pct_of_min` to 0); Thursday evening turned
    Friday into a `plug` before Friday began; and worst, from 20:00 ET
    **Saturday**, `DATE_TRUNC(CURRENT_DATE(), WEEK)` rolled forward to the
    next Sunday — so the week that had just finished dropped out of the
    report entirely and a brand-new, fully-projected empty week appeared
    in its place.
  - **Fix**: all three `CURRENT_DATE()` calls in `bounds` now pass
    `REPORTING_TIMEZONE` (`America/New_York`), a constant at the top of
    `views.py`. Re-run `--create-views` to apply it — a view bakes its SQL
    in at creation time, so editing the constant alone changes nothing
    until the view is recreated.
  - **Verified**: the two `UNION ALL` branches were re-simulated for all
    seven days after the change and remain mutually exclusive and
    exhaustive, with the Sunday run still producing a one-day Friday plug
    exactly as documented under "User report" above. `log_date` needed no
    change — it is already a `DATE`, so `DATE_TRUNC`/`EXTRACT` over it
    carry no timezone.
  - **Not fixed by this, and separate**: `timelogs.log_date` is the date
    portion of Teamwork's UTC `timeLogged`, so an entry logged late in the
    evening Eastern can still be bucketed to the following day. This fix
    corrects *when a day is considered elapsed*, not *which day an entry
    lands on*.
  - **`REPORTING_TIMEZONE` vs `SYNC_TIMEZONE`**: same business day, two
    constants on purpose — a view bakes its timezone in at
    `--create-views` time, while `SYNC_TIMEZONE` is read fresh on every
    sync run. If the team's anchor timezone ever changes, change both and
    re-run `--create-views`.
- **Task scope — measured and confirmed correct (2026-09-04).** A review
  round suspected the scope filter was over-pulling, on the theory that
  `sync_projects()` only ever tested `archived_at` and never `status`, so
  dormant-but-unarchived projects were sneaking in. **That theory was
  wrong**, and `--explain-task-scope` disproved it against the live API.
  The real numbers, for anyone re-deriving an expected row count later:

  | projects | status | archive bucket | tasks |
  |---:|---|---|---:|
  | 218 | `active` | never archived | 13,930 |
  | 605 | `inactive` | archived >= 2026-01-01 | 23,296 |
  | 1,066 | `inactive` | archived < 2026-01-01 | *excluded* |
  | **823** | | **in scope** | **37,226** |
  | 1,889 | | every non-deleted project | 55,386 |

  - **`status` and `archived_at` are perfectly collinear on this account**:
    `active` always means never-archived, `inactive` always means archived.
    There is no inactive-but-unarchived project, so "all active projects"
    and "all non-archived projects" select the identical 823 projects and
    the identical 37,226 tasks. `ACTIVE_PROJECT_STATUSES` (added by that
    review round) is therefore a **no-op on current data** — kept because
    it states the requirement literally, and because any future divergence
    now shows up under `excluded_not_active` in `RUN_SUMMARY` instead of
    silently changing the row count.
  - **The expected tasks row count is ~37,226, not ~23,000.** 23,296 is
    the *archived-only* half of the scope — what you get from
    `... JOIN projects p ... WHERE p.archived_at >= '2026-01-01'`. That
    query deliberately omits the 218 active projects and the 13,930 tasks
    they carry, so it is not a check on the table's total size. Confirmed
    with the repo owner 2026-09-04: 37,226 is the correct target.
  - **Two other hypotheses from the same review, both disproved live**:
    `archivedAt` carries no Go zero-time sentinel on this account (0 of
    1,889 projects were unparseable or pre-1900), and `projectIds=` *is*
    being honoured (the narrowest scope returns 13,930 against a site-wide
    55,386, so batches are genuinely scoped rather than silently returning
    everything).
  - **What was kept from that round anyway**, since none of it depends on
    the disproved theory: `transform.parse_archived_at()` (parses to a real
    `date` rather than lexicographically comparing a raw API string),
    `transform.task_project_id()` (the projectId fallback chain, so scope
    no longer rests on one nested key whose absence would drop every task),
    dedupe by `task_id` before load, a refusal to write an empty scope over
    a populated table, an error log when a task returns for a project that
    wasn't requested, and `task_pull_project_scope` / `rows_dropped` in
    `RUN_SUMMARY` so a surprising row count explains itself next time.
  - **Re-check the numbers any time** with `python sync.py
    --explain-task-scope` (or the "Explain task scope only" checkbox in the
    Actions workflow). It reads only — no BigQuery writes, no task rows
    fetched, counts come from `meta.page.count` at `pageSize=1`.
- **Tasks in a COMPLETED tasklist (fixed 2026-09-03).** Tasks belonging to a
  completed tasklist used to be silently excluded from the tasks pull, even
  within an active, non-archived project. Found via task 49325131 ("Record
  health insurance allocation", a completed subtask in project 1388473,
  "CNE Monthly Books (2026)"), which turned up missing from the tasks table
  despite its project being genuinely active — so not the archived-project
  cutoff above.
  - **Root cause, confirmed live**: its tasklist (id 3941748, "Monthly
    Close 2026-05") is **completed**, not deleted — an earlier pass at this
    investigation wrongly concluded "deleted" from a bare `404` on
    `GET /tasklists/{id}.json`, corrected after you pointed out (with a
    screenshot) that the tasklist is visible under the project's
    "Completed task lists" section.
  - **Fix — `showCompletedLists=true`.** Sourced from Teamwork's own
    official example repo
    ([Teamwork/Teamwork.com-API-Request-Examples](https://github.com/Teamwork/Teamwork.com-API-Request-Examples),
    `getRequests/tasks/Get all tasks.js`), confirmed verbatim and then
    tested live: it must be combined with the `includeCompletedTasks=true`
    and `includeArchivedProjects=true` this pipeline already sends — added
    alone (or with `status=all` instead of `includeCompletedTasks`) it's a
    no-op, which is why an earlier pass at this investigation wrongly
    concluded no fix existed. With the full three-flag combination:
    site-wide task count went from 19,577 to 55,355 (**+35,778 tasks**),
    and task 49325131 is now present when the same call is scoped to its
    project. `list_tasks()` now passes it unconditionally, matching your
    instruction that inclusion should not depend on a task's own tasklist
    being completed. (A related third-party guess of the same parameter
    name, offered without this combination or a citable source, was
    separately tested and — on its own — correctly found to be a no-op;
    the difference was entirely the missing `includeCompletedTasks=true`
    pairing, a useful reminder to verify any unsourced API-parameter claim
    against the real API rather than trusting it either way.)
  - **No separate cutoff for this one — the existing project-level cutoff
    already covers it.** Per your instruction: the new logic should pull
    all tasks (open or completed, any tasklist) for all active projects
    plus any project archived on or after `ARCHIVED_PROJECT_TASKS_CUTOFF`,
    regardless of whether a task's own tasklist is completed. Since
    `task_pull_project_ids` in `sync_projects()` already filters purely on
    each *project's* `archived_at`, adding `showCompletedLists=true`
    site-wide and leaving that filter untouched produces exactly this
    behavior with no additional code — tasks from completed tasklists flow
    through the same project-level gate as everything else. No tasklist
    `completedAt` timestamp was needed (tasklists don't carry one anyway,
    only `status: "completed"` and `updatedAt`).
  - **Scope, exact count**: site-wide `tasklists.json` (a real, working,
    non-project-scoped endpoint) gives an exact count via
    `meta.page.count`: 599 tasklists without `showCompleted=true`, 896
    with it — **297 completed tasklists** site-wide. Averaging ~120 tasks
    per completed tasklist (35,778 / 297) — well above the ~33-34 seen in
    the one project inspected directly, so the distribution is uneven;
    some completed tasklists (particularly older, long-running recurring
    ones) likely carry far more history than others. Combined with the
    archived-projects cutoff, expect the `tasks` table to land well above
    the ~12,160 rows that cutoff alone produced — re-run and check the
    `RUN_SUMMARY` for the real number.
- **Archived-project tasks (fixed 2026-09-03).** Tasks belonging to an
  archived project used to be silently excluded from the tasks pull —
  `teamwork_client.list_tasks()` called the site-wide `tasks.json` endpoint
  with no equivalent of the `includeArchivedProjects=true` flag that
  `list_projects()` already needed for projects.json, so any task whose
  project had been archived just never came back, even though the task
  itself was neither deleted nor completed-and-gone. Confirmed live against
  the real API (2026-09-02): a real, non-deleted, completed task in a known
  archived project was invisible to a site-wide pull, but returned fine
  once queried scoped to its own project.
  - **Fix**: `list_tasks()` now passes `includeArchivedProjects=true`
    (confirmed live: task count went from 7,689 to 19,527 with the flag —
    a real, working parameter, not a guess). `includeArchivedTasks=true`
    was also tried as a plausible alternative name and confirmed to be a
    no-op on this account — not real.
  - **Cutoff, not everything**: pulling every archived project's tasks
    unconditionally would add ~11,838 mostly-stale rows from projects
    archived as far back as this account's 2023 start — a lot of volume
    for very little reporting value. Instead, `sync.py`'s
    `ARCHIVED_PROJECT_TASKS_CUTOFF` (currently `2026-01-01`) only pulls
    tasks for projects archived on or after that date; older archived
    projects are excluded from the tasks pull (their project row still
    exists in **projects**, only their tasks are skipped). Confirmed live:
    of 1,671 archived projects, 605 were archived on/after 2026-01-01
    (4,471 tasks) vs. 1,066 archived earlier (7,367 tasks) — so the cutoff
    brings the tasks table to roughly 12,160 rows instead of 19,527.
    Change the constant (not the SQL/query params) if the cutoff date ever
    needs to move, and re-run a full sync.
  - **Correction to a previous README claim**: this account's real project
    `status` field is only ever `'active'`/`'inactive'` — "archived" was
    previously (incorrectly) assumed to be one of six `status` values
    alongside active/current/late/upcoming/completed. It's actually a
    separate `archivedAt` timestamp field (`projects.archived_at` in
    BigQuery), unrelated to `status`.
- **Scheduled-run delays (partially mitigated, not fully solved).**
  The original cron (`0 5,17 * * *`, firing exactly on the hour) was
  investigated after noticing a run at an unexpected time. Pulled via the
  GitHub Actions API (`created_at` on each `event=schedule` run — precise,
  not the rounded UI display) for the 9 most recent scheduled firings as
  of 2026-09-01:

  | Run # | Actual fire time (UTC) | Intended slot | Delay |
  |---|---|---|---|
  | 21 | 2026-08-28 01:16:54 | Aug 27, 17:00 | 8h 16m |
  | 23 | 2026-08-28 17:10:04 | Aug 28, 17:00 | 10m |
  | 26 | 2026-08-29 11:28:58 | Aug 29, 05:00 | 6h 28m |
  | 30 | 2026-08-29 19:36:39 | Aug 29, 17:00 | 2h 36m |
  | 32 | 2026-08-30 10:16:46 | Aug 30, 05:00 | 5h 16m |
  | 34 | 2026-08-30 19:38:55 | Aug 30, 17:00 | 2h 38m |
  | 36 | 2026-08-31 11:22:13 | Aug 31, 05:00 | 6h 22m |
  | 37 | 2026-08-31 21:44:11 | Aug 31, 17:00 | 4h 44m |
  | 38 | 2026-09-01 09:46:32 | Sep 1, 05:00 | 4h 46m |

  Delay ranged from 10 minutes to over 8 hours — not a fixed offset — and
  the gap *between* consecutive scheduled fires ranged from ~8 to ~18
  hours, when a healthy twice-daily schedule should hold a steady ~12.
  This is more than GitHub's documented "top-of-the-hour congestion"
  effect (typically minutes, not hours) would explain on its own.
  **Mitigation applied**: moved the cron off `:00` to `15 4,16 * * *`
  (04:15/16:15 UTC) — a cheap, documented best practice, but not
  guaranteed to eliminate multi-hour delays given the severity above. If
  delays are still unacceptable after this change, the documented
  fallback is Cloud Scheduler + Cloud Run (see "Scheduling" above), which
  doesn't share GitHub's best-effort queue.
- **Retired user-report views — drop order matters.**
  `v_user_daily_billable_hours_long`, `_wide`, `v_user_daily_billable_hours_trend_long`,
  `_wide`, and `v_user_current_week_hybrid` were all replaced by
  `v_user_daily_billable_hours_base` / `v_user_weekly_billable_hours` (see
  "User report" above). `--create-views` does not drop views removed from
  `VIEW_NAMES` — the old ones stay live in BigQuery until manually
  dropped. **Before dropping `v_user_daily_billable_hours_wide`
  specifically**: it was already wired into a live "Team Hours" Combo
  chart in Looker Studio — rebuild that chart against
  `v_user_weekly_billable_hours` first, then drop the 5 old views (safe
  to drop the other 4 immediately; nothing was built against them yet).
- **Retired `v_exception_missing_activity` — same drop caveat.** Replaced
  by `v_exception_missing_activity_with_time` and
  `v_exception_missing_activty_no_time` (see "Exception reporting views"
  above). `--create-views` won't drop the old view — check whether any
  Looker Studio report is still pointed at
  `v_exception_missing_activity` and repoint it before running
  `DROP VIEW` on it in BigQuery.
- **Endpoint paths.** All four (`PROJECTS_PATH`, `TASKS_PATH`,
  `TIMELOGS_PATH`, `PROJECT_BUDGETS_PATH`) have now returned real data in a
  live `--dry-run` against this account.
- **Pagination (fixed).** The first real full-sync run failed: pagination
  used the wrong query param names (`page[size]`/`page[offset]` instead of
  Teamwork's actual `pageSize`/`page`), so every "next page" request was
  silently ignored by the API and kept re-fetching, running away until
  Teamwork rate-limited it (HTTP 429). Fixed to use the correct param names,
  and added a hard `MAX_PAGES` cap in `teamwork_client.py` plus a page-1-vs-
  page-2 sanity check in `--dry-run` so this class of bug fails loudly
  next time instead of quietly hammering the API. Re-run `--dry-run` after
  any pagination-related change and confirm the "pagination sanity check"
  line says `[OK]`.
- **`category_name` (fixed).** Originally resolved from `projects.json`'s
  sideloaded `included["projectCategories"]` block, which is confirmed
  present when sampled through other tooling but was confirmed **empty on
  every page of every real production run of this script** — `category_id`
  populated fine, but the name lookup always came back NULL as a result.
  Root cause not pinned down (same auth/params, different result — never
  reproduced outside production). Rather than keep chasing it,
  `category_name` now comes from a dedicated `projectcategories.json` call
  (`list_project_categories()`), the same pattern already used for
  budgets/custom fields — not dependent on whatever does or doesn't ride
  along with the main projects pull. Re-run and check `--dry-run`'s
  "Project category diagnostic" section, or the `rows_with_category_name`
  count in a real run's `RUN_SUMMARY`, to confirm.
- **`health` (project health)** is included as a column but is best-effort:
  it wasn't present in the standard project payload during testing (even
  though Teamwork lets you *filter* projects by health). The code just
  reads `raw.get("health")`, so it'll populate automatically if your
  account's API happens to return it, and silently stay `NULL` otherwise —
  it won't break the sync either way.
- **`client_name` (company/client resolution).** Sourced from a dedicated
  `companies.json` endpoint (`list_companies()`), not from any
  `projects.json` sideload — following the same reasoning as the
  `category_name` fix below, since this API has a track record of
  sideloads not being reliable in production even when they work in direct
  sampling. Item-key casing for `companies.json` hasn't been directly
  observed, so `list_companies()` defensively tries `"companies"` then
  `"Companies"` and logs the real top-level keys if neither matches — same
  pattern as `list_project_categories()`. Check `--dry-run`'s "Client
  (company) diagnostic" section, or `rows_with_client_name` /
  `clients_resolved` in a real run's `RUN_SUMMARY`, to confirm.
- **Portfolio boards / Portfolio columns** — not included. No endpoint for
  this surfaced anywhere in the Teamwork API surface available during
  development; it may require a separate Portfolio-specific endpoint. Let
  me know if you have a specific API path for it and I'll wire it in.
- **Budget capacity/used/left** are sourced from the project-budgets
  endpoint's `capacity`/`capacityUsed` fields (confirmed real via a live
  test pull). A project can have multiple budgets (e.g. recurring monthly
  time budgets) — the code picks the one with `status == "ACTIVE"`, latest
  `startDate` among ties. Worth a sanity-check against a project you know
  the numbers for.
- **BigQuery dataset location** defaults to `US` (multi-region) — change
  `BQ_LOCATION` in `.env` if you need a specific region.
- **`users` table includes `user_cost` and `user_rate`** (each person's
  internal cost rate and billing rate, converted from Teamwork's cents to
  dollars). This is compensation-adjacent data, included per explicit
  confirmation — if BigQuery access to this project is ever opened up to a
  wider audience, consider restricting read access to the `users` table (or
  those two columns specifically) at that point.
- **`tasks.activity` — confirmed live.** The field is named `ACTIVITY`
  (all caps) in Teamwork, id 98742, applies site-wide (not scoped to a
  specific project), and is a dropdown with a fixed set of options (BOOKS /
  GL, BANK RECS, A/P & EXP, A/R & INV, REVnCOGS, CONTROLLING, PAYROLL, HR,
  ADVISORY, CLIENT MNGMT, FP&A, PROJECTS, COMPLIANCE — pulled from Teamwork
  at sync time, not hardcoded, so this list updates itself if the options
  ever change). Casing quirks worth knowing if touching this code:
  `customfields.json` returns items under `"customfields"` (lowercase f),
  and both the per-task and bulk-sideload values come back under
  `"customfieldTasks"` — different from the camelCase used by every other
  endpoint in this repo. A resolution failure for any individual task is
  non-fatal (that row's `activity` just stays `NULL`).
- **Bulk vs. per-task fetching for Activity.** Originally built as
  ~7,400+ individual API calls (one per task, 8 concurrent) because no bulk
  mechanism appeared documented. Teamwork's own
  [public API-Request-Examples repo](https://github.com/Teamwork/Teamwork.com-API-Request-Examples)
  showed `tasks.json?includeCustomFields=true` sideloads all values in bulk
  under `included.customfieldTasks` — essentially free, since it rides on
  the tasks pull that already happens. This is now the primary path; the
  per-task method is kept as an automatic fallback (triggers only if that
  sideload key is missing from the response) rather than removed, so a
  quirk on this specific account can't silently lose the whole feature.
  `--dry-run`'s diagnostic reports which path a real run would take.

## Files

- `sync.py` — entrypoint (`--dry-run`, `--explain-task-scope`,
  `--backfill-months`, `--create-views`, or a full sync)
- `config.py` — env var loading
- `teamwork_client.py` — Teamwork REST API client (auth, pagination, retries)
- `schemas.py` — BigQuery table schemas
- `transform.py` — raw Teamwork JSON → BigQuery row mapping
- `bigquery_sync.py` — dataset/table creation, truncate+load, the
  transactional current-month replace for timelogs
- `views.py` — the six exception/QC reporting views plus `v_usermins`,
  `v_user_daily_billable_hours_base`, and `v_user_weekly_billable_hours`
  (see "Exception reporting views" above)
