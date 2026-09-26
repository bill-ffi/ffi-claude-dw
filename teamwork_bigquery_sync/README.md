# Teamwork → BigQuery sync

Pulls Projects, Tasks, Users, and Time logs from Teamwork and loads them into
BigQuery (`radiant-rig-284611.teamwork_data`). Meant to run on a schedule
(twice daily), not inside a chat session — see **Scheduling** below.

## What it does

- **projects**, **tasks**, **users**: full truncate + reload every run.
- **timelogs**: a rolling **two-month** window is deleted and reinserted
  each run — the current calendar month plus the one before it (by
  `log_date`, derived from Teamwork's `timeLogged` field). Months older
  than that are left untouched. The span is set by
  `TIMELOG_SYNC_MONTHS_BACK` in `sync.py` (`1` = one previous month;
  `0` restores the old current-month-only behaviour). The previous month
  is included so that timelogs entered *retroactively* against a
  just-closed month still get picked up — see "Known gaps". The
  delete+insert is wrapped in a single BigQuery multi-statement
  transaction (via a staging table) so a mid-run failure can't leave the
  table half-deleted.
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
- Users scope: everyone on the account, **including deleted users** —
  they're kept (flagged via `is_deleted`) rather than dropped, so historical
  timelogs/tasks referencing them still resolve to a name instead of a
  dangling ID. This needs `showDeleted=true` on `people.json`
  (`USERS_LIST_PARAMS`); without it Teamwork returns only current people. That
  flag was missing until 2026-09-24 — see "Known gaps". `v_usermins` filters
  deleted users back out, so former staff never get a billing target.
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
   - `SYNC_TIMEZONE` — the timezone the timelogs window's calendar months should be
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

Seven BigQuery views, meant to be the direct data source for Looker Studio
reports for leadership — each one is filterable by user / project / client /
tasklist directly off its columns (no extra joins needed in Looker). Plus
nine more that aren't exception rules: `v_usermins` (see "External reference
data" below), the user report's `v_user_daily_billable_hours_base` /
`v_user_weekly_billable_hours` (see "User report" below),
`v_timelog_detail` (see "Drill-down reporting" below), `v_task_review`,
`v_project_detail`, `v_client_month`, `v_user_daily_time_split` and
`v_data_freshness` (see "Last updated" below). **Sixteen** views in total, all defined in `views.py`;
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
| `v_exception_missing_estimate` | Tasks with `estimate_minutes` NULL or 0; carries `has_parent_task` (sub-task vs top-level) | Monitored categories only, minus the Client Management / Client Management v2 / HR Advisory tasklist exception within Non-Monthly |
| `v_exception_billable_time_internal_projects` | Billable timelogs (`minutes > 0`) posted to an internal-category project | `FFI Internal Projects`, `Functional`, or `Individual` category only |
| `v_exception_long_time_entries` | Timelogs over 2 hours | All projects (not category-scoped), minus `LONG_ENTRY_EXEMPT_TASK_IDS` |
| `v_exception_time_without_task` | Time posted straight to a project with no task at all — no tasklist, no Activity, nothing to roll the work up against | All projects (not category-scoped); **windowed to the prior quarter + current QTD** |
| `v_exception_recurring_compliance` | Top-level tasks (no `parent_task_id`) in a "Books Maintenance"-category project with no `sequence_id` | Books Maintenance category only; sub-tasks excluded since they inherit recurrence from their parent and don't carry their own `sequence_id` |

### `v_client_month` — % of budget over a dynamic date range

One row per client per month: billable hours, billable revenue, and the
**monthly budget in force that month**.

**Why the grain matters.** A Looker Studio blend joins a client-level budget to
month-level revenue and contributes that budget **once**, no matter how many
months the reader selects. Revenue scales with the date filter; the denominator
does not — three months of revenue over one month of budget. Putting the
monthly budget on each month's row makes the denominator scale by itself:

```
SUM(billable_revenue) / SUM(monthly_budget)
```

is correct for one month, a quarter, or year-to-date, with no per-card
arithmetic. Supporting a *dynamic* period is the whole reason this view exists
rather than a calculated field.

**The view emits additive columns only — there is deliberately no
`pct_of_budget` or `is_over_budget`.** `monthly_budget`, `billable_revenue`,
`billable_hours` and `logged_hours` are zero-filled and sum cleanly over any
range; compute the percentage in the report as an aggregate over them.

A row-level percentage column shipped briefly and was removed on 2026-09-21
for two reasons, both worth remembering before anyone re-adds one:

1. **It cannot be aggregated.** Summing or averaging a percentage across
   months is wrong, so the column was only correct for a single month — and a
   *dynamic* range is the one thing this view exists to support. A column that
   looks usable and is not is worse than no column.
2. **It double-scales in Looker Studio.** This repo's `pct_*` columns are
   already multiplied by 100 (see `pct_of_estimate_used`,
   `pct_of_budget_used`), so applying Looker's native **Percent** type
   multiplies again: a real 128% rendered as **12,840%** in a live report.

Use this instead — correct for one month, a quarter or year-to-date, and it
returns a ratio that formats natively as Percent:

```
SUM(billable_revenue) / SUM(monthly_budget)
```

"Over budget" is the same expression compared to 1. Two tests enforce the
absence: one for these two column names, one asserting no non-additive measure
(`pct_`, `AVG(`, `ratio`) appears anywhere in the SELECT, comments excluded.

> ⚠️ **The x100 convention still applies to the other views.** `pct_of_budget_used`
> on `v_project_detail` and `pct_of_estimate_used` on `v_task_review` return
> e.g. `128.4`, not `1.284`. In Looker set those to **Number with a `%`
> suffix**, never the Percent type. They were left as-is rather than rescaled:
> changing their semantics would silently shift any report already reading
> them, and one rescaled column beside two that are not is worse than a
> consistent convention.

**Month spine.** One row per month each budgeted project is live, bounded by
that project's own `start_date` and `end_date` (per instruction), clamped below
by `CLIENT_MONTH_HISTORY_FLOOR` and above by the current month. No `end_date`
means ongoing. Budget therefore accrues only while the engagement is live —
charging a client for months before they onboarded would make the percentage
meaningless. Simulated across seven cases before shipping (started before the
floor, started and ended mid-window, ended before the floor, `end_date` in the
future, no dates at all, starts next month); an empty window yields no rows
rather than an error.

**`CLIENT_MONTH_HISTORY_FLOOR` is `2026-01-01`** because `timelogs` history
begins there. Without the clamp, a project that started earlier would get
budgeted months carrying a full month's budget against artificially zero
revenue, dragging every percentage down. Raise it only after backfilling the
corresponding months.

**A `FULL OUTER JOIN`** keeps both sides: a budgeted month with no time logged
still consumes budget (a quiet retainer month is real), and revenue on a
project with no budget still appears rather than vanishing.

> ⚠️ **While budgets are still being rolled out, any budget percentage reads
> high.**
> Revenue counts every project; only budgeted projects contribute a
> denominator. `project_count` exceeding `budgeted_project_count` on a
> client-month is the tell. A second, budgeted-projects-only revenue column was
> built and then **deliberately cut** — budgets are expected on all active
> projects imminently, and two revenue columns that converge to the same number
> would be permanent confusion for a temporary condition.

**Budget history is not modelled.** `transform.pick_current_budget()` keeps only
the active budget, so the current recurring figure is repeated across all
months. Accepted deliberately — the feature is new in Teamwork — but if a
recurring budget is ever edited, past months silently re-base. Fixing that
means persisting the budgets the pipeline already fetches and discards; see the
budget entry under "Known gaps".

### `v_project_detail` — the project-level Looker source

One row per **active** project (`archived_at IS NULL`, ~219 rows), the
projects-side counterpart to `v_timelog_detail` and `v_task_review`.

Built rather than pointing Looker Studio at the `projects` table directly, for
two concrete reasons: `owner_id`, `created_by` and `completed_by` are bare user
ids that are useless as report dimensions, and `tag_ids` is a **REPEATED**
column the Looker Studio connector cannot read. Resolving the ids needs a join
Looker can only fake with a blend; the view does it in BigQuery for free. It
also insulates reports from schema changes in the underlying table.

Carries: identity and the `/tasks/list` deep link, client and category, owner /
creator / completer **as names**, dates with `is_completed` and
`is_past_end_date`, budget columns in dollars with `pct_of_budget_used`,
`is_over_budget` and `has_budget`, plus task counts and time rollups.

**Scope uses the same predicate as `v_task_review`** — `archived_at IS NULL`,
not `status = 'active'`. The two are perfectly collinear on this account, so
they select identically today; using one spelling in both places means the two
views cannot quietly disagree about which projects exist if that ever changes.
A test asserts it. The raw `status` is still exposed as `project_status`.

**Deliberately excluded:**

| Column | Why |
|---|---|
| `tag_ids` | REPEATED, so Looker Studio cannot read it — and no tags table is ingested, so the only thing a view could emit is a string of bare ids with no names. Tag *names* would need new ingestion. |
| `health` | Best-effort on the standard payload and empty in practice (see `schemas.py`). Shipping a column NULL on every row is exactly what the two dead `web_link` columns were. |
| `cost_rate`, `user_cost`, `user_rate` | Comp-adjacent, same caution as `v_timelog_detail`, so this view can be shared more widely. A test enforces it. |

> ⚠️ **`logged_hours` and `budget_used` measure different things and will not
> reconcile.** `budget_used` is Teamwork's own budget tracking; `logged_hours`
> is summed from our `timelogs`, which only holds history the pipeline has
> loaded — currently from **2026-01-01**. Any project worked before then is
> understated. Use `budget_used` for budget consumption and `logged_hours` for
> "what did we actually record against this project". Reporting them side by
> side as if they were the same measure will produce questions nobody can
> answer.

Both rollups come from CTEs grouped by `project_id`, so each contributes at
most one row and the `LEFT JOIN`s cannot fan out — the view is one row per
project without a `DISTINCT`. A test pins that.

### `v_task_review` — the data-hygiene review surface

One row per task, every task **open and completed** whose project is not
archived. Unlike the `v_exception_*` rules it **asserts no policy and flags
nothing**: it exposes the dimensions and a set of `has_*`/`is_*` booleans, and
the reviewer decides what counts as a problem by combining filters in Looker
Studio. "Show me Books Maintenance tasks for this client, assigned to nobody,
with no estimate" is a filter combination here, not a new view.

Requested filter dimensions, all present: `proj_owner`, `project_name`,
`assignee_names`, `client_name`, `category_name`, `tasklist_name`, `task_name`,
`is_recurring`, `has_estimate`, `has_time_logged`.

Added beyond the request, in rough order of how often they earn their place:

| Column | Why |
|---|---|
| `task_url` | Teamwork deep link. A review tool people act on is far more useful when the fix is one click from the finding. |
| `has_assignee` / `assignee_count` | Unassigned work is usually the highest-value hygiene gap, and it isn't visible from any of the requested filters. |
| `has_activity` / `activity` | The same gap the two `missing_activity` rules chase, here as a filter instead of a fixed rule. |
| `has_due_date`, `is_overdue`, `days_overdue` | Scheduling hygiene. `is_overdue` is false for completed tasks — a late-but-finished task is not actionable. |
| `logged_hours`, `billable_logged_hours`, `time_entry_count` | Turns `has_time_logged` from a yes/no into "how much". |
| `estimate_variance_hours`, `pct_of_estimate_used`, `is_over_estimate` | Estimate quality, the natural follow-on to `has_estimate`. |
| `days_since_updated`, `days_since_last_time`, `last_time_logged_date` | Staleness — an open task untouched for months is a different finding from one with a missing field. |
| `has_description`, `is_private`, `has_parent_task`, `priority`, `progress_pct` | Cheap dimensions that make the filter set materially more expressive. |
| `task_status`, `is_completed`, `project_status`, `project_is_billable` | Needed to separate the open and completed halves, since both are in scope. |
| `hygiene_gap_count` | Convenience only — counts **four** gaps (no assignee, no estimate, no activity, no due date) so a reviewer can sort worst-first. The individual booleans are the source of truth. `has_description` and `has_time_logged` are deliberately **not** counted: both are near-constants here (~90% of open tasks have no description, measured 2026-09-14), so including them offsets every score instead of discriminating between tasks. Both remain filter columns. |

**Why both views carry the rollup: a monthly close spans three calendar
months.** Per the domain owner, for a close period such as "Monthly Close
2026-08", some time posts in **August**, the bulk posts in **September** (the
month after the period being closed), and a close that runs long posts again in
**October**.

That makes the two views genuinely complementary rather than redundant, and
neither can answer the other's question:

| Question | View | Why |
|---|---|---|
| Did this close land on estimate? | `v_task_review` | Lifetime `logged_hours` vs `estimate_minutes`, period-agnostic. A monthly slice only ever shows a fragment of a close, so a month-grained view cannot answer this at all. |
| *When* did the effort land? | `v_timelog_detail` | `log_date` pivoted by month shows the Aug / Sep / Oct spread. Task grain has no date and cannot show it. |

A close still accruing time in its third month is the signal worth watching,
and it is only visible in the timelog-grained view. Do not "simplify" by
retiring either lens.

**`rollup_task_id` / `rollup_task_name` also exist on `v_timelog_detail`**, at
timelog grain. That is the view to use for anything **month-by-month**:
`v_task_review` is one row per task carrying lifetime totals, so it has no date
to pivot on. Same derivation in both, and a test asserts the two cannot drift.
On `v_timelog_detail`, project-level time (no task at all) leaves both columns
NULL rather than inventing a group — `task_join_status` already names that
population.

> ⚠️ **`estimate_minutes` on `v_timelog_detail` is the TASK's estimate repeated
> on every time entry for that task.** `SUM` multiplies it by the number of
> entries. It is safe to filter or display, never to total. For estimate-vs-
> actual at task grain use `v_task_review`, which carries `estimate_minutes`,
> `logged_hours` and a precomputed `pct_of_estimate_used` on one row per task.

**Rolling sub-tasks up to their parent.** `parent_task_id` alone cannot drive
a grouped report: it is NULL on top-level tasks, and an integer is not a
readable dimension. Three columns handle it —

| Column | Meaning |
|---|---|
| `parent_task_name` | The parent's name; NULL on a top-level task. |
| `rollup_task_id` / `rollup_task_name` | **Group a report by these.** A sub-task reports its parent; a top-level task reports itself. A parent and its sub-tasks therefore land on one line. |

The pair branches on a *single* condition — whether the parent actually
resolved — so the id and the name always describe the same task. Coalescing
them independently (`COALESCE(t.parent_task_id, t.task_id)` alongside
`COALESCE(parent.name, t.name)`) would pair a parent's id with a child's name
whenever the parent is missing from `tasks` (deleted, or outside the pull). A
test asserts the independent form is *absent*.

> ⚠️ **One level only.** `parent_task_id` is the *immediate* parent, so a
> sub-sub-task rolls up to its own parent, not to the top of the tree. If
> deeper nesting ever matters, that needs a recursive CTE, not another join.

The self-join is `LEFT JOIN tasks parent ON parent.task_id = t.parent_task_id`.
`tasks` is de-duplicated by `task_id` in the pipeline, so it matches at most
one row and cannot fan out the grain; an inner join would silently drop every
top-level task. Tests cover both.

**Deliberately excluded**: `cost_rate`, `user_cost`, `user_rate`. Same caution
as `v_timelog_detail` — leaving comp-adjacent columns out lets this view be
shared more widely than the `users` table. A test enforces it.

**Grain is one row per task**, per your instruction: assignees are concatenated
into a single `assignee_names` string. Task counts are therefore always
correct with no dedupe step, but **an assignee filter in Looker Studio must be
a "Text contains" control, not an exact-match dropdown** — the distinct values
are combinations ("Bob, Jane") rather than people. The alternative considered
was fanning out one row per task-per-assignee, which gives a clean dropdown but
double-counts every multi-assignee task; that trade was declined.

> ⚠️ **`has_time_logged` means "no time in *loaded* history", not "never".**
> `timelogs` holds only what the pipeline has ingested, which currently begins
> **2026-01-01**. A task whose only time was posted before then reads
> `has_time_logged = FALSE`, and `days_since_last_time` is NULL. Before
> concluding an older task was never worked, widen the history with
> `--backfill-months`. Every column derived from time inherits this.

A task whose project row is missing entirely also passes the archived filter
(the project join is a `LEFT JOIN`). That is the intended direction for a
review tool: an orphaned task surfaces rather than silently disappearing.

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
confirming against a known-completed task's row in BigQuery. The filter is
written `COALESCE(t.status, '') != 'completed'` rather than
`t.status != 'completed'`, so a task with a NULL status counts as
still-open rather than vanishing — see "Known gaps".

**`has_parent_task`** — on `v_exception_missing_estimate` only. Boolean,
`TRUE` where `tasks.parent_task_id IS NOT NULL`, i.e. the task is a
**sub-task**; `FALSE` for a top-level task. Added so the missing-estimate
report can separate the two, since an estimate on a parent and an estimate
on each of its sub-tasks are different expectations.

This is the same test `v_exception_recurring_compliance` already uses to
mean "top-level" (`t.parent_task_id IS NULL`), just inverted — the two rules
agree on what a parent is.

Deliberately **not** derived from `sequence_id`, which is the
recurring-series identifier and a different thing entirely: sub-tasks
inherit recurrence from their parent and carry no `sequence_id` of their
own, so the two columns are close to mutually exclusive. A test asserts the
derivation so it cannot quietly drift.

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

### Untasked time: `v_exception_time_without_task`

Teamwork allows logging time straight to a project without naming a task.
That leaves the work undescribed — no tasklist, no Activity, nothing to roll
it up against — so this view surfaces it.

**Built on `v_timelog_detail`, not on `timelogs`.** "No task" therefore has
a single definition, and any fix to the underlying joins or the derived
`hours` propagates here for free. That makes creation order matter:
`v_timelog_detail` must exist first, and `create_or_replace_views()`
iterates the dict in insertion order, so this entry stays after it. A test
asserts that ordering.

The filter is `d.task_id IS NULL`, not a string comparison against
`task_join_status`. Same population, but it cannot silently break if that
label's wording is ever edited.

**Window: from the start of the prior quarter onward**, evaluated in
`REPORTING_TIMEZONE`. In practice that is "prior quarter + current QTD",
since there is normally no data after today.

- `quarter_bucket` splits the two periods (`Current QTD` / `Prior quarter`)
  so a Looker report can show them side by side, and `quarter_label`
  (e.g. `2026-Q3`) gives a clean grouping dimension.
- **There is deliberately no upper bound.** Capping at `CURRENT_DATE` would
  hide a timelog dated in the future — which is itself an anomaly worth
  seeing on an exception report. Verified against the quarter boundaries
  before shipping, including the January case where the window has to reach
  back into the prior year (on 2027-01-15 the window opens at 2026-10-01).

**Scope is site-wide**, not restricted to `MONITORED_CATEGORIES` —
following `v_exception_long_time_entries` rather than rules 1/2/5. Untasked
time on an internal or uncategorised project is as much a gap in the record
as on a billable one. Say the word if it should be narrowed.

Every row carries `user_name`, `user_email`, `project_name`, `client_name`,
`proj_owner`, `hours`, `billable_status` and the timelog's own description,
so a follow-up can go straight to the person and the project without
another join.

### Drill-down reporting: `v_timelog_detail`

Not an exception rule and not part of the user-hours report — a wide,
unfiltered Looker Studio data source with **one row per time entry**,
joined out to project, client, person, tasklist and Activity.

Grain is guaranteed one row per timelog: a timelog has exactly one user,
one project and at most one task, and `tasks` is de-duplicated by `task_id`
in the pipeline, so no join can fan out. Every join is a `LEFT JOIN` — an
inner join anywhere would silently drop time entries, which is the one
thing a drill-down over timelogs must never do.

Three things it does differently from the two exception views, because a
drill-down surfaces rows those rules filter away:

- **`task_join_status`** — explains *why* the task columns are blank on a
  row, which they can be for two unrelated reasons:
  `'No task (project-level time)'` (Teamwork allows logging time straight
  to a project) versus `'Task outside tasks-table scope'` (the task exists
  in Teamwork but `tasks` covers only active projects plus those archived
  on/after the cutoff — 823 of 1,889 projects — while `timelogs` is scoped
  only by date and so spans all of them). **Without this column a blank
  Activity reads as a compliance failure when it is often just an
  out-of-scope project.** Filter to `'Task matched'` before drawing any
  conclusion about Activity coverage.
- **`hours` is computed as `minutes / 60`**, not read from `timelogs.hours`,
  which the pipeline stores pre-rounded to 4 decimal places. Per-row
  rounding is invisible on one entry and accumulates when Looker SUMs tens
  of thousands. `minutes` is what Teamwork actually holds.
- **`billable_status`** is a string (`Billable` / `Non-billable` /
  `Unknown`) because `is_billable` can be NULL, and a NULL boolean drops
  out of *both* sides of a Looker Yes/No filter. The raw boolean is kept
  alongside it.

`log_week_start` uses the same Sunday-start week as
`v_user_daily_billable_hours_base`, so the two reports agree on which week
a date belongs to — Looker's own week grouping defaults to Monday and would
quietly disagree.

`user_email` is included, so Looker's per-viewer row-level security works
on this view (a timelog has exactly one user, so it is a clean exact-match
field — unlike the task-based views, see above).

**Money columns**: `billable_rate` and a derived `billable_amount`
(`hours x rate`, computed **only** where `is_billable IS TRUE`) are
included. `cost_rate` is **not** — it is comp-adjacent, same caution as
`users.user_cost`/`user_rate`, and leaving it out lets this view be shared
more widely than the `users` table. Margin analysis would need it; ask.

`billable_amount` is NULL, not 0, for non-billable entries and for a
billable entry with no rate. `SUM` skips NULLs, so a missing rate
understates revenue rather than inventing zero-value billable work.

**`billable_rate`'s units were verified against known rates on 2026-09-14:
it arrives in dollars, so `billable_amount` is correct as published.**

This is worth stating explicitly because **the two money fields on this
account do not agree with each other**, and the inconsistency looks like a
bug in either direction:

| Field | Endpoint | Arrives as | Handled in |
|---|---|---|---|
| `billableRate` | `time.json` | **dollars** | passed through unchanged |
| `userRate` / `userCost` | `people.json` | **cents** | divided by 100 in `transform.normalize_user()` |

So `transform.normalize_timelog()` passing `billableRate` straight through
is **correct, not an oversight**, and the `/100` in `normalize_user()` is
equally correct. Do not "make them consistent" by adding a division to
`normalize_timelog()` — that would make every revenue figure 100x too
small — and do not remove the one in `normalize_user()`. This is Teamwork's
inconsistency, not the pipeline's, and it is the same class of per-endpoint
surprise as the item-key casing noted above: confirm units per endpoint
rather than generalizing from a sibling field.

To re-check after any Teamwork-side change, pick a person whose rate you
know:

```sql
SELECT user_name, billable_rate, COUNT(*) AS total_rows
FROM `radiant-rig-284611.teamwork_data.v_timelog_detail`
WHERE billable_rate IS NOT NULL
GROUP BY 1, 2 ORDER BY total_rows DESC LIMIT 20;
```

`150` for someone billed at $150/hr is dollars (the current, expected
state); `15000` would mean it had switched to cents, and the fix would then
be one line in `transform.py` plus a re-sync — **not** a division in the
SQL, which would leave the underlying table wrong for every other
consumer.

**Cost note**: this view is unbounded — it reads all of `timelogs` on every
query, and that table is neither partitioned nor clustered. A Looker report
without a date filter will full-scan it each refresh. Partitioning
`timelogs` on `log_date` is the fix and needs a one-off table rebuild; see
"Known gaps".

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
- **Both are HOURS, not money** (confirmed 2026-09-22), despite the name
  "value": `min_bill` is minimum weekly **billable** hours, `min_value` is
  minimum weekly **value-added** hours — always non-billable, so it has no
  bearing on revenue. They sum to roughly 30–35 hours for everyone on the
  sheet; partners invert the split. The derived `daily_min_value` /
  `wkly_min_value` read like currency and are **not**; they keep their names
  so existing reports don't break. Reading `min_value` as a dollar minimum
  was nearly used to project revenue, which would have put most people at
  about $0.20 of projected revenue per day.
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

**`billable_revenue` and `week_label` reach the weekly view too (2026-09-22).**

`billable_revenue` **is projected for the current week, mirroring hours
exactly**, at each person's **standard rate** (`user_rate`):

| | Hours | Revenue |
|---|---|---|
| `actual` rows | real, zero-filled | real (per-entry `billable_rate`), zero-filled |
| `minimum` rows | `daily_min_bill` | `daily_min_bill × user_rate` |
| `plug` row (Fri) | `GREATEST(daily_min_bill×5 − non_friday_total, 0)` | `GREATEST(daily_min_bill×5×user_rate − non_friday_revenue, 0)` |
| target column | `daily_min_bill` | `daily_min_revenue` |

`current_week_pace` carries a second sum, `non_friday_revenue`, built the same
way as `non_friday_total`: actual revenue for elapsed Mon–Thu buckets, the
standard-rate target for buckets not yet elapsed.

**Why this earns its place: the revenue plug diverges from the hours plug by
exactly the rate shortfall.** Elapsed days count *actual* revenue while the
target is at *standard* rate. Simulated before shipping: a person on target for
hours but billed at $100 against a $150 standard shows an hours plug of 6.0 —
apparently fine — and a revenue plug of **$1,500 instead of $900** by Wednesday,
**$2,100** by Friday. The gap is two (or four) days × 6 h × $50 of discounting,
which the hours view cannot see at all. In every case with a positive plug, the
week lands exactly on the weekly target; overshooting clamps both plugs at 0;
Saturday produces no projected rows. A NULL `user_rate` makes the projection
NULL — unknown, rather than a misleading zero.

`daily_min_revenue` is additive, so `SUM(billable_revenue) / SUM(daily_min_revenue)`
is correct over any range. **No row-level revenue percentage is emitted**, for
the same reasons `v_client_month` dropped its own: it cannot be aggregated, and
the repo's `pct_*` convention double-scales under Looker's Percent type.

> **`min_value` plays no part.** It is minimum weekly *value-added* hours —
> always non-billable — not a dollar minimum, despite the name (confirmed
> 2026-09-22; see the `v_usermins` notes). It was nearly used as the revenue
> basis on the strength of its name, which would have projected about $0.20 a
> day for most people. A test asserts it appears in no code line of this view.

`week_label` is a **STRING** (`YYYY-MM-DD`) on both this view and the base.
A DATE dimension makes Looker Studio plot a **daily** axis, so weekly totals
land on Sundays with six empty days between and the line collapses to zero in
the gaps. A string forces categorical spacing, sorts chronologically as text,
and preserves the Sunday boundary rather than letting Looker re-bucket on its
Monday-based ISO week.

> ⚠️ **This view is a `UNION ALL`, which matches branches POSITIONALLY.**
> Adding a column to one branch and not the other — or at a different position
> — either fails the union or silently shifts every later column's meaning. A
> test extracts each branch's ordered output aliases and asserts they are
> identical; add any future column to both branches at the same position.
>
> The `actual_hours` CTE must also carry any base column the outer query
> references. Adding `ah.billable_revenue` without adding
> `base.billable_revenue` to that CTE is an unresolved reference that **only
> BigQuery rejects** — the text-based tests cannot see it. That mistake was
> made and caught during this change; a test now pins both halves.

**Layer 1 — `v_user_daily_billable_hours_base`**: the shared aggregation.
One row per `(user, day_bucket, week_start)` — real, historical
`SUM(hours)` from billable timelogs, with the weekday-bucketing rule
applied exactly once. Deliberately sparse (no user scaffold, no
`daily_min_bill`, no zero-filling) and unbounded (all history, since it's
a view — cost is incurred at query time, and the one consumer below
filters to whatever range it needs).

**`billable_revenue` was added alongside `hours` (2026-09-21)** — same
bucket, additive, so it sums over any range. It is derived from
`(minutes / 60) * billable_rate`, **not** from the `hours` column beside it,
so revenue means exactly what it means in `v_timelog_detail.billable_amount`
and `v_client_month.billable_revenue`. `timelogs.hours` is stored pre-rounded,
so `hours * rate` would be a second, subtly different definition that would
not tie back to those views.

> The deliberate consequence: **within this view, `billable_revenue / hours`
> does not exactly equal the billable rate**, because the two columns rest on
> different bases — exact minutes vs pre-rounded hours. `hours` was left alone
> rather than rebased, because changing it would silently shift every number
> `v_user_weekly_billable_hours` has ever reported.

The addition was safe precisely because **`v_user_weekly_billable_hours` reads
this view with an explicit column list, never `SELECT *`.** That matters more
than usual: the dependent is a `UNION ALL`, so a `SELECT *` would change one
branch's column count the moment anyone adds a field here, breaking the union
outright. A test asserts the explicit list, and another asserts the weekly
view did *not* pick the new column up — additive means additive. Keep that
property for any future column.

A NULL `billable_rate` yields NULL and is skipped by `SUM`, so a missing rate
understates revenue rather than valuing the work at zero. Measured
2026-09-21: **0 of 18,184** billable entries lack a rate, so nothing is lost
today; the behaviour is the guard for a person added later without one.

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

### Time mix: `v_user_daily_time_split`

One row per **logged date × user × client × Activity**, with the hours split
into columns so a Looker table or stacked bar can show how each person's time
divides between internal work and client work:

| Column | What goes in it |
|---|---|
| `pto_hours` | All time on the PTO task, **47878044** (`PTO_TASK_ID` in `views.py`), whatever its client or billable flag. Tested first, so leave never counts as internal work |
| `internal_hours` | All other time on Forward Financial Intelligence, Inc. — Teamwork company id **1380118** (`INTERNAL_CLIENT_COMPANY_ID` in `views.py`) — billable or not |
| `client_billable_hours` | Time on any other client, marked billable |
| `cnb_hours` | Time on any other client, not marked billable (client non-billable). An entry whose billable flag is blank also lands here: only time Teamwork positively marks billable counts as billable |
| `no_client_hours` | Time on a project with no client set. Neither internal nor external, so it gets its own column rather than being guessed into one. If those projects are really internal, change the view's CASE rather than patching it in the report |
| `total_hours` | All of the above. The five columns always add up to this |

Also carries `log_date`; `week_start`, the **Sunday** that begins the entry's
week (the same Sunday-start week as every other report), for rolling days up to
weeks in Looker; `week_label`, its text form, for chart axes (a DATE dimension
makes Looker plot a daily axis with gaps); `user_name`, `user_email`,
`company_id`, `client_name`, `client_type` (`Internal` / `External` /
`No client`, handy as a filter) and `activity`, plus `time_entry_count`.

Things to know:

- **PTO is split out of internal (added 2026-09-24).** PTO is posted to a task
  on the FFI client, so before this it was counted in `internal_hours`. Staff
  often post PTO ahead of the leave, so `pto_hours` can appear on future dates;
  that is planned leave, not an error. `client_type` still describes the
  client, so a PTO row reads `Internal` there — use the hour columns, not
  `client_type`, to separate leave from internal work. The same `PTO_TASK_ID`
  drives the long-entry exemption, so the two cannot drift apart.
- **Internal is decided by company id, not by name.** A rename in Teamwork
  cannot move internal time to CNB, and a different company that happens to
  share the name is still external. To support this, `v_timelog_detail` now
  carries `company_id` too.
- **The client test comes before the billable test.** Billable time on the
  internal client counts as internal, not client billable.
  `v_exception_billable_time_internal_projects` is where such entries get
  flagged; this view only sorts time.
- **`activity` is the task's Activity**, so it is blank for time logged
  straight to a project (no task) and for tasks outside the tasks-table scope.
  Those rows still carry their hours.
- All hour columns are additive, so they sum correctly over any range of days,
  weeks, users or clients. For a percentage (e.g. share of time that is client
  billable), compute it in the report as
  `SUM(client_billable_hours) / SUM(total_hours)`; don't average row-level
  ratios.
- Built on `v_timelog_detail`, so it is created after it, and its hours come
  from exact minutes like every other time report.

### Last updated: `v_data_freshness`

One row. Its subject is the pipeline rather than the business — it exists so
a report can show "last updated at" without every report re-deriving it.

**The problem it solves.** `synced_at` is a BigQuery `TIMESTAMP`, which is an
absolute instant — `transform.utc_now_iso()` writes it correctly. But Looker
Studio *renders* a TIMESTAMP in UTC, so the 09:53 UTC sync of 2026-09-21
displays as a mid-morning refresh when it actually ran at 05:53 ET. Looker
gives you no timezone-aware conversion for a field; the only in-Looker option
is a fixed offset:

```
DATETIME_SUB(synced_at, INTERVAL 4 HOUR)     -- DO NOT
```

which is silently an hour wrong from November to March — the same shape as
the `CURRENT_DATE()` bug in "Known gaps". The conversion therefore happens
here, in SQL, where `DATETIME(ts, tz)` resolves DST off the tz database.

| Column | Use |
|---|---|
| `last_synced_at_et` | **The display column.** A BigQuery `DATETIME` carries no timezone, so Looker shows it verbatim instead of re-reading it as UTC |
| `last_updated_label` | The same instant pre-formatted, e.g. `Sep 21, 2026 at 05:53 AM`, for a scorecard that wants a sentence rather than a date widget |
| `last_synced_at_utc` | The raw TIMESTAMP, for anything that wants to do its own arithmetic |
| `minutes_since_sync` / `hours_since_sync` | Age. "05:53 AM" means nothing to a reader who doesn't know what normal looks like |
| `is_stale` | Age ≥ `DATA_FRESHNESS_STALE_AFTER_HOURS` |
| `tables_reporting` | Should always be 4. Less means a table this view vouches for is empty |
| `newest_synced_at_et`, `stage_skew_minutes` | Diagnostics — see below |
| `projects_synced_at_et`, `tasks_synced_at_et`, `users_synced_at_et`, `timelogs_synced_at_et` | Per-table, for when the skew is non-zero and you want to know which stage is behind |

**Verified live 2026-09-21**, the first query against the created view, at
~13:05 UTC against a sync that ran at 09:53 UTC:

```
last_updated_label          hours_since_sync  is_stale  tables_reporting  stage_skew_minutes
Sep 21, 2026 at 05:53 AM    3.2               false     4                 1
```

Every column matched prediction. `05:53 AM` is the one that matters — a
broken conversion would read `09:53`, which is exactly the symptom this view
exists to remove. `stage_skew_minutes` of 1 matches the run's own log
(`projects` written 09:53:16, `timelogs` finished 09:54:53), confirming the
`MIN` headline was not masking a failed stage, and `tables_reporting` of 4
confirms no table was silently absent. Recorded because there is no local
BigQuery emulator: until this query, the SQL had only been read and
simulated, never executed.

**Two things in the SQL that look like mistakes and are not.**

*`MAX` within each table, then `MIN` across them.* `MAX` within is required
because `timelogs` is a **windowed** replace: only the rolling window's rows
get a fresh `synced_at`, so an untouched January row keeps a months-old
stamp, and a `MIN` within that table would report it as the sync time. `MIN`
*across* the four is the honest headline — the pipeline fails a stage rather
than writing bad data, so a partial failure leaves one table stale while the
rest are current, and `MAX` across would claim a freshness the dashboard
doesn't have. `last_synced_at_et` therefore means "every table is at least
this fresh". `stage_skew_minutes` is what exposes the case the `MIN` is
hiding from the reader: within a healthy run the four stages land seconds to
a couple of minutes apart, so hours of skew means a stage failed.

*`CURRENT_TIMESTAMP()` carries no timezone argument*, unlike every
`CURRENT_DATE()` in `views.py`. That is correct: a TIMESTAMP is an absolute
instant and `TIMESTAMP_DIFF` between two of them is timezone-independent.
The timezone only matters when rendering.

**`DATA_FRESHNESS_STALE_AFTER_HOURS` is 18, deliberately not 12.** The cron
implies a 12-hour cycle; GitHub actually delivers the two firings 9.5-15.1h
apart (see "Known gaps"). A 12h or even 16h threshold would read "stale"
during entirely normal operation and train everyone to ignore the indicator.
18h clears the measured worst case with headroom and still catches a genuinely
missed sync, which lands at 24h+. Worth tightening if the Cloud Run migration
lands and real scheduling guarantees arrive.

**Set the Looker Studio data source's freshness to the shortest option.** A
BigQuery data source caches for 12 hours by default, which means a "last
updated" scorecard can itself be twelve hours out of date — the worst possible
failure mode for this particular widget, since it fails in the direction of
false reassurance.


## Backfilling history

The normal run only covers a rolling two-month window (see **What it does**
above), so it won't populate anything older on its own. To load history —
e.g. the months before this script started running, or to repair a month
that has since aged out of the rolling window — use `--backfill-months`:

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
twice daily. Cron: `30 3,15 * * *` — 03:30 and 15:30 UTC.

| Nominal (UTC) | Eastern (EDT, ~Mar-Nov) | Eastern (EST) |
|---|---|---|
| 03:30 | 23:30 *(previous day)* | 22:30 *(previous day)* |
| 15:30 | 11:30 | 10:30 |

Moved 45 minutes earlier from `15 4,16` on **2026-09-21**, targeting
10:30 AM/PM Eastern — which is the **EST** reading, so until the clocks
change it lands at 11:30. See "Known gaps" for what this experiment can and
cannot show.

- **Evenly spaced on paper**, 12 hours apart — but see the delay data
  below: in practice *nothing fires on time*, and the real gaps run
  9.3-15.2h.
- **Cron is UTC and DST-unaware**, so the Eastern clock times shift by an
  hour once the clocks change. That drift is **accepted deliberately** —
  holding midnight/noon Eastern year-round would mean firing at all four
  candidate UTC hours and having the job no-op unless the local hour
  matched, which is more machinery than an hour of drift on a reporting
  refresh justifies. Say the word if it ever does matter.
- **`schedule:` only fires from the default branch (`main`).** A cron edit
  on a feature branch does nothing until merged.
- **Moving the cron off `:00` was a mitigation that did not work.** Measured
  over 33 consecutive firings (2026-09-05 to 2026-09-21) on the previous
  `15 4,16`: median delay ~4h 20m, and *not one run fired on time*. Under
  that schedule the effective times were roughly **05:00 and 15:00 ET**, not
  the nominal midnight and noon. The 2026-09-21 shift to `30 3,15` does not
  change that mechanism — see "Known gaps".
- `SYNC_TIMEZONE` (`America/New_York`, a workflow env var) controls the
  calendar months of the timelogs window. It is independent of the cron's
  UTC timing — the two are not linked automatically.
- `timeout-minutes: 60` bounds the job. A hung run would otherwise hold the
  `concurrency` group and silently stall every later scheduled sync.

**Cloud Scheduler + Cloud Run is the recommended next step** — planned in
detail in [`docs/cloud-run-migration.md`](../docs/cloud-run-migration.md),
including a blocker (the shared `timelogs__staging` table) that must be
fixed in code before any cutover, no longer a
speculative fallback — the delay data below is now strong enough to justify
it. Two benefits: real timing guarantees that GitHub's best-effort scheduler
does not give, and the GCP service-account key stops being a GitHub secret
entirely (Cloud Run uses an attached service account, so the key never
leaves GCP).

## Known gaps / things to verify before relying on this

- **Every table and view was set to delete itself after 60 days (fixed
  2026-09-26).** The dataset carried a default table expiration of 60 days —
  set outside this pipeline, which never sets one — so every object got a
  deletion date 60 days after it was created. `projects`, `tasks` and
  `timelogs` were due on **2026-10-25**, `users` on 10-26 and
  `gs_minimum_user_info` on 10-28. Found by accident while adding a column
  to the sheet.
  - **Why nothing noticed:** a truncate-and-load keeps a table's original
    expiration rather than resetting it, so twice-daily reloads never moved
    the date. Views looked safe only because each `--create-views` rebuilds
    them and restarts their clock.
  - **What losing it would have cost:** `projects`/`tasks`/`users` rebuild on
    the next sync, but `timelogs` reloads only its rolling window — January
    to July would have been gone until re-pulled with `--backfill-months`.
  - **The fix, run by hand as the project owner:** `ALTER SCHEMA ... SET
    OPTIONS (default_table_expiration_days = NULL)`, then `ALTER TABLE ...
    SET OPTIONS (expiration_timestamp = NULL)` on the four native tables, then
    `--create-views` to rebuild the views without dates. The external sheet
    table rejects `ALTER TABLE SET OPTIONS` and had to be recreated with
    `CREATE OR REPLACE EXTERNAL TABLE` instead.
  - **The detector:** every full sync and `--create-views` now carries
    `table_expirations` in `RUN_SUMMARY` —
    `{"dataset_default_expiration_days": null, "expiring": []}` is healthy,
    `null` means the check could not run — and logs a `WARNING` with the exact
    `ALTER` statement when anything has a date. `timelogs__staging` is skipped:
    it is scratch space rebuilt every run.

- **Former staff's time had no name, for months (fixed 2026-09-24).** The
  README and `list_users()`'s docstring both said deleted users were loaded,
  but the call sent no params, and `people.json` returns only current people
  unless asked. Two former staff — user ids 700802 and 646926 — owned 857
  timelogs (424.6h, January to July) that read with a blank `user_name` in
  every view. Nothing warned: `timelogs.user_id` was 100% filled, so the fill-
  rate check was satisfied; the gap was only visible after a join.
  - **What was tried, live, via `--dry-run` (runs #128 and #129):**
    `showDeleted=true` returns 20 people vs 16, the four extras all
    `deleted: true` and including both missing ids — adopted.
    `includeDeleted=true` is silently ignored (still 16). The v1
    `/people/deleted.json` endpoint answers but returns no one. The v3
    `/people/{id}.json` endpoint 404s for a deleted id.
  - **A knock-on that had to be handled:** `v_usermins` inner-joins `users` to
    the compensation sheet, so it had excluded former staff only because they
    were never loaded. It now filters `is_deleted IS NOT TRUE` explicitly;
    otherwise anyone still on the sheet would reappear in
    `v_user_weekly_billable_hours` with a target and a projected plug.
  - **The detector that was missing:** every full sync's `RUN_SUMMARY` now
    carries `unresolved_timelog_users` — timelog user ids with no `users` row,
    across all loaded history — and logs a `WARNING` when it is non-empty.
    `[]` is healthy; `null` means the check itself could not run. The dry run's
    "Former-staff diagnostic" also confirms the flag still adds people.

- **A live `--create-views` failed on a GROUP BY the text tests could not see
  (2026-09-22).** `week_label` — `FORMAT_DATE(..., DATE_TRUNC(tl.log_date,
  WEEK))` — was added to `v_user_daily_billable_hours_base`'s SELECT but not its
  `GROUP BY`. BigQuery rejected it: *"SELECT list expression references
  tl.log_date which is neither grouped nor aggregated."* The view groups by the
  **alias** `week_start`, and BigQuery does not infer that a new expression is
  derived from a grouped alias. Every text assertion passed.
  - **No outage.** `create_or_replace_views()` replaces views independently, so
    the base kept its previous definition (which already carried
    `billable_revenue`) and the weekly view compiled against it. Only the base's
    new label was missing until the fix shipped.
  - Fixed by grouping `week_label` explicitly — a pure function of
    `week_start`, so the grain is unchanged.
  - **Now guarded structurally**: a test parses the base view's SELECT and
    asserts every non-aggregated column appears in the `GROUP BY`, so the next
    plain column added there fails the suite rather than a deploy.
  - The general lesson, same family as the unresolved `ah.billable_revenue`
    reference caught earlier the same day: **SQL scoping and grouping rules are
    enforced only by BigQuery.** Text-level tests confirm what the SQL *says*,
    not whether it is *valid*. Where a rule can be checked structurally, check
    it that way.

- **Project budgets were stored in cents, not dollars (fixed 2026-09-19).**
  `budget_capacity` and `budget_used` come from the budgets endpoint's
  `capacity`/`capacityUsed`, which Teamwork returns as integer **cents**, and
  were passed straight through — so every budget figure read 100x too large.
  Confirmed by hand against three projects before changing anything.
  `transform.cents_to_dollars()` now converts on the way in, and `budget_left`
  derives from the converted values (equivalent to converting the difference).
  - **This is the third money field with its own unit, and the three do not
    agree.** That makes it a rule rather than a quirk:

    | Field | Endpoint | Arrives as | Treatment |
    |---|---|---|---|
    | `userRate` / `userCost` | `people.json` | cents | `/100` |
    | `capacity` / `capacityUsed` | `budgets.json` | cents | `/100` |
    | `billableRate` | `time.json` | **dollars** | passed through |

    **Confirm the unit per endpoint against a known value; never infer it from
    a sibling field.** Applying `cents_to_dollars()` to `billableRate` would
    understate every revenue figure by 100x — a test asserts it stays
    unconverted, specifically to stop someone "finishing the job".
  - `projects` is truncate-and-reload, so one full sync corrected all rows;
    no backfill was needed.
  - **`budget_type` is deliberately NOT captured.** Teamwork also supports
    *time* budgets, whose `capacity` is **minutes** — on which this conversion
    would be wrong. Every budget checked on this account is financial, and
    sourcing a type column means guessing a payload key, which is the exact
    mistake that produced the two dead `web_link` columns. If a time budget is
    ever configured, the symptom is a project whose budget reads as an
    implausibly small dollar figure; confirm the payload key first, then
    convert conditionally.

- **`web_link` is empty on BOTH `tasks` and `projects`, and always has been
  (confirmed 2026-09-15).** Measured: 0 of 14,595 tasks and 0 of 1,890
  projects carry a value. `transform.normalize_task()` and
  `normalize_project()` both read it as
  `(raw.get("meta") or {}).get("webLink")`, and Teamwork v3 does not return a
  `meta.webLink` on either payload. A missing dict key yields `None`, so this
  never raised — the column simply materialized empty on every run since it
  was added, and nothing noticed until `v_task_review` exposed it as
  `task_url` and a `WHERE task_url IS NOT NULL` returned zero rows.
  - Same root cause as the `included`-sideload and item-key-casing entries
    below: **a payload key that was assumed rather than confirmed**. The
    defensive pattern this repo uses elsewhere — try candidate keys, log a
    warning when none match — would have surfaced it on the first run.
  - **The systemic fix: every run now reports fill rates.** Each stage measures
    the columns listed in `schemas.ALWAYS_POPULATED_COLUMNS` on the rows it is
    about to write and carries `fill_rates` + `underfilled_columns` in its
    `RUN_SUMMARY` block, logging a `WARNING` for anything below
    `schemas.MIN_FILL_RATE` (1.0). This would have caught both `web_link`
    columns on their very first run instead of months later. Costs one
    in-memory pass over rows already held; no extra BigQuery reads.
    - **Warns, never fails** — same posture as the orphaned-views check. A
      column that stops populating is a signal for a human, not a reason to
      abandon a sync that is otherwise writing good data.
    - The declared list is deliberately **conservative**: only structurally
      always-present columns. `category_name` (1,885/1,890), `activity`,
      `estimate_minutes`, `due_date` and `tasklist_name` are legitimately
      sparse and are excluded, because a warning that fires every run is one
      nobody reads. Tests assert both halves — that every declared column
      really exists in its schema (a typo would read 0.0 forever) and that the
      known-sparse columns stay out.
    - If a declared column turns out to have legitimate NULLs, **remove it
      from the list** as a documented decision rather than lowering
      `MIN_FILL_RATE`, which protects every other column too.
  - The fix is to **construct** the URL from `task_id` / `project_id` and
    `config.teamwork_base_url` rather than fetch it, which removes the
    dependency on Teamwork returning a link at all. It belongs in
    `transform.py` so it lands in the tables for every consumer — **not** in
    `views.py`, which would put the Teamwork subdomain in the repo when it is
    currently a GitHub secret (`TEAMWORK_BASE_URL`).
  - **Fixed for `tasks` (2026-09-15).** `transform.task_web_link()` builds
    `{base_url}/app/tasks/{task_id}`, a format confirmed against a real task
    in the live account rather than assumed. `base_url` is taken off
    `tw_client.base_url` in `sync_tasks()` rather than threaded down from
    `cfg`. It returns `None` when either half is missing: an empty column is
    recoverable, a column of links that look valid and 404 is not. Populates
    on the next full sync — `--create-views` alone will not fill it, because
    the value lives in the `tasks` table.
  - **Fixed for `projects` too (2026-09-15).** `transform.project_web_link()`
    builds `{base_url}/app/projects/{project_id}/tasks/list`, also confirmed
    against the live account. Note the path: it lands on the project's **task
    List page**, not the project overview at `/app/projects/{id}`, per the
    stated preference — that is where someone reviewing a project's work wants
    to arrive. It is a deliberate landing spot, not a longer-than-necessary
    URL; a test fails if it is shortened. The two helpers are intentionally
    separate rather than one parameterized builder, because the shapes differ
    and a shared one invites getting a suffix wrong for both at once.

- **PTO trips the long-entry rule, so its task is exempted (2026-09-14).**
  Staff log a full PTO day as 8 hours against a single task
  (`task_id 47878044`), and post it *before* taking the leave. Both halves
  collide with `v_exception_long_time_entries`, which flags anything over
  `LONG_ENTRY_THRESHOLD_HOURS` with no date bound: every PTO day was a
  standing false positive, and appeared ahead of the absence. The rule now
  excludes `LONG_ENTRY_EXEMPT_TASK_IDS`.
  - Scoped to the **task**, not the project category, because PTO is the only
    thing posting to that task. A category exemption would also have hidden
    genuine over-long entries elsewhere in the same category.
  - **The exclusion is `COALESCE(tl.task_id, -1) NOT IN UNNEST(...)`, and the
    COALESCE is load-bearing.** `timelogs.task_id` is NULLABLE (project-level
    time carries no task — 78 such rows as of this date), and in SQL
    `NULL NOT IN (...)` evaluates to NULL, not TRUE. A bare
    `tl.task_id NOT IN UNNEST(...)` would therefore have silently dropped
    every untasked entry over the threshold out of the report — the same NULL
    trap already recorded for `status` and `tasklist_name` below. Three tests
    pin this, including one asserting the bare form is *absent*.
  - Forward-dated time is expected on this account generally, and is **not**
    a data-entry error. It cannot reach `v_user_weekly_billable_hours`: both
    branches are bounded (`actual` is `week_start < current_week_start`, the
    other pins the current week). It *is* present in
    `v_user_daily_billable_hours_base`, which is deliberately unbounded — any
    new view reading from that base must bound dates itself if a future week
    would distort it.

- **Cron was changed away from its intended times and back (2026-09-04).**
  A round of review changed the schedule from `15 4,16 * * *` to
  `15 11,16 * * *` on the stated requirement "twice per day at 16:15 UTC
  and 11:15 UTC". That was wrong: the original intent was **midnight and
  noon EDT**, which *is* `15 4,16 * * *` (00:15 / 12:15 Eastern in summer).
  The cron in the repo had been correct all along; only the second slot
  (16:15 UTC = noon EDT) ever matched under the mistaken version. Now
  reverted.
  - **The tell was there and was noted but not pressed**: the change turned
    an even 12h/12h spacing into a lopsided 5h/19h, which was flagged at
    the time as "worth confirming you want". A twice-daily job going
    lopsided is a strong signal that the stated times are off — worth
    treating as a blocker rather than a footnote next time.
  - **Reading the cron in Eastern terms** is the reliable check, since the
    requirement is always expressed in local time while the cron is always
    UTC: `04:15Z = 00:15 EDT`, `16:15Z = 12:15 EDT`.
- **Two view predicates silently dropped NULL rows (fixed 2026-09-05).**
  In SQL a comparison against NULL yields NULL, not TRUE, and a `WHERE`
  clause keeps only rows that evaluate TRUE. So an unguarded predicate
  doesn't merely fail to match a NULL row — it removes it from the report
  entirely, which is backwards for an exception view whose whole job is
  surfacing incomplete data.
  - **`v_exception_missing_activty_no_time`**: `t.status != 'completed'`
    excluded every task whose `status` is NULL. An unknown status is
    precisely *not* a known-completed one, so those tasks should count
    toward a tasklist's cluster of three. Now
    `COALESCE(t.status, '') != 'completed'`.
  - **`v_exception_missing_estimate`** — the same class, found by auditing
    every predicate rather than fixing only the one reported. The exemption
    reads `NOT (category = 'Non-Monthly' AND tasklist_name IN (...))`.
    `NULL IN (...)` is NULL, so `TRUE AND NULL` is NULL and `NOT NULL` is
    NULL: a Non-Monthly task with **no tasklist name** was dropped rather
    than flagged for its missing estimate. An unnamed tasklist is not one
    of the three exempt ones, so it should be flagged. Now
    `COALESCE(t.tasklist_name, '') IN UNNEST(...)`.
  - **Impact depends on whether these columns are ever NULL in practice.**
    Both come straight from `raw.get(...)` on the tasks payload, so they
    are populated on normal rows; this is a correctness fix to the SQL, not
    a claim that any count will move. Check with:
    `SELECT COUNTIF(status IS NULL) AS null_status,
    COUNTIF(tasklist_name IS NULL) AS null_tasklist FROM tasks;`
  - **Guarded against recurrence**: `tests/test_views.py` now fails on *any*
    inequality against a string literal anywhere in the generated SQL that
    lacks a NULL guard, not just these two.
  - Requires `--create-views` to take effect.
- **Three known-gaps entries were deleted by a later doc edit (restored
  2026-09-05).** The alerting, retries, and full-replace-guard entries above
  vanished from this file between commits `e962e22` and `98e712f`. Cause: the
  schedule-revert edit rewrote a *span* of this section by index — replacing
  everything between two anchor strings — and those three entries happened to
  sit inside it. The code was never affected, only its record here. Recovered
  verbatim from `e962e22`. **Edit this file by replacing a specific known
  string, never a span between two anchors** — a span silently takes whatever
  drifted into it.
- **A failed scheduled run was silent (fixed 2026-09-04).** Nobody watches a
  cron run. A failure produced GitHub's default email to the workflow author
  and nothing else — easy to miss, and going to one person. Since the sync
  now *refuses* to write bad data rather than writing it, "loud" only helps
  if somebody hears it.
  - The workflow now opens a GitHub issue labelled `sync-failure` when a
    **scheduled** run fails, and closes it when a scheduled run next
    succeeds. Repeated failures comment on the open issue rather than
    opening a new one every 12 hours.
  - Manual `workflow_dispatch` runs are deliberately excluded — somebody
    clicked the button and is watching the result; an issue would be noise.
  - Needs `issues: write`, which is why the workflow's `permissions:` block
    is no longer `contents: read` alone. No new secrets: it uses the
    built-in `github.token`.
- **Retries only covered four status codes and no transport failures (fixed
  2026-09-04).** `_get()` retried 429/502/503/504 and nothing else, so a
  single dropped connection, read timeout, or body cut off mid-transfer —
  across the thousands of requests one run makes — killed the whole stage.
  - **Now retried**: `ConnectionError`, `Timeout`, `ChunkedEncodingError`,
    `ContentDecodingError`, and a JSON decode failure (a truncated body
    looks like `ValueError`, not an HTTP error). Plus HTTP 500 alongside the
    gateway family — this API has returned transient 500s, and a genuine
    server bug still surfaces after `MAX_RETRIES`.
  - **Still never retried**: 400, 401, 403, 404 and friends. No number of
    retries fixes a bad request, so those raise on the first response.
  - **Backoff** is now exponential (2s, 4s, 8s) rather than linear, honours
    a `Retry-After` header when the API sends one, and caps any single wait
    at `MAX_RETRY_SLEEP_SECONDS` (120) so one absurd `Retry-After` can't
    park the job. The old code also slept after its *final* attempt before
    giving up; it no longer does.
  - **Timeouts** are now `(connect, read) = (10, 60)`. There was no connect
    timeout at all, so a black-holed TCP connect could hang a stage until
    the workflow's `timeout-minutes` killed it.
- **A full-replace table could be silently emptied by a bad pull (fixed
  2026-09-04).** `projects`, `tasks` and `users` are truncate-and-reload, so
  `truncate_and_load()` wrote whatever the pull returned. If Teamwork
  returned `200` with zero items — an API blip, not a real result — the
  table was emptied (a `logger.warning`, nothing more), every view built on
  it went blank, and the stage still reported `"status": "success"` so the
  run exited 0 and the workflow went green. `WRITE_TRUNCATE` leaves no
  prior version to fall back to. Worse, an empty `projects` pull cascaded:
  the task scope came out empty, so `tasks` was emptied in the same run.
  - **Fix**: `truncate_and_load()` now refuses two cases outright —
    - **zero rows**, unconditionally (`EmptyLoadRefused`). There is no flag
      to override this; nothing legitimately empties these tables.
    - **a shrink past `MIN_REPLACE_ROWS_RATIO`** (0.5 — more than half the
      rows disappearing), via `SuspiciousShrinkRefused`. The prior count
      comes from table metadata (`num_rows`), so the check costs no query.
  - **The escape hatch**: a deliberate scope reduction — moving
    `ARCHIVED_PROJECT_TASKS_CUTOFF` forward, say — legitimately shrinks a
    table. Run `python sync.py --allow-shrink`, or tick **"Permit a
    full-replace table to shrink by more than half"** in the Actions
    workflow. That flag relaxes the shrink guard only; the zero-row refusal
    still stands.
  - **Same hazard on timelogs, handled differently**: an empty pull over a
    populated window would delete those rows and insert nothing.
    `replace_timelogs_window()` now refuses that, but *only* when the
    window currently holds rows — an empty window is legitimate when
    backfilling a month with no activity, so that case just logs and
    returns 0.
  - **Failure is loud, and partial by design**: the refusal raises, so that
    stage records `"status": "failed"` with the reason and the run exits 1.
    The table keeps its previous contents. Other stages still run, as they
    already did.
- **Retroactive timelogs against a closed month were never ingested
  (fixed 2026-09-04).** The normal run replaced only the *current*
  calendar month, so the moment the month rolled over in `SYNC_TIMEZONE`,
  the previous month froze exactly as it stood at its final run. Two
  things were lost, permanently, until someone ran `--backfill-months` by
  hand:
  1. Time logged on the last day of a month after that day's last sync —
     ~11h45m under either the old 04:15/16:15 UTC schedule or the
     corrected 11:15/16:15 UTC one (the last run covering September was
     Sep 30 12:15 ET in both cases; the next run was already October).
  2. **The bigger one**: any entry made or edited *retroactively* against
     a closed month. That is routine in timekeeping — writing up last week
     on a Monday, recoding a mistyped entry days later — and none of it
     ever reached BigQuery.
  - **Fix**: the normal run now replaces a rolling window of the current
    month plus `TIMELOG_SYNC_MONTHS_BACK` (default `1`) previous months,
    via `sync.timelog_window()`. On 2026-10-01 the window is
    `[2026-09-01, 2026-11-01)`, so all of September is re-pulled for the
    whole of October. `bigquery_sync.replace_current_month_timelogs()` was
    renamed `replace_timelogs_window()` to match — it was never
    month-specific, only ever called with one.
  - **Residual gap, by design**: a retroactive edit made more than
    `TIMELOG_SYNC_MONTHS_BACK` months after the fact still falls outside
    the window. Raise the constant to widen the margin; the cost is one
    extra paginated Teamwork range per month per run, and the rewrite
    stays a single atomic transaction regardless. If prior-period
    adjustments here routinely run months late, `2` or `3` is a
    reasonable setting.
  - **Not addressed by this**: `log_date` is the date portion of
    Teamwork's UTC `timeLogged` while the API filters by its own date
    interpretation, so a row at a window edge can still be inserted
    without being deleted, duplicating on each run. Widening the window
    shrinks the exposure (a row drifting across the old month boundary now
    lands *inside* the window and is replaced correctly) but does not
    remove it at the outer edges. Check with:
    `SELECT COUNT(*), COUNT(DISTINCT timelog_id) FROM timelogs;`
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
- **Scheduled-run delays — the `:15` mitigation was tried and disproven.**
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
  **Mitigation applied 2026-09-04**: moved the cron off `:00` to
  `15 4,16 * * *` — a cheap, documented best practice.

  **It did not work. Measured over the 19 consecutive firings from
  2026-09-05 to 2026-09-14:**

  | | before (`0 5,17`, 9 runs) | after (`15 4,16`, 19 runs) |
  |---|---|---|
  | median delay | 4h 46m | **4h 22m** |
  | minimum delay | 0h 10m | **1h 59m** |
  | maximum delay | 8h 16m | **9h 36m** |
  | fired within 30 min of schedule | 1 of 9 | **0 of 19** |
  | delayed more than 2 hours | 8 of 9 | **18 of 19** |

  The median barely moved and the *best* case got worse — under the old
  schedule at least one run was prompt; since the change, none have been.
  Moving off `:00` is therefore disproven for this repo; do not re-try it,
  and do not attempt to "tune" the cron to compensate, because the delay
  varies by 2-3.5 hours *within* each slot and any such tuning would encode
  a dependency on GitHub's current congestion pattern.

  The delay is also **slot-specific and systematic**, not random noise:

  | Slot | Actually fires (UTC) | In Eastern | Delay range |
  |---|---|---|---|
  | 04:15 | 08:37-09:48 | 04:37-05:48 ET | 4h 22m - 5h 34m |
  | 16:15 | 18:14-19:55 | 14:14-15:55 ET | 2h 00m - 3h 40m |

  So the **effective** schedule is roughly 05:00 and 15:00 ET, not midnight
  and noon, and real gaps run 9.3-15.2h rather than a steady 12h.

  **What this does and does not cost.** There is *no correctness impact*:
  the rolling two-month timelog window makes delay harmless at month
  boundaries, and task scope is computed at run time. All 19 runs succeeded,
  in 72-159s each, with `rows_dropped` all zero and `activity.method` still
  `bulk_sideload`. What it costs is freshness and predictability — worst
  case the data is ~15 hours stale, and a report opened at 1pm ET expecting
  a midday refresh is showing numbers from ~5am.

  **Next step: Cloud Scheduler + Cloud Run** (see "Scheduling" above). The
  evidence now justifies escalating rather than tuning further. A cheaper
  stopgap, if that is deferred, is simply adding more cron slots — four a
  day instead of two cuts worst-case staleness from ~15h to ~7h without
  fixing predictability at all.

  **Re-measured 2026-09-21 — the pattern holds, and has become *more*
  predictable without becoming faster.** 14 consecutive firings from
  2026-09-14 (pm slot) to 2026-09-21 (am slot), same `15 4,16` cron:

  | | Sep 5-14 (19 runs) | Sep 14-21 (14 runs) |
  |---|---|---|
  | median delay | 4h 22m | **4h 17m** |
  | minimum delay | 1h 59m | **2h 20m** |
  | maximum delay | 9h 36m | **5h 37m** |
  | fired within 30 min of schedule | 0 of 19 | **0 of 14** |
  | delayed more than 2 hours | 18 of 19 | **14 of 14** |

  The median did not move. What collapsed is the *spread*: the worst case
  improved by four hours and every firing now lands in a roughly one-hour
  band per slot.

  | Slot | Actually fires (UTC) | In Eastern | Delay range |
  |---|---|---|---|
  | 04:15 | 08:46-09:53 | 04:46-05:53 ET | 4h 31m - 5h 37m |
  | 16:15 | 18:35-20:18 | 14:35-16:18 ET | 2h 20m - 4h 02m |

  Against the Sep-14 table the am slot is unchanged and the pm slot has
  drifted roughly 20 minutes later. Gaps between consecutive fires ran
  9.5-15.1h (median 13.1h), so worst-case staleness is still ~15 hours —
  the number that actually matters to a reader of the dashboards, and it
  has not improved at all.

  **Do not read the narrowing as the problem fixing itself.** A tight
  distribution around a 4h 17m median is still a 4h 17m median; it means
  the delay is a stable property of these two UTC slots rather than noise,
  which is evidence *for* migrating off GitHub's scheduler, not against.
  It is also still not a reason to re-tune the cron — the band is ~1 hour
  wide per slot and would encode a dependency on GitHub's current
  congestion pattern, which is exactly what the `:15` attempt above proved
  is not stable over time.

  Pipeline health over the same 14 runs was clean: **14 of 14 succeeded**
  (49 of 49 scheduled runs all-time), 96-168s wall clock, `rows_dropped`
  all zero, `activity.method` still `bulk_sideload`, every `fill_rates`
  entry at 1.0 with `underfilled_columns` empty, and no `sync-failure`
  issue has ever been opened. Row counts moved as expected over the week —
  tasks 37,891 -> 38,090, timelogs (Aug+Sep window) 6,437 -> 7,224 — while
  `projects` held at exactly 1,890 rows and an 824-project task scope
  (219 active + 605 archived-on-or-after-cutoff) across all 14 runs. That
  stasis is plausible for a week on this account, but it is the one number
  here that would look identical if the projects pull ever went stale, so
  confirm it moves before treating a constant `projects` count as healthy.

  **Cron moved 45 minutes earlier (2026-09-21), `15 4,16` -> `30 3,15`.**
  Requested by the owner as a cheap experiment while the Cloud Run migration
  waits on bandwidth. Recorded here with its limits stated up front, because
  the next reader will want to know whether it worked and the honest answer
  is that this design cannot cleanly tell them.

  What it changes: the nominal slots, now 03:30 and 15:30 UTC — 23:30 and
  11:30 EDT, becoming 22:30 and 10:30 EST once the clocks change. The 10:30
  target was stated in local time and is an **EST** reading; reading the cron
  in Eastern terms is the check this file already prescribes after the
  2026-09-04 incident, and it shows the target is met in winter, not now.
  Spacing stays an even 12h/12h, so the lopsided-schedule tell from that
  incident is clean.

  What it does **not** change: the delay mechanism. Nothing fires on time,
  and the nominal cron has never been when this job runs. The move re-rolls
  onto two different UTC slots whose congestion is unmeasured — the outcome
  could be earlier, later or unchanged, and it is not 45 minutes either way.

  **How to evaluate it, and why care is needed.** Within-slot variation on
  the old schedule was 1h 12m (am) to 2h 02m (pm) across the last 14
  firings, and up to 3h 40m over the full 33. A 45-minute nominal shift is
  *inside that noise*. Comparing a handful of new firings against the old
  medians will produce a number that looks like a result and is not one.
  Give it at least two weeks (~28 firings), compare medians and per-slot
  ranges rather than individual runs, and treat anything under about an hour
  of median movement as indistinguishable from noise. `created_at` on each
  `event=schedule` run via the Actions API is the precise source; the UI's
  rounded display is not.

  **This is not a reversal of "do not tune the cron to compensate".** That
  rule stands: it is about chasing the delay, and a 45-minute move cannot
  chase a 2-5.5 hour one. If the next measurement shows improvement, the
  most likely explanation is still that these two slots happen to be less
  congested, which is a property of GitHub's load and not something this
  repo controls or can rely on.

- **Retired views — all now dropped, and orphans are self-reporting
  (closed 2026-09-13).** `--create-views` never drops a view removed from
  `VIEW_NAMES`, so a retired view stays live in BigQuery *frozen at its
  last-written SQL while still querying the live tables* — returning
  fresh-looking numbers from obsolete logic, with nothing announcing it.

  | Retired view | Replaced by | Status |
  |---|---|---|
  | `v_user_daily_billable_hours_long` | `v_user_weekly_billable_hours` | dropped |
  | `v_user_daily_billable_hours_wide` | `v_user_weekly_billable_hours` | dropped (after its "Team Hours" Combo chart was rebuilt) |
  | `v_user_daily_billable_hours_trend_long` | `v_user_weekly_billable_hours` | dropped |
  | `v_user_daily_billable_hours_trend_wide` | `v_user_weekly_billable_hours` | dropped |
  | `v_user_current_week_hybrid` | `v_user_weekly_billable_hours` | dropped |
  | **`v_user_daily_billable_hours_trend`** | `v_user_weekly_billable_hours` | dropped 2026-09-13 |
  | `v_exception_missing_activity` | `..._with_time` + `..._activty_no_time` | dropped 2026-09-13 |

  - **How the last two survived**: this list originally named the five
    `_long`/`_wide`/`hybrid` views but **not**
    `v_user_daily_billable_hours_trend` — the original trend view that
    `_trend_long`/`_trend_wide` had replaced one commit earlier (`fc55e9f`).
    It was orphaned before the retirement note was written, so it never made
    the checklist and outlived the cleanup that removed its own successors.
    It was eventually spotted by eye in a BigQuery console screenshot,
    months later. `v_exception_missing_activity` was on the list but had
    simply not been actioned.
  - **Why that mattered**: the stale trend view still carried
    `DATE_TRUNC(..., WEEK(MONDAY))` — a Monday-start business week, against
    the Sunday-start convention every current view uses — and a bare
    `CURRENT_DATE()`, the UTC bug fixed everywhere else on 2026-09-04.
    Anything charting from it disagreed with `v_user_weekly_billable_hours`
    about which week a Sunday belonged to.
  - **Now automated**: `views.list_orphan_views()` queries
    `INFORMATION_SCHEMA.VIEWS` and every `--create-views` run reports any
    view present in the dataset but absent from `VIEW_NAMES` — logged, and
    carried in `RUN_SUMMARY` as `orphaned_views`. Only VIEWS are listed, so
    the native tables and the externally-managed Google Sheet table never
    appear. It returns `null` rather than an empty list if the check itself
    fails, since a false "clean" would be worse than an admitted failure.
  - **Orphans never fail the run.** Dropping a view is a decision that needs
    a look at Looker Studio first: retiring one still means removing it from
    `VIEW_NAMES`, repointing any report that uses it, and then running
    `DROP VIEW` by hand.
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
- **Per-task Activity fallback is unbounded — reviewed 2026-09-05 and
  deliberately left as is.** If `included.customfieldTasks` is ever absent
  from *every* page of *every* project batch, `enrich_tasks_with_activity()`
  falls back to one API call per task. At the current 37,226 tasks and
  `CUSTOM_FIELD_FETCH_WORKERS = 8`, that is roughly **12-23 minutes** at the
  ~200ms per-request latency seen in real runs — and potentially far longer
  if Teamwork rate-limits it, which it has done to this pipeline before at
  much lower volumes. Enrichment runs *before* the tasks table is written,
  so a fallback that overruns `timeout-minutes: 60` kills the job by
  SIGKILL (not catchable, so the `try/except` around enrichment does not
  help) and leaves tasks, users and timelogs unwritten for that run.
  - **Why it was left**: `activity` is an explicitly nullable, non-fatal
    enrichment — a failure just leaves the column NULL and everything else
    loads. Spending 20+ minutes and risking a whole sync to populate a
    nullable column is a bad trade, but the blast radius is now bounded by
    `timeout-minutes: 60` and made visible by the failure-alerting issue,
    neither of which existed when this was first raised. The trigger has
    also never actually occurred here.
  - **What would justify revisiting**, all visible in `RUN_SUMMARY`:
    `activity.method` reading `per_task_fallback` rather than
    `bulk_sideload`; `activity.tasks_resolved` dropping materially from the
    14,944 measured on 2026-09-04; or a sync taking much longer than its
    usual ~2 minutes.
  - **The quieter sibling worth watching**: `included` is merged across all
    batches, so if *some* batches carry `customfieldTasks` and others do
    not, the key is present, the bulk path is taken, and the result is
    silently **partial** — no fallback, no warning, just a lower
    `tasks_resolved`. That number is the only signal, which is why it is
    listed above.
  - **The failure this guards against is not primarily an API change.** The
    strongest precedent is this pipeline's own history: `projects.json`'s
    `included["projectCategories"]` sideload was confirmed present when
    sampled through other tooling and confirmed empty on every page of
    every real production run of this script, with the same params, and the
    cause was never established (see `teamwork_client.py`). A different
    sideload key on the same API behaving the same way cannot be ruled out.
  - **The fix, if it is ever needed**: cap the fallback by task count —
    above the cap, log an error and leave `activity` NULL rather than
    spending the run on it. Below it, the fallback still works, which is
    the small-account case it was actually useful for.
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

## Tests

```
pip install -r requirements-dev.txt
python -m pytest tests/
```

406 tests, ~0.7s, entirely offline — no Teamwork API, no BigQuery, no
credentials, no network. CI runs them on every push
(`.github/workflows/tests.yml`).

Every case corresponds to a bug this pipeline actually hit, so the suite
doubles as an executable version of "Known gaps" below:

| File | Covers |
|---|---|
| `test_transform.py` | Row mapping: the Go zero-time `archivedAt` sentinel, the `projectId` fallback chain, `is_billable` present-but-null, cents-to-dollars, budget selection, the Activity custom-field sideload |
| `test_sync_scope_and_windows.py` | Task scope selection and the rolling timelogs window, including year boundaries and the month-tail gap |
| `test_teamwork_client.py` | Retry matrix, `Retry-After`, the `page`/`pageSize` params behind the original rate-limiting incident, `projectIds` batching |
| `test_bigquery_guards.py` | Refusing an empty load or a suspicious shrink; the `--allow-shrink` override |
| `test_views.py` | Rendered SQL: no bare `CURRENT_DATE()`, rule constants reaching the SQL, view creation order, `VIEW_NAMES` drift |

**The suite was itself verified** by re-introducing nine bugs fixed on
2026-09-04 — UTC `CURRENT_DATE()`, the missing connect timeout, un-retried
500s, the `page[size]` pagination bug, the current-month-only window, the
`archived_at` string comparison, the `is_billable` fallback, the dropped
`projectId` chain, and the unguarded empty load — and confirming each one
fails it. A suite that passes against known-broken code is worse than none.

What it deliberately does **not** cover: anything requiring the live
Teamwork API or a real BigQuery client. Those are still verified by
`--dry-run`, `--explain-task-scope`, and reading `RUN_SUMMARY`.

## Files

- `sync.py` — entrypoint (`--dry-run`, `--explain-task-scope`,
  `--backfill-months`, `--create-views`, or a full sync)
- `config.py` — env var loading
- `teamwork_client.py` — Teamwork REST API client (auth, pagination, retries)
- `schemas.py` — BigQuery table schemas
- `transform.py` — raw Teamwork JSON → BigQuery row mapping
- `bigquery_sync.py` — dataset/table creation, truncate+load, the
  transactional windowed replace for timelogs
- `views.py` — the six exception/QC reporting views plus `v_usermins`,
  `v_user_daily_billable_hours_base`, and `v_user_weekly_billable_hours`
  (see "Exception reporting views" above)
