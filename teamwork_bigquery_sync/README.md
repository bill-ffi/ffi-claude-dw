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
- Projects/tasks scope: all projects except deleted ones (active, current,
  late, upcoming, completed, and archived are all included, per your
  instruction to "pull everything but deleted"). Tasks are filtered to
  belong to one of those in-scope projects.
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
(no extra joins needed in Looker). Plus two more that aren't exception
rules — `v_usermins` (see "External reference data" below) and
`v_user_daily_billable_hours` (see "User report" below). All seven are
defined in `views.py`; created/updated via:

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
| `v_exception_missing_activity` | Tasks with no "Activity" value set | Monitored categories only |
| `v_exception_missing_estimate` | Tasks with `estimate_minutes` NULL or 0 | Monitored categories only, minus the Client Management / Client Management v2 / HR Advisory tasklist exception within Non-Monthly |
| `v_exception_billable_time_internal_projects` | Billable timelogs (`minutes > 0`) posted to an internal-category project | `FFI Internal Projects`, `Functional`, or `Individual` category only |
| `v_exception_long_time_entries` | Timelogs over 2 hours | All projects (not category-scoped) |
| `v_exception_recurring_compliance` | Top-level tasks (no `parent_task_id`) in a "Books Maintenance"-category project with no `sequence_id` | Books Maintenance category only; sub-tasks excluded since they inherit recurrence from their parent and don't carry their own `sequence_id` |

**Assignee names, not IDs.** The three task-based views (missing_activity,
missing_estimate, recurring_compliance) surface a task's assignees as a
single `assignee_names` string column (e.g. `"Bob, Jane, Mary"`), built via
a `STRING_AGG` subquery over `tasks.assignee_user_ids` — not one row per
assignee. This keeps these views at one row per task (a task with 3
assignees used to show up as 3 duplicate rows before this). Trade-off: in
Looker Studio, filtering this column by one person needs a **"Text
contains"** filter condition rather than an exact-match dropdown, since
it's a free-text concatenation, not a clean dimension value. The two
timelog-based views (billable_time_internal_projects, long_time_entries)
are unaffected — a timelog only ever has one `user_id`.

**`proj_owner`** — the project's owner (`projects.owner_id`) resolved to a
name, on all five views (added anywhere project context already appears).

**`has_time_logged`** — boolean, true if any timelog with `minutes > 0`
exists against the task, via a correlated `EXISTS` subquery on `timelogs`.
Only added to the three task-based views (missing_activity,
missing_estimate, recurring_compliance) — on the two timelog-based views
this would be trivially true for every row (the row itself is a posted
time entry), so it wasn't added there.

**`user_email`** — added to the two timelog-based views
(billable_time_internal_projects, long_time_entries) alongside the existing
`user_name`, specifically so Looker Studio's built-in per-viewer row-level
security (data source setting: restrict rows by the viewer's login email)
can be turned on for these two — each timelog has exactly one `user_id`,
so this is a clean exact-match field. **Deliberately not added** to the
three task-based views (missing_activity, missing_estimate,
recurring_compliance): a task can have multiple assignees, and those views
already collapse to one `assignee_names` string per task (see above) to
avoid duplicate rows — a concatenated string can't satisfy Looker's
exact-match email security. Confirmed with you: these three views stay
one-row-per-task with no per-assignee security, rather than reverting to
one row per assignee just to enable it.

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

### User report: `v_user_daily_billable_hours`

A 7th view, also not an exception rule — feeds a per-user "billable hours
by day of week" report (current week vs. a prior-4-week baseline vs. each
person's daily minimum from `v_usermins`).

- **Weekday buckets, not calendar days.** Every timelog's `log_date` is
  bucketed into one of 5 weekdays: Sunday folds into Monday, Saturday
  folds into Friday — both within the *same* Monday–Sunday week (via
  BigQuery's `WEEK(MONDAY)` truncation, which groups a Sunday with the
  Monday that started its own week). If the intent was actually a
  Sunday–Saturday week (Sunday folding into the *next* Monday instead),
  this needs to change — flag it if the numbers don't look right.
- **Two series per user/weekday**: `Current Week` (this week so far —
  live, so future weekdays in the current week naturally show 0 until
  logged) and `Prior 4-Week Avg` (the four full Mon–Sun weeks immediately
  before this one). The average always divides by 4, treating a
  zero-hour day as a real 0 rather than excluding it — so a new hire's
  first month, or an inactive stretch, will pull the average down rather
  than being excluded from it. Say the word if that should change to
  "divide by weeks actually worked" instead.
- **`daily_min_bill`** rides along on every row (repeated per weekday/
  period) as the reference target — confirmed to represent an hours
  target, not a dollar figure (see `v_usermins` above; `daily_min_value`
  is something else and isn't used here).
- Scoped to users present in `v_usermins` (i.e. who have a minimum
  defined) via `CROSS JOIN` against a 5-row weekday scaffold, so every
  user always has all 5 weekdays × both periods present — even at 0
  hours — rather than gaps a bar chart would render inconsistently.
- **Two Looker Studio setups, same view, no SQL difference**: a "My
  Hours" page uses this view as a data source with row-level security on
  (restricted to the viewer's own `user_email`) for self-service viewing;
  a "Team Hours" page uses the *same view added as a second data source*
  without RLS, plus a user-picker filter control, for managers to look up
  anyone.

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

## Scheduling recommendation

Target cadence: daily at 5am and noon (pick the timezone via `SYNC_TIMEZONE`
and configure the scheduler in the same timezone — the two aren't linked
automatically).

Given this now lives in a GitHub repo, **GitHub Actions scheduled workflow**
is the simplest fit: no infra to stand up, secrets live in repo/org GitHub
Secrets (`TEAMWORK_API_KEY`, and the service-account JSON base64-encoded or
via Workload Identity Federation to avoid storing a long-lived key at all),
and a `cron` trigger covers twice-daily easily. Cloud Scheduler + Cloud Run
is the better fit only if this needs to live alongside other GCP-native
infra, needs sub-minute reliability guarantees, or the service-account key
should never leave GCP (Cloud Run can use the attached service account
directly with zero stored key material — arguably the more secure option
long-term). Plain cron is fine too if there's already an always-on host for
it, but adds a "did the box stay up" failure mode the other two don't have.

Not building the scheduler itself per your instructions — say the word and
I'll wire up whichever one you pick.

## Known gaps / things to verify before relying on this

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

- `sync.py` — entrypoint (`--dry-run` or full sync)
- `config.py` — env var loading
- `teamwork_client.py` — Teamwork REST API client (auth, pagination, retries)
- `schemas.py` — BigQuery table schemas
- `transform.py` — raw Teamwork JSON → BigQuery row mapping
- `bigquery_sync.py` — dataset/table creation, truncate+load, the
  transactional current-month replace for timelogs
- `views.py` — the five exception/QC reporting views plus `v_usermins`
  and `v_user_daily_billable_hours` (see "Exception reporting views" above)
