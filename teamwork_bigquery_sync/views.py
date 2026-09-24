"""BigQuery views for the exception / quality-control reporting layer.

Six views for the five rules agreed on for identifying where staff isn't
using Teamwork the way it's meant to be used (the missing-activity rule is
split across two views — see below). Each view is meant to be the direct
data source for a Looker Studio report, filterable by user / project /
client / tasklist — the columns needed for those filters are included in
every view's output, not just the flagged field itself.

IMPORTANT — assumptions baked into these views that were not 100% spelled
out in the original rules and should be verified against real data before
trusting the numbers (see README "Exception reporting views" for the full
writeup):
  - Rules 1, 2, and 5 are scoped to MONITORED_CATEGORIES only (the three
    project categories confirmed as "all of the billable projects we are
    monitoring"). Un-categorized / other-category projects are excluded
    from those three rules entirely.
  - Rule 3 ("internal projects") is scoped to INTERNAL_CATEGORIES, an
    explicit list confirmed against real category_name values in
    production (not the inverse of MONITORED_CATEGORIES). This is a
    deliberate narrowing: categories that are neither monitored nor
    internal (Books, Advisory, Tax/Compliance, Onboarding, Legacy
    Projects, and uncategorized projects — collectively far larger than
    the monitored + internal sets combined) are excluded from rule 3
    entirely, per your explicit confirmation.
  - Rule 4 (long time entries) is intentionally NOT scoped to monitored
    categories — it checks every timelog site-wide, since excess time on an
    internal task is arguably just as worth a look as on a billable one.

Update the constants below (not the SQL) if any of these lists change.
"""

import logging

logger = logging.getLogger(__name__)

# The project categories that make up "all of the billable projects we are
# monitoring" per your instruction. Rules 1, 2, and 5 are scoped to these.
#
# "Non-Monthly & Payroll" was originally treated as one category here, but
# turned out to be two distinct real Teamwork categories ("Non-Monthly" and
# "Payroll") — confirmed via your direct edit of the missing_estimate view
# in BigQuery. The combined string never matched any real project, so
# rules 1, 2, and 5 were silently skipping both categories entirely until
# this was split out.
MONITORED_CATEGORIES = ["Books Maintenance", "Monthly Close", "Non-Monthly", "Payroll"]

# Categories that count as "internal" for rule 3 (billable time posted to
# an internal project). An explicit list, not the inverse of
# MONITORED_CATEGORIES — verified against real category_name values in
# production (SELECT category_name, COUNT(*) FROM projects GROUP BY 1).
# You confirmed this should be a strict whitelist: categories that are
# neither monitored nor internal (Books [1056 projects — the single
# largest category in the account], Advisory [49], Tax/Compliance [253],
# Onboarding [27], Legacy Projects [4], and 5 uncategorized projects) are
# deliberately excluded from rule 3 by this choice, not an oversight.
INTERNAL_CATEGORIES = ["FFI Internal Projects", "Functional", "Individual"]

# Tasklists exempt from the "must have an estimate" rule, but only within
# projects categorized "Non-Monthly" — per your instruction ("non-monthly
# projects"), not "Payroll".
ESTIMATE_EXEMPT_TASKLISTS = ["Client Management", "Client Management v2", "HR Advisory"]
ESTIMATE_EXEMPT_CATEGORY = "Non-Monthly"

# Category whose tasks must all be recurring (have a sequence_id), except
# sub-tasks — those inherit recurrence from their parent and don't carry
# their own sequence_id, per your data observation.
RECURRING_REQUIRED_CATEGORY = "Books Maintenance"

LONG_ENTRY_THRESHOLD_HOURS = 2

# The single Teamwork task every PTO entry on this account posts against
# (confirmed 2026-09-18). One constant for both places that treat PTO
# specially: the long-entry exemption below, and v_user_daily_time_split's
# pto_hours column.
PTO_TASK_ID = 47878044

# Timelog task_ids the long-entry rule never flags, however many hours they
# carry. 47878044 is the single task every PTO entry on this account posts
# against: a full PTO day is logged as 8 hours, so it clears the 2-hour
# threshold by definition, and staff pre-post PTO before taking the leave, so
# it clears it for dates that haven't happened yet. Neither is the data-entry
# problem this rule looks for.
#
# Scoped to the task rather than to a project category deliberately: PTO is the
# only thing posting to this task, so exempting it silences exactly the false
# positives and nothing else. A category exemption would also hide genuine
# over-long entries sitting elsewhere in the same category.
LONG_ENTRY_EXEMPT_TASK_IDS = [PTO_TASK_ID]

# The business timezone the user-hours report's "has today happened yet?"
# logic is evaluated in.
#
# BigQuery's CURRENT_DATE() takes no timezone and therefore returns the UTC
# date. Eastern is UTC-4/-5, so from 20:00 ET onward BigQuery already
# believes it is tomorrow, and v_user_weekly_billable_hours misclassifies
# every evening: Mon-Thu buckets flip from 'minimum' to 'actual' a day
# early, Thursday evening turns Friday into a 'plug' before Friday starts,
# and — worst — from 20:00 ET Saturday DATE_TRUNC(CURRENT_DATE(), WEEK)
# rolls to the NEXT Sunday, so the week just completed drops out of the
# report and an empty, fully-projected week takes its place. Simulated
# hour by hour across a full week: 28 of 168 hours (17%) were wrong.
#
# This is the same business day SYNC_TIMEZONE anchors the timelogs window
# to; they are kept as separate constants because a view bakes its SQL in
# at --create-views time while SYNC_TIMEZONE is read per sync run. If the
# team's anchor timezone ever changes, change both and re-run
# --create-views.
#
# NOTE: this fixes when a day is *considered elapsed*. It does not change
# how timelogs are bucketed — timelogs.log_date is the date portion of
# Teamwork's UTC `timeLogged`, so entries near midnight can still land on
# the adjacent day. See README "Known gaps".
REPORTING_TIMEZONE = "America/New_York"

# External table backed by a Google Sheet (named range "mins4bq" in "FFI
# Compensation Database and Budget"), created manually via the BigQuery
# console — NOT by this pipeline, and NOT managed by ensure_all_tables().
# It's queried live off the Sheet at query time. Created under your own
# Google identity, so only readers who themselves have Drive access to
# that sheet (or who query through something running as you, like Looker
# Studio's "owner's credentials" mode) can actually read it — our GitHub
# Actions service account was deliberately NOT given access, since it has
# no need to touch this table.
#
# Columns (per your confirmation): tw_userid (INT64, join key -> users.
# user_id), first, last, email, as_of, min_bill, min_value.
#
# BOTH min_bill AND min_value ARE HOURS, NOT MONEY, despite the name
# "value" (confirmed 2026-09-22):
#   min_bill  -- minimum weekly BILLABLE hours per employee.
#   min_value -- minimum weekly VALUE-ADDED hours per employee. Always
#                non-billable, so it has no bearing on revenue.
# For everyone on the sheet the two sum to roughly 30-35 hours; partners
# invert the split (a low billable minimum, a high value-added one). An
# earlier version of this comment called them "a per-person minimum billing
# figure", and that reading of min_value as a dollar minimum was nearly
# used to project revenue -- it would have put most people at about $0.20
# of projected revenue a day. The derived columns daily_min_value /
# wkly_min_value keep their names so existing reports do not break; read
# them as hours.
#
# Still worth restricting read access if this is ever shared beyond its
# current audience: per-person targets sit alongside user_cost/user_rate.
ANCILLARY_USER_INFO_TABLE = "gs_minimum_user_info"

# How old the data must be before v_data_freshness flags it as stale.
#
# Deliberately NOT 12h, which is what the twice-daily cron implies. GitHub's
# scheduler delivers those two firings 9.5-15.1h apart in practice (measured
# over 33 consecutive firings -- see README "Known gaps"), so a 12h or even
# 16h threshold would read "stale" during entirely normal operation and train
# everyone to ignore the indicator. 18h clears the measured worst case with
# headroom while still catching a genuinely missed sync, which lands at 24h+.
#
# Revisit this if the Cloud Run migration lands: real scheduling guarantees
# would make a much tighter threshold meaningful.
DATA_FRESHNESS_STALE_AFTER_HOURS = 18

# The earliest month v_client_month will emit a row for.
#
# `timelogs` only holds history the pipeline has loaded, which begins
# 2026-01-01. A budgeted month earlier than that would carry a full month's
# budget against artificially zero revenue and drag every percentage down, so
# the month spine is clamped here even when a project started earlier.
#
# Raise this only after backfilling the corresponding months with
# --backfill-months; lower it only if earlier history is actually loaded.
CLIENT_MONTH_HISTORY_FLOOR = "2026-01-01"

# The Teamwork company id of the one client whose time v_user_daily_time_split
# counts as INTERNAL: Forward Financial Intelligence, Inc. (confirmed
# 2026-09-24). Every other client is external work, split into billable and CNB
# (client non-billable) by the time entry's own billable flag.
#
# Matched on projects.company_id, NOT on the client name: the id survives a
# rename in Teamwork, where a name match would silently move every internal
# hour to CNB. Deliberately a client test, not a category test:
# INTERNAL_CATEGORIES above answers a different question (which project
# categories should never carry billable time).
INTERNAL_CLIENT_COMPANY_ID = 1380118

VIEW_NAMES = [
    "v_exception_missing_activity_with_time",
    "v_exception_missing_activty_no_time",
    "v_exception_missing_estimate",
    "v_exception_billable_time_internal_projects",
    "v_exception_long_time_entries",
    "v_exception_recurring_compliance",
    "v_usermins",
    "v_user_daily_billable_hours_base",
    "v_user_weekly_billable_hours",
    "v_timelog_detail",
    "v_exception_time_without_task",
    "v_task_review",
    "v_project_detail",
    "v_client_month",
    "v_user_daily_time_split",
    "v_data_freshness",
]


def _sql_string_array(values):
    return "[" + ", ".join("'" + v.replace("'", "\\'") + "'" for v in values) + "]"


def _sql_int_array(values):
    return "[" + ", ".join(str(int(v)) for v in values) + "]"


def build_view_sql(project_id, dataset):
    """Returns {view_name: CREATE OR REPLACE VIEW sql} for every view in VIEW_NAMES."""

    def fqn(table):
        return f"`{project_id}.{dataset}.{table}`"

    projects = fqn("projects")
    tasks = fqn("tasks")
    timelogs = fqn("timelogs")
    users = fqn("users")

    monitored = _sql_string_array(MONITORED_CATEGORIES)
    internal = _sql_string_array(INTERNAL_CATEGORIES)
    exempt_tasklists = _sql_string_array(ESTIMATE_EXEMPT_TASKLISTS)

    # Excluded by task_id, NULL-safely. `tl.task_id NOT IN UNNEST(...)` would
    # be wrong here: timelogs.task_id is NULLABLE (project-level time carries
    # no task), and in SQL `NULL NOT IN (...)` evaluates to NULL, not TRUE, so
    # a bare NOT IN would silently drop every no-task entry over the threshold
    # from this report. COALESCE to a sentinel no real task_id can take keeps
    # those rows in. Built conditionally because `UNNEST([])` has no inferable
    # element type in BigQuery and fails to compile.
    if LONG_ENTRY_EXEMPT_TASK_IDS:
        long_entry_exempt_tasks = (
            "\n  AND COALESCE(tl.task_id, -1) NOT IN UNNEST("
            + _sql_int_array(LONG_ENTRY_EXEMPT_TASK_IDS)
            + ")"
        )
    else:
        long_entry_exempt_tasks = ""

    # A task can have multiple assignees (assignee_user_ids is repeated).
    # Rather than fan out one row per assignee via UNNEST + JOIN (which
    # duplicates every other column on the task and breaks "one row per
    # task" for these views), this resolves all of a task's assignees to
    # names and concatenates them into a single string, e.g. "Bob, Jane,
    # Mary" — via a correlated subquery so the outer query stays one row
    # per task. Trade-off: this column is no longer a clean exact-match
    # filter dimension in Looker Studio — filtering by one assignee there
    # needs a "Text contains" filter rather than an exact-value dropdown.
    assignee_names = f"""(
    SELECT STRING_AGG(u.full_name, ', ' ORDER BY u.full_name)
    FROM UNNEST(t.assignee_user_ids) AS assignee_id
    JOIN {users} u ON u.user_id = assignee_id
  ) AS assignee_names"""

    # The project's owner (projects.owner_id), resolved to a name. Relevant
    # wherever project context is shown, so this is added to all five views.
    proj_owner_join = f"LEFT JOIN {users} owner ON owner.user_id = p.owner_id"
    proj_owner_col = "owner.full_name AS proj_owner"

    # Whether ANY time (more than 0 minutes) has ever been logged against
    # this task. Only meaningful on the task-based views — the timelog-based
    # views (billable_time_internal_projects, long_time_entries) are already
    # individual time entries, so "was time posted" is trivially true there.
    has_time_logged_col = f"""EXISTS(
    SELECT 1 FROM {timelogs} tl
    WHERE tl.task_id = t.task_id AND tl.minutes > 0
  ) AS has_time_logged"""

    views = {}

    # Missing-activity was originally one view; split per your instruction
    # into a task-level view for the cases where time HAS been posted (the
    # more urgent case — billable work happening with no Activity value)
    # and a tasklist-level rollup for the no-time-posted cases, which only
    # surfaces when they cluster (3+ in one tasklist) rather than flagging
    # every individual task.
    has_no_activity_time_col = f"""EXISTS(
    SELECT 1 FROM {timelogs} tl
    WHERE tl.task_id = t.task_id AND tl.minutes > 0
  )"""

    views["v_exception_missing_activity_with_time"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_missing_activity_with_time")} AS
SELECT
  p.project_id,
  p.name AS project_name,
  p.category_name,
  p.client_name,
  {proj_owner_col},
  t.tasklist_id,
  t.tasklist_name,
  t.task_id,
  t.name AS task_name,
  t.status,
  {assignee_names},
  t.due_date,
  t.start_date,
  t.created_at,
  t.updated_at,
  t.synced_at
FROM {tasks} t
JOIN {projects} p ON p.project_id = t.project_id
{proj_owner_join}
WHERE t.activity IS NULL
  AND p.category_name IN UNNEST({monitored})
  AND {has_no_activity_time_col}
"""

    # Refinement per your instruction: a completed task missing "Activity"
    # is done and isn't going to get one — only count still-open tasks
    # toward the tasklist's cluster.
    #
    # COALESCE, not a bare `t.status != 'completed'`: in SQL, NULL != 'x'
    # evaluates to NULL rather than TRUE, and a WHERE clause keeps only
    # rows that are TRUE. So a task whose status is NULL was being dropped
    # from this view entirely — the opposite of the intent, since an
    # unknown status is precisely not a known-completed one. Same reasoning
    # as the tasklist exemption in missing_estimate below.
    views["v_exception_missing_activty_no_time"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_missing_activty_no_time")} AS
SELECT
  p.project_id,
  p.name AS project_name,
  p.category_name,
  p.client_name,
  {proj_owner_col},
  t.tasklist_id,
  t.tasklist_name,
  COUNT(*) AS missing_activity_no_time_task_count
FROM {tasks} t
JOIN {projects} p ON p.project_id = t.project_id
{proj_owner_join}
WHERE t.activity IS NULL
  AND p.category_name IN UNNEST({monitored})
  AND COALESCE(t.status, '') != 'completed'
  AND NOT {has_no_activity_time_col}
GROUP BY p.project_id, p.name, p.category_name, p.client_name, proj_owner, t.tasklist_id, t.tasklist_name
HAVING COUNT(*) >= 3
"""

    # has_parent_task: TRUE for a sub-task, FALSE for a top-level task.
    # Same test v_exception_recurring_compliance already uses to mean
    # "top-level" (`t.parent_task_id IS NULL`), just inverted — so the two
    # rules agree on what a parent is. Deliberately NOT derived from
    # sequence_id, which is the recurring-series identifier and unrelated:
    # sub-tasks inherit recurrence from their parent and carry no
    # sequence_id of their own, so the two are close to mutually exclusive.
    #
    # COALESCE on tasklist_name for the same reason as the status filter
    # above: `NULL IN (...)` is NULL, so `TRUE AND NULL` is NULL, and
    # `NOT NULL` is NULL — meaning a Non-Monthly task with no tasklist name
    # was silently dropped from this view instead of being flagged. An
    # unnamed tasklist is not one of the three exempt ones, so it should be
    # flagged.
    views["v_exception_missing_estimate"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_missing_estimate")} AS
SELECT
  p.project_id,
  p.name AS project_name,
  p.category_name,
  p.client_name,
  {proj_owner_col},
  t.tasklist_id,
  t.tasklist_name,
  t.task_id,
  t.name AS task_name,
  t.status,
  t.estimate_minutes,
  (t.parent_task_id IS NOT NULL) AS has_parent_task,
  {assignee_names},
  {has_time_logged_col},
  t.due_date,
  t.start_date,
  t.created_at,
  t.updated_at,
  t.synced_at
FROM {tasks} t
JOIN {projects} p ON p.project_id = t.project_id
{proj_owner_join}
WHERE (t.estimate_minutes IS NULL OR t.estimate_minutes = 0)
  AND p.category_name IN UNNEST({monitored})
  AND NOT (
    p.category_name = '{ESTIMATE_EXEMPT_CATEGORY}'
    AND COALESCE(t.tasklist_name, '') IN UNNEST({exempt_tasklists})
  )
"""

    views["v_exception_billable_time_internal_projects"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_billable_time_internal_projects")} AS
SELECT
  p.project_id,
  p.name AS project_name,
  p.category_name,
  p.client_name,
  {proj_owner_col},
  tk.tasklist_id,
  tk.tasklist_name,
  tl.task_id,
  tk.name AS task_name,
  tl.timelog_id,
  tl.user_id,
  u.full_name AS user_name,
  u.email AS user_email,
  tl.log_date,
  tl.hours,
  tl.minutes,
  tl.is_billable,
  tl.description,
  tl.synced_at
FROM {timelogs} tl
LEFT JOIN {projects} p ON p.project_id = tl.project_id
LEFT JOIN {tasks} tk ON tk.task_id = tl.task_id
LEFT JOIN {users} u ON u.user_id = tl.user_id
{proj_owner_join}
WHERE tl.is_billable = TRUE
  AND tl.minutes > 0
  AND p.category_name IN UNNEST({internal})
"""

    views["v_exception_long_time_entries"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_long_time_entries")} AS
SELECT
  p.project_id,
  p.name AS project_name,
  p.category_name,
  p.client_name,
  {proj_owner_col},
  tk.tasklist_id,
  tk.tasklist_name,
  tl.task_id,
  tk.name AS task_name,
  tl.timelog_id,
  tl.user_id,
  u.full_name AS user_name,
  u.email AS user_email,
  tl.log_date,
  tl.hours,
  tl.minutes,
  tl.is_billable,
  tl.description,
  tl.synced_at
FROM {timelogs} tl
LEFT JOIN {projects} p ON p.project_id = tl.project_id
LEFT JOIN {tasks} tk ON tk.task_id = tl.task_id
LEFT JOIN {users} u ON u.user_id = tl.user_id
{proj_owner_join}
WHERE tl.hours > {LONG_ENTRY_THRESHOLD_HOURS}{long_entry_exempt_tasks}
"""

    views["v_exception_recurring_compliance"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_recurring_compliance")} AS
SELECT
  p.project_id,
  p.name AS project_name,
  p.category_name,
  p.client_name,
  {proj_owner_col},
  t.tasklist_id,
  t.tasklist_name,
  t.task_id,
  t.name AS task_name,
  t.status,
  t.sequence_id,
  t.parent_task_id,
  {assignee_names},
  {has_time_logged_col},
  t.due_date,
  t.start_date,
  t.created_at,
  t.updated_at,
  t.synced_at
FROM {tasks} t
JOIN {projects} p ON p.project_id = t.project_id
{proj_owner_join}
WHERE p.category_name = '{RECURRING_REQUIRED_CATEGORY}'
  AND t.parent_task_id IS NULL
  AND t.sequence_id IS NULL
"""

    # Not an exception rule — a reference view joining Teamwork users to
    # the ancillary minimum-billing data from the "mins4bq" Google Sheet
    # range (ANCILLARY_USER_INFO_TABLE, above). INNER JOIN is intentional
    # (per your SQL): only users present in the sheet show up here, not
    # every Teamwork user. min_bill/min_value are weekly HOURS in the sheet
    # (billable and value-added respectively -- see ANCILLARY_USER_INFO_TABLE
    # above); daily versions are derived (/5) alongside them.
    views["v_usermins"] = f"""
CREATE OR REPLACE VIEW {fqn("v_usermins")} AS
SELECT
  u.user_id,
  u.email,
  u.first_name,
  u.last_name,
  u.user_type,
  u.user_rate,
  u.user_cost,
  m.min_bill AS wkly_min_bill,
  (m.min_bill / 5) AS daily_min_bill,
  m.min_value AS wkly_min_value,
  (m.min_value / 5) AS daily_min_value,
  m.as_of
FROM {users} u
JOIN {fqn(ANCILLARY_USER_INFO_TABLE)} m ON u.user_id = m.tw_userid
-- Former staff stay out of the minimums even if their row is still on the
-- sheet. Until 2026-09-24 `users` held no deleted people at all, so this join
-- dropped them implicitly; now that they are loaded (so their names resolve on
-- historical time), the exclusion has to be explicit or they would reappear
-- in v_user_weekly_billable_hours with a target and a projected plug.
-- IS NOT TRUE, not = FALSE, so a NULL flag keeps the person rather than
-- silently dropping a current employee.
WHERE u.is_deleted IS NOT TRUE
"""

    # Layer 1 — the shared aggregation base. One row per (user, day_bucket,
    # week_start): real, historical, aggregated SUM(hours) from billable
    # timelogs, with the weekday-bucketing rule applied exactly once, here
    # -- Sunday's hours fold into Monday, Saturday's into Friday, both
    # within the SAME week. The week itself runs Sunday-through-Saturday
    # (BigQuery's default WEEK truncation, i.e. WEEK(SUNDAY)) per your
    # instruction: "week of" is always a Sunday, and the business week
    # logically ends on Saturday. This also makes the fold direction
    # unambiguous in a way the old Monday-Sunday week wasn't: Sunday (day
    # 1 of its week) and Monday (day 2) are adjacent and both near the
    # start; Friday (day 6) and Saturday (day 7) are adjacent and both
    # near the end -- no more "hasn't happened yet" ambiguity about which
    # week a Sunday belongs to.
    #
    # Deliberately sparse: no user scaffold, no daily_min_bill, no
    # zero-filling for missing combinations -- just the raw aggregation.
    # Every downstream consumer applies its own scaffolding, since
    # "which weeks/users need a guaranteed row" differs per report.
    # Unbounded (all history) since this is a view, not a materialized
    # table -- cost is incurred at query time, and the one current
    # consumer (v_user_weekly_billable_hours) filters to whatever range
    # it needs anyway.
    views["v_user_daily_billable_hours_base"] = f"""
CREATE OR REPLACE VIEW {fqn("v_user_daily_billable_hours_base")} AS
SELECT
  tl.user_id,
  CASE EXTRACT(DAYOFWEEK FROM tl.log_date)
    WHEN 1 THEN 'Monday'    -- Sunday folds into Monday (same Sun-Sat week)
    WHEN 2 THEN 'Monday'
    WHEN 3 THEN 'Tuesday'
    WHEN 4 THEN 'Wednesday'
    WHEN 5 THEN 'Thursday'
    WHEN 6 THEN 'Friday'
    WHEN 7 THEN 'Friday'    -- Saturday folds into Friday (same Sun-Sat week)
  END AS day_bucket,
  CASE EXTRACT(DAYOFWEEK FROM tl.log_date)
    WHEN 1 THEN 1 WHEN 2 THEN 1 WHEN 3 THEN 2 WHEN 4 THEN 3
    WHEN 5 THEN 4 WHEN 6 THEN 5 WHEN 7 THEN 5
  END AS day_order,
  DATE_TRUNC(tl.log_date, WEEK) AS week_start,
  -- Text form of week_start, for charts. A DATE dimension makes Looker
  -- Studio plot a DAILY axis, so weekly totals land on Sundays with six
  -- empty days between and the line collapses to zero in the gaps. A
  -- STRING forces categorical spacing. Formatted YYYY-MM-DD so it sorts
  -- chronologically as text, and it preserves the Sunday boundary exactly
  -- rather than letting Looker re-bucket on its Monday-based ISO week.
  FORMAT_DATE('%Y-%m-%d', DATE_TRUNC(tl.log_date, WEEK)) AS week_label,
  SUM(tl.hours) AS hours,
  -- Billable revenue for the same bucket. Additive, so it sums over any range.
  --
  -- Derived from minutes, NOT from the `hours` column above, so that revenue
  -- means exactly what it means everywhere else: this is the same expression
  -- as v_timelog_detail.billable_amount and v_client_month.billable_revenue.
  -- timelogs.hours is stored pre-rounded (see README), so hours * rate would
  -- be a second, slightly different definition of revenue that would not tie
  -- back to those views.
  --
  -- Consequence, deliberate: within THIS view, billable_revenue / hours does
  -- not exactly equal the billable rate, because the two columns rest on
  -- different bases -- exact minutes vs pre-rounded hours. `hours` is left
  -- alone rather than "fixed": changing it would silently shift every number
  -- v_user_weekly_billable_hours has ever reported.
  --
  -- No IF needed: the WHERE below already restricts to billable entries. A
  -- NULL billable_rate yields NULL and is skipped by SUM, so a missing rate
  -- understates revenue rather than counting the work as free. Measured
  -- 2026-09-21: 0 of 18,184 billable entries lack a rate.
  SUM((tl.minutes / 60) * tl.billable_rate) AS billable_revenue
FROM {timelogs} tl
WHERE tl.is_billable = TRUE
-- week_label must be grouped explicitly. It is a pure function of
-- week_start, so this does not change the grain -- but BigQuery does not
-- infer that an expression over tl.log_date is derived from the GROUPED
-- ALIAS week_start, and rejects it as an ungrouped reference. That failed
-- a live --create-views on 2026-09-22; text tests cannot see it.
GROUP BY user_id, day_bucket, day_order, week_start, week_label
"""

    # Layer 2 — actual + projected, via UNION ALL (your preferred approach,
    # so actuals and projections show up together in one report). Replaces
    # v_user_daily_billable_hours_long/_wide, _trend_long/_wide, and
    # v_user_current_week_hybrid entirely -- all five are retired by this
    # one view. IMPORTANT MIGRATION NOTE: v_user_daily_billable_hours_wide
    # was already wired into your live "Team Hours" Combo chart -- that
    # chart will need to be rebuilt against this view once the old ones
    # are dropped, since nothing here preserves the old view names.
    #
    # Grain: one row per (user, day_bucket, week_start), spanning from the
    # start of the last COMPLETED calendar quarter through the current (
    # possibly in-progress) week -- a rolling window that's ~13 weeks
    # right after a quarter just turned over, growing to ~26 weeks right
    # before the next one does (per your instruction: "max of
    # approximately 26 weeks available at any time").
    #
    # Two UNION ALL branches, mutually exclusive by construction (verified
    # against every day of the week before building this):
    #   - ACTUAL: every already-elapsed (week, day_bucket) combination,
    #     scaffolded against v_usermins x day_buckets x a generated weekly
    #     date spine, so a real 0-hour day still shows as an explicit 0
    #     rather than a missing row. All of history before the current
    #     week is unconditionally "actual"; within the current week, a
    #     bucket is "actual" once its weekday has fully passed.
    #   - PROJECTED: only the current week's NOT-yet-elapsed buckets --
    #     flat daily_min_bill as a placeholder for today/future Mon-Thu
    #     days, and a catch-up "plug" for Friday: (5 x daily_min_bill)
    #     minus whatever the other 4 buckets are currently showing
    #     (actual where already past, assumed-minimum otherwise), clamped
    #     at 0 rather than going negative if someone's already ahead of
    #     pace by Thursday.
    #
    # Per your explicit confirmation of these edge cases:
    #   - Run on SUNDAY (the first day of its own week): the ENTIRE coming
    #     week hasn't started yet, so Monday-Thursday all show `minimum`
    #     and Friday shows the `plug` -- which works out to exactly one
    #     day's minimum, since nothing else has happened yet. This is the
    #     case you specifically asked to confirm: "Week of Aug 30" (a
    #     Sunday) appearing as the last/current row, fully projected.
    #   - Run on MONDAY: identical to Sunday -- nothing's happened yet
    #     either way.
    #   - Run on SATURDAY: the whole Mon-Fri business week has already
    #     elapsed (Saturday is the LAST day of a Sun-Sat week), so every
    #     bucket, Friday included, shows `actual` -- the PROJECTED branch
    #     produces zero rows for that week in this case.
    #
    # `value_type` ('actual' / 'minimum' / 'plug') rides along on every
    # row so Looker Studio can visually distinguish real numbers from
    # assumed/calculated ones (e.g. italicize or footnote non-actual
    # cells) instead of silently blending them.
    views["v_user_weekly_billable_hours"] = f"""
CREATE OR REPLACE VIEW {fqn("v_user_weekly_billable_hours")} AS
WITH day_buckets AS (
  SELECT 'Monday' AS day_bucket, 1 AS day_order UNION ALL
  SELECT 'Tuesday', 2 UNION ALL
  SELECT 'Wednesday', 3 UNION ALL
  SELECT 'Thursday', 4 UNION ALL
  SELECT 'Friday', 5
),
bounds AS (
  SELECT
    -- Start of the last COMPLETED quarter: back up one quarter from the
    -- start of the current (still in-progress) quarter, then snap to
    -- that date's own Sunday-week so it lines up with real week_start
    -- values below. Naturally rolls forward each time the current
    -- quarter turns over.
    DATE_TRUNC(
      DATE_SUB(DATE_TRUNC(CURRENT_DATE('{REPORTING_TIMEZONE}'), QUARTER), INTERVAL 3 MONTH),
      WEEK
    ) AS earliest_week_start,
    DATE_TRUNC(CURRENT_DATE('{REPORTING_TIMEZONE}'), WEEK) AS current_week_start,
    -- Today's weekday mapped onto the same 1-5 scale as day_order.
    -- Sunday -> 0 (the coming week hasn't started yet -- everything
    -- through Friday is still ahead). Saturday -> 6 (past Friday --
    -- the whole business week just finished).
    CASE EXTRACT(DAYOFWEEK FROM CURRENT_DATE('{REPORTING_TIMEZONE}'))
      WHEN 1 THEN 0   -- Sunday
      WHEN 2 THEN 1   -- Monday
      WHEN 3 THEN 2   -- Tuesday
      WHEN 4 THEN 3   -- Wednesday
      WHEN 5 THEN 4   -- Thursday
      WHEN 6 THEN 5   -- Friday
      WHEN 7 THEN 6   -- Saturday
    END AS today_order
),
week_spine AS (
  SELECT week_start
  FROM bounds,
       UNNEST(GENERATE_DATE_ARRAY(
         bounds.earliest_week_start, bounds.current_week_start, INTERVAL 7 DAY
       )) AS week_start
),
actual_hours AS (
  SELECT base.user_id, base.day_bucket, base.week_start, base.hours, base.billable_revenue
  FROM {fqn("v_user_daily_billable_hours_base")} base, bounds b
  WHERE base.week_start >= b.earliest_week_start
),
-- What the current week's Mon-Thu buckets are showing right now (actual
-- where already elapsed, assumed daily_min_bill otherwise) -- needed to
-- size Friday's catch-up plug in the PROJECTED branch below. Computed
-- independently of the two branches (a UNION's branches can't reference
-- each other), using the same elapsed/not-elapsed test as the ACTUAL
-- branch's WHERE clause.
current_week_pace AS (
  SELECT
    m.user_id,
    SUM(
      CASE
        WHEN db.day_order < b.today_order OR b.today_order > 5
          THEN COALESCE(ah.hours, 0)
        ELSE m.daily_min_bill
      END
    ) AS non_friday_total,
    -- The same pace in dollars: actual revenue for elapsed Mon-Thu buckets,
    -- the standard-rate target for buckets not yet elapsed. Sizes the
    -- revenue plug exactly as non_friday_total sizes the hours plug.
    SUM(
      CASE
        WHEN db.day_order < b.today_order OR b.today_order > 5
          THEN COALESCE(ah.billable_revenue, 0)
        ELSE m.daily_min_bill * m.user_rate
      END
    ) AS non_friday_revenue
  FROM {fqn("v_usermins")} m
  CROSS JOIN day_buckets db
  CROSS JOIN bounds b
  LEFT JOIN actual_hours ah
    ON ah.user_id = m.user_id AND ah.day_bucket = db.day_bucket
   AND ah.week_start = b.current_week_start
  WHERE db.day_order < 5
  GROUP BY m.user_id
)
-- ACTUAL branch.
SELECT
  m.user_id, m.email AS user_email, m.first_name, m.last_name,
  ws.week_start,
  FORMAT_DATE('%Y-%m-%d', ws.week_start) AS week_label,
  db.day_bucket, db.day_order, m.daily_min_bill,
  -- The revenue analogue of daily_min_bill: that day's billable-hours
  -- minimum at the person's STANDARD rate. Additive, so
  -- SUM(billable_revenue) / SUM(daily_min_revenue) is correct over any
  -- range. min_value is deliberately not involved -- it is non-billable
  -- value-added hours and has no bearing on revenue.
  m.daily_min_bill * m.user_rate AS daily_min_revenue,
  'actual' AS value_type,
  COALESCE(ah.hours, 0) AS hours,
  -- Real revenue for an elapsed bucket. Zero-filled like hours: a scaffolded
  -- row with no time genuinely earned nothing.
  COALESCE(ah.billable_revenue, 0) AS billable_revenue,
  SAFE_DIVIDE(COALESCE(ah.hours, 0), m.daily_min_bill) AS pct_of_min
FROM {fqn("v_usermins")} m
CROSS JOIN day_buckets db
CROSS JOIN week_spine ws
CROSS JOIN bounds b
LEFT JOIN actual_hours ah
  ON ah.user_id = m.user_id AND ah.day_bucket = db.day_bucket
 AND ah.week_start = ws.week_start
WHERE ws.week_start < b.current_week_start
   OR b.today_order > 5
   OR db.day_order < b.today_order

UNION ALL

-- PROJECTED branch: only the current week's not-yet-elapsed buckets.
-- Produces zero rows once the whole week is already elapsed (Saturday),
-- since the ACTUAL branch already covers that case.
SELECT
  m.user_id, m.email AS user_email, m.first_name, m.last_name,
  b.current_week_start AS week_start,
  FORMAT_DATE('%Y-%m-%d', b.current_week_start) AS week_label,
  db.day_bucket, db.day_order, m.daily_min_bill,
  m.daily_min_bill * m.user_rate AS daily_min_revenue,
  CASE WHEN db.day_order = 5 THEN 'plug' ELSE 'minimum' END AS value_type,
  CASE WHEN db.day_order = 5
    THEN GREATEST(m.daily_min_bill * 5 - cwp.non_friday_total, 0)
    ELSE m.daily_min_bill
  END AS hours,
  -- Projected revenue, mirroring the hours projection exactly: the
  -- standard-rate value of the day's billable-hours minimum, and on Friday
  -- the catch-up plug needed to reach the week's standard-rate target.
  --
  -- Because elapsed days count ACTUAL revenue (per-entry billable_rate)
  -- while the target is at STANDARD rate, the revenue plug can exceed zero
  -- even when the hours plug is zero. That is the point, not a bug: it means
  -- the hours were billed at below-standard rates, and the plug shows the
  -- dollars still needed. A NULL user_rate makes the projection NULL --
  -- unknown, rather than a misleading zero.
  CASE WHEN db.day_order = 5
    THEN GREATEST(m.daily_min_bill * 5 * m.user_rate - cwp.non_friday_revenue, 0)
    ELSE m.daily_min_bill * m.user_rate
  END AS billable_revenue,
  SAFE_DIVIDE(
    CASE WHEN db.day_order = 5
      THEN GREATEST(m.daily_min_bill * 5 - cwp.non_friday_total, 0)
      ELSE m.daily_min_bill
    END,
    m.daily_min_bill
  ) AS pct_of_min
FROM {fqn("v_usermins")} m
CROSS JOIN day_buckets db
CROSS JOIN bounds b
LEFT JOIN current_week_pace cwp ON cwp.user_id = m.user_id
WHERE b.today_order <= 5
  AND db.day_order >= b.today_order
"""

    # Not an exception rule and not part of the user-hours report — a wide,
    # unfiltered drill-down over every time entry, meant as a Looker Studio
    # data source for ad-hoc slicing by project, client, person, tasklist and
    # Activity. One row per timelog, always: a timelog has exactly one user,
    # one project and at most one task, and `tasks` is de-duplicated by
    # task_id in the pipeline, so none of these joins can fan out.
    #
    # Three things this view does that the two exception views above do not,
    # because a drill-down surfaces rows those rules filter away:
    #
    #  1. `task_join_status` explains WHY the task columns are blank on a
    #     row. They can be blank for two completely different reasons and a
    #     report that cannot tell them apart will read one as the other:
    #       - the time was logged against a project with no task at all
    #         (Teamwork permits this), or
    #       - the task exists in Teamwork but not in our `tasks` table,
    #         because that table is scoped to active projects plus those
    #         archived on/after ARCHIVED_PROJECT_TASKS_CUTOFF (823 of 1,889
    #         projects as of 2026-09-04), while `timelogs` is scoped only by
    #         date and so covers all of them.
    #     Without this column, "Activity is blank" looks like a compliance
    #     problem when it is often just an out-of-scope project.
    #
    #  2. `hours` is computed here as minutes/60 rather than reading
    #     timelogs.hours, which the pipeline stores pre-rounded to 4 decimal
    #     places. Rounding per row is harmless when reading one entry and
    #     accumulates when Looker SUMs tens of thousands. minutes is the
    #     value Teamwork actually holds, so it is the one to aggregate from.
    #
    #  3. `billable_status` is a string, not the raw boolean. is_billable can
    #     be NULL, and a NULL boolean in Looker silently drops out of both
    #     sides of a Yes/No filter. The raw boolean is kept alongside it for
    #     anyone who wants it.
    #
    # log_week_start uses the same Sunday-start week as
    # v_user_daily_billable_hours_base so the two reports agree on which week
    # a date belongs to — Looker's own week grouping would default to Monday
    # and quietly disagree.
    # Money columns, per an explicit decision: billable_rate (client-facing
    # revenue) is exposed, cost_rate is not. Cost is comp-adjacent, same
    # caution as users.user_cost/user_rate, and leaving it out lets this view
    # be shared more widely than the users table.
    #
    # !! billable_rate's UNITS ARE UNVERIFIED !!
    # transform.normalize_timelog passes Teamwork's billableRate straight
    # through, while the sibling userRate/userCost on the people endpoint
    # were confirmed to arrive in CENTS and are divided by 100 there. Nobody
    # has checked which billableRate is. If it is also cents, every
    # billable_amount is 100x too large. That would be a bug in transform.py,
    # not here — this view just multiplies hours by whatever the column
    # holds. Verify one entry against a rate you know before trusting a
    # revenue total, and if it is cents, fix it in transform.py and re-sync
    # rather than dividing in the SQL.
    #
    # billable_amount is NULL (not 0) for non-billable entries and for a
    # billable entry with no rate. SUM skips NULLs, so a missing rate
    # understates revenue rather than inventing zero-value billable work.
    views["v_timelog_detail"] = f"""
CREATE OR REPLACE VIEW {fqn("v_timelog_detail")} AS
SELECT
  -- the time entry
  tl.timelog_id,
  tl.log_date,
  DATE_TRUNC(tl.log_date, WEEK) AS log_week_start,
  DATE_TRUNC(tl.log_date, MONTH) AS log_month,
  tl.minutes,
  tl.minutes / 60 AS hours,
  tl.is_billable,
  CASE
    WHEN tl.is_billable IS TRUE THEN 'Billable'
    WHEN tl.is_billable IS FALSE THEN 'Non-billable'
    ELSE 'Unknown'
  END AS billable_status,
  tl.description AS timelog_description,
  tl.is_locked,

  -- UNITS ARE UNVERIFIED: see the note above this view in views.py before
  -- publishing any revenue figure from these two columns.
  tl.billable_rate,
  CASE
    WHEN tl.is_billable IS TRUE THEN (tl.minutes / 60) * tl.billable_rate
  END AS billable_amount,

  -- who the time belongs to, and who entered it
  tl.user_id,
  u.full_name AS user_name,
  u.email AS user_email,
  u.user_type,
  u.is_deleted AS user_is_deleted,
  tl.logged_by_user_id,
  lb.full_name AS logged_by_name,

  -- project and client
  p.project_id,
  p.name AS project_name,
  p.category_name,
  p.company_id,
  p.client_name,
  {proj_owner_col},
  p.status AS project_status,
  (p.archived_at IS NOT NULL) AS project_is_archived,

  -- task detail (see task_join_status before treating a blank as a finding)
  tl.task_id,
  tk.name AS task_name,
  tk.tasklist_id,
  tk.tasklist_name,
  tk.activity,
  tk.status AS task_status,
  tk.estimate_minutes,
  tk.due_date AS task_due_date,
  tk.parent_task_id,
  parent.name AS parent_task_name,

  -- Same rollup pair as v_task_review, here at timelog grain so a monthly
  -- pivot can group sub-tasks under their parent. A sub-task reports its
  -- parent, a top-level task reports itself; both branch on the SAME
  -- condition so the id and the name always describe one task.
  --
  -- Project-level time (no task at all) leaves both NULL rather than
  -- inventing a group -- task_join_status already names that case.
  --
  -- ONE LEVEL ONLY, as in v_task_review: a sub-sub-task rolls up to its own
  -- parent, not to the top of the tree.
  IF(parent.task_id IS NOT NULL, tk.parent_task_id, tk.task_id) AS rollup_task_id,
  IF(parent.task_id IS NOT NULL, parent.name, tk.name) AS rollup_task_name,

  tk.sequence_id,
  CASE
    WHEN tl.task_id IS NULL THEN 'No task (project-level time)'
    WHEN tk.task_id IS NULL THEN 'Task outside tasks-table scope'
    ELSE 'Task matched'
  END AS task_join_status,

  tl.synced_at
FROM {timelogs} tl
LEFT JOIN {projects} p ON p.project_id = tl.project_id
LEFT JOIN {tasks} tk ON tk.task_id = tl.task_id
-- tasks is de-duplicated by task_id, so this matches at most one row and
-- cannot fan out the one-row-per-timelog grain.
LEFT JOIN {tasks} parent ON parent.task_id = tk.parent_task_id
LEFT JOIN {users} u ON u.user_id = tl.user_id
LEFT JOIN {users} lb ON lb.user_id = tl.logged_by_user_id
{proj_owner_join}
"""

    # Exception rule: time posted straight to a project with no task at all.
    # Teamwork permits it, but it leaves the work undescribed — no tasklist,
    # no Activity, nothing to roll up against — so it is worth surfacing.
    #
    # Built ON v_timelog_detail rather than re-deriving from timelogs, so
    # "no task" has one definition and any fix to the joins or the derived
    # hours propagates here automatically. That makes ordering matter:
    # v_timelog_detail must exist first, and create_or_replace_views()
    # iterates this dict in insertion order, so this entry stays after it.
    #
    # The filter is `task_id IS NULL` rather than a string comparison
    # against task_join_status. Same population, but it cannot silently
    # break if that label's wording is ever edited.
    #
    # Scope is SITE-WIDE, not restricted to MONITORED_CATEGORIES — following
    # v_exception_long_time_entries rather than rules 1/2/5. Untasked time on
    # an internal or uncategorised project is just as much a gap in the
    # record as on a billable one.
    #
    # WINDOW: from the start of the PRIOR quarter onward, with no upper
    # bound. In practice that is "prior quarter + current QTD", since there
    # is normally no data after today. The upper bound is deliberately left
    # off rather than capped at CURRENT_DATE: a timelog dated in the future
    # is itself an anomaly, and an exception report should show it rather
    # than hide it. quarter_bucket splits the two periods for reporting.
    views["v_exception_time_without_task"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_time_without_task")} AS
WITH bounds AS (
  SELECT
    DATE_TRUNC(CURRENT_DATE('{REPORTING_TIMEZONE}'), QUARTER) AS current_quarter_start,
    DATE_SUB(
      DATE_TRUNC(CURRENT_DATE('{REPORTING_TIMEZONE}'), QUARTER), INTERVAL 1 QUARTER
    ) AS prior_quarter_start
)
SELECT
  d.timelog_id,
  d.log_date,
  DATE_TRUNC(d.log_date, QUARTER) AS log_quarter_start,
  CONCAT(
    CAST(EXTRACT(YEAR FROM d.log_date) AS STRING), '-Q',
    CAST(EXTRACT(QUARTER FROM d.log_date) AS STRING)
  ) AS quarter_label,
  CASE
    WHEN d.log_date >= b.current_quarter_start THEN 'Current QTD'
    ELSE 'Prior quarter'
  END AS quarter_bucket,

  d.user_id,
  d.user_name,
  d.user_email,
  d.logged_by_user_id,
  d.logged_by_name,

  d.project_id,
  d.project_name,
  d.category_name,
  d.client_name,
  d.proj_owner,
  d.project_status,
  d.project_is_archived,

  d.minutes,
  d.hours,
  d.is_billable,
  d.billable_status,
  d.timelog_description,
  d.synced_at
FROM {fqn("v_timelog_detail")} d
CROSS JOIN bounds b
WHERE d.task_id IS NULL
  AND d.log_date >= b.prior_quarter_start
"""

    # A wide, one-row-per-task review surface for data hygiene: "show me every
    # task where X is missing". Not an exception rule -- it asserts no policy
    # and flags nothing on its own. It exposes the raw dimensions and a set of
    # has_/is_ booleans, and the person reviewing decides what counts as a
    # problem by combining filters in Looker Studio.
    #
    # Scope, per your instruction: every task -- open AND completed -- whose
    # project is not archived. Completed tasks are in deliberately; a completed
    # task with no time logged or no estimate is still a hygiene finding.
    # Because the project join is a LEFT JOIN, a task whose project row is
    # missing entirely also passes the filter. That is the safe direction for a
    # review tool: an orphaned task surfaces rather than silently disappearing.
    #
    # Grain is one row per task, per your instruction -- assignees are
    # concatenated into a single string by {assignee_names}, the same trade-off
    # the other task views make. Task counts are therefore always correct with
    # no dedupe step, but an assignee filter in Looker Studio must be a "Text
    # contains" control, not an exact-match dropdown, because the distinct
    # values are combinations ("Bob, Jane") rather than people.
    #
    # Time columns come from one pre-aggregated CTE joined once, rather than a
    # correlated subquery per column: six time-derived columns as six EXISTS/
    # SUM subqueries would re-scan timelogs six times per task.
    #
    # CAVEAT on has_time_logged and every column derived from it: `timelogs`
    # only holds history the pipeline has actually loaded, which currently
    # begins 2026-01-01. A task whose only time was posted before then reads
    # has_time_logged = FALSE. Treat "no time logged" as "no time in loaded
    # history", and widen it with --backfill-months before drawing a
    # conclusion about an older task.
    views["v_task_review"] = f"""
CREATE OR REPLACE VIEW {fqn("v_task_review")} AS
WITH task_time AS (
  SELECT
    tl.task_id,
    SUM(tl.minutes) / 60 AS logged_hours,
    SUM(IF(tl.is_billable, tl.minutes, 0)) / 60 AS billable_logged_hours,
    COUNT(*) AS time_entry_count,
    COUNT(DISTINCT tl.user_id) AS time_contributor_count,
    MIN(tl.log_date) AS first_time_logged_date,
    MAX(tl.log_date) AS last_time_logged_date
  FROM {timelogs} tl
  WHERE tl.task_id IS NOT NULL AND tl.minutes > 0
  GROUP BY tl.task_id
)
SELECT
  -- identity, and a way back to the record so a reviewer can fix it
  t.task_id,
  t.name AS task_name,
  t.web_link AS task_url,

  -- project and client context
  p.project_id,
  p.name AS project_name,
  {proj_owner_col},
  p.category_name,
  p.client_name,
  p.status AS project_status,
  p.is_billable AS project_is_billable,

  -- where the task sits
  t.tasklist_id,
  t.tasklist_name,
  t.parent_task_id,
  (t.parent_task_id IS NOT NULL) AS has_parent_task,
  parent.name AS parent_task_name,

  -- One pair of columns to group a report by so a parent and its sub-tasks
  -- land on a single line: a sub-task reports its parent, a top-level task
  -- reports itself. parent_task_id alone cannot do this -- it is NULL on
  -- top-level tasks, and an integer is not a readable report dimension.
  --
  -- Both fall back to the task itself only when the parent actually resolved,
  -- so the id and the name always describe the SAME task. Coalescing them
  -- independently would pair a parent's id with a child's name whenever the
  -- parent is missing from the tasks table (deleted, or outside the pull).
  --
  -- ONE LEVEL ONLY: parent_task_id is the immediate parent, so a
  -- sub-sub-task rolls up to its own parent, not to the top of the tree.
  IF(parent.task_id IS NOT NULL, t.parent_task_id, t.task_id) AS rollup_task_id,
  IF(parent.task_id IS NOT NULL, parent.name, t.name) AS rollup_task_name,

  -- who owns the work
  {assignee_names},
  COALESCE(ARRAY_LENGTH(t.assignee_user_ids), 0) AS assignee_count,
  COALESCE(ARRAY_LENGTH(t.assignee_user_ids), 0) > 0 AS has_assignee,

  -- state
  t.status AS task_status,
  (COALESCE(t.status, '') = 'completed') AS is_completed,
  t.priority,
  t.progress_pct,
  t.is_private,

  -- recurrence: Teamwork's native mechanism, shared sequence_id per series
  t.sequence_id,
  (t.sequence_id IS NOT NULL) AS is_recurring,

  -- the Activity custom field
  t.activity,
  (t.activity IS NOT NULL) AS has_activity,

  -- estimate
  t.estimate_minutes,
  t.estimate_minutes / 60 AS estimate_hours,
  (COALESCE(t.estimate_minutes, 0) > 0) AS has_estimate,

  -- dates
  t.start_date,
  t.due_date,
  (t.due_date IS NOT NULL) AS has_due_date,
  (
    t.due_date IS NOT NULL
    AND COALESCE(t.status, '') != 'completed'
    AND t.due_date < CURRENT_DATE('{REPORTING_TIMEZONE}')
  ) AS is_overdue,
  IF(
    t.due_date IS NOT NULL
      AND COALESCE(t.status, '') != 'completed'
      AND t.due_date < CURRENT_DATE('{REPORTING_TIMEZONE}'),
    DATE_DIFF(CURRENT_DATE('{REPORTING_TIMEZONE}'), t.due_date, DAY),
    NULL
  ) AS days_overdue,

  -- time actually posted (see the has_time_logged caveat above)
  COALESCE(tt.logged_hours, 0) AS logged_hours,
  COALESCE(tt.billable_logged_hours, 0) AS billable_logged_hours,
  COALESCE(tt.time_entry_count, 0) AS time_entry_count,
  COALESCE(tt.time_contributor_count, 0) AS time_contributor_count,
  (tt.task_id IS NOT NULL) AS has_time_logged,
  tt.first_time_logged_date,
  tt.last_time_logged_date,

  -- estimate vs actual. NULL, not 0, where there is no estimate to compare
  -- against: SUM skips NULLs, so an un-estimated task cannot drag a variance
  -- total toward zero as though it had come in exactly on budget.
  IF(
    COALESCE(t.estimate_minutes, 0) > 0,
    COALESCE(tt.logged_hours, 0) - (t.estimate_minutes / 60),
    NULL
  ) AS estimate_variance_hours,
  IF(
    COALESCE(t.estimate_minutes, 0) > 0,
    ROUND(100 * SAFE_DIVIDE(COALESCE(tt.logged_hours, 0), t.estimate_minutes / 60), 1),
    NULL
  ) AS pct_of_estimate_used,
  (
    COALESCE(t.estimate_minutes, 0) > 0
    AND COALESCE(tt.logged_hours, 0) > (t.estimate_minutes / 60)
  ) AS is_over_estimate,

  -- staleness
  t.created_at,
  t.updated_at,
  -- DATE(timestamp) alone converts in UTC; the second argument is required
  -- or this subtracts a UTC-derived date from an Eastern one and reads a day
  -- off for anything updated 20:00-23:59 ET. Same trap as an unqualified
  -- current-date call, one level down.
  DATE_DIFF(
    CURRENT_DATE('{REPORTING_TIMEZONE}'),
    DATE(t.updated_at, '{REPORTING_TIMEZONE}'),
    DAY
  ) AS days_since_updated,
  IF(
    tt.last_time_logged_date IS NOT NULL,
    DATE_DIFF(CURRENT_DATE('{REPORTING_TIMEZONE}'), tt.last_time_logged_date, DAY),
    NULL
  ) AS days_since_last_time,
  (COALESCE(t.description, '') != '') AS has_description,

  -- Convenience only, so a reviewer can sort worst-first: how many of the
  -- four gaps below this task has. The individual booleans above are the
  -- source of truth -- this asserts no policy about which gaps matter on
  -- which task.
  --
  -- Two flags are deliberately NOT counted, both because they are near-
  -- constants on this account and a term that is almost always 1 (or almost
  -- always 0) adds an offset rather than discriminating between tasks:
  --
  --   has_description -- measured 2026-09-14, ~90% of open tasks have no
  --     description, so counting it added ~1 to nearly every score and
  --     compressed the range that makes sorting worst-first useful.
  --   has_time_logged -- "no time yet" is normal for a task not yet started,
  --     and it also inherits the loaded-history floor (see the caveat above).
  --
  -- Both remain available as standalone filter columns.
  (
    CAST(COALESCE(ARRAY_LENGTH(t.assignee_user_ids), 0) = 0 AS INT64)
    + CAST(COALESCE(t.estimate_minutes, 0) = 0 AS INT64)
    + CAST(t.activity IS NULL AS INT64)
    + CAST(t.due_date IS NULL AS INT64)
  ) AS hygiene_gap_count,

  t.synced_at
FROM {tasks} t
LEFT JOIN {projects} p ON p.project_id = t.project_id
LEFT JOIN {users} owner ON owner.user_id = p.owner_id
LEFT JOIN task_time tt ON tt.task_id = t.task_id
-- tasks is de-duplicated by task_id in the pipeline, so this self-join
-- matches at most one row and cannot fan out the grain.
LEFT JOIN {tasks} parent ON parent.task_id = t.parent_task_id
WHERE p.archived_at IS NULL
"""

    # One row per ACTIVE project -- the projects-side counterpart to
    # v_timelog_detail and v_task_review. Exists mainly because the raw
    # `projects` table is a poor Looker Studio source: owner_id, created_by
    # and completed_by are bare user ids rather than names, and tag_ids is a
    # REPEATED column the connector cannot handle.
    #
    # Scope is `archived_at IS NULL`, the same predicate v_task_review uses,
    # so the two views can never disagree about which projects exist. On this
    # account `status = 'active'` is perfectly collinear with it, so the two
    # spellings currently select the same 219 projects; a test asserts the
    # predicate matches v_task_review's rather than letting them drift. The
    # raw `status` stays exposed as a column for filtering.
    #
    # Deliberately NOT carried:
    #   tag_ids -- REPEATED, and no tags table is ingested, so the only thing
    #     this could emit is a string of bare ids with no names. Useless as a
    #     Looker dimension; a tag lookup would be new ingestion work.
    #   health -- best-effort on the standard payload and empty in practice
    #     (see schemas.py). Shipping a column that is NULL on every row is the
    #     exact failure the two web_link columns represented.
    #   cost_rate / user_cost / user_rate -- comp-adjacent, same caution as
    #     v_timelog_detail, so this view can be shared more widely.
    #
    # Rollups come from two pre-aggregated CTEs, each grouped by project_id and
    # therefore at most one row per project, so the LEFT JOINs cannot fan out
    # and the view stays one row per project without a DISTINCT.
    views["v_project_detail"] = f"""
CREATE OR REPLACE VIEW {fqn("v_project_detail")} AS
WITH project_tasks AS (
  SELECT
    t.project_id,
    COUNT(*) AS task_count,
    COUNTIF(COALESCE(t.status, '') = 'completed') AS completed_task_count,
    COUNTIF(COALESCE(t.status, '') != 'completed') AS open_task_count
  FROM {tasks} t
  WHERE t.project_id IS NOT NULL
  GROUP BY t.project_id
),
project_time AS (
  SELECT
    tl.project_id,
    SUM(tl.minutes) / 60 AS logged_hours,
    SUM(IF(tl.is_billable, tl.minutes, 0)) / 60 AS billable_logged_hours,
    COUNT(*) AS time_entry_count,
    MAX(tl.log_date) AS last_time_logged_date
  FROM {timelogs} tl
  WHERE tl.project_id IS NOT NULL AND tl.minutes > 0
  GROUP BY tl.project_id
)
SELECT
  -- identity, and a way into the record
  p.project_id,
  p.name AS project_name,
  p.web_link AS project_url,
  p.description,

  -- client and classification
  p.client_name,
  p.company_id,
  p.category_name,
  p.status AS project_status,
  p.sub_status,
  p.is_billable AS project_is_billable,

  -- people, resolved to names: the main reason this view exists
  owner.full_name AS proj_owner,
  creator.full_name AS created_by_name,
  completer.full_name AS completed_by_name,

  -- dates and state
  p.start_date,
  p.end_date,
  p.created_at,
  p.updated_at,
  p.completed_at,
  (p.completed_at IS NOT NULL) AS is_completed,
  (
    p.end_date IS NOT NULL
    AND p.completed_at IS NULL
    AND p.end_date < CURRENT_DATE('{REPORTING_TIMEZONE}')
  ) AS is_past_end_date,
  -- DATE(timestamp) alone converts in UTC; the timezone argument is required
  -- or this subtracts a UTC-derived date from an Eastern one.
  DATE_DIFF(
    CURRENT_DATE('{REPORTING_TIMEZONE}'),
    DATE(p.updated_at, '{REPORTING_TIMEZONE}'),
    DAY
  ) AS days_since_updated,

  -- budget, in DOLLARS (transform.cents_to_dollars converts on ingest)
  p.budget_capacity,
  p.budget_used,
  p.budget_left,
  (COALESCE(p.budget_capacity, 0) > 0) AS has_budget,
  -- NULL, not 0, without a budget: SUM skips NULLs, so an un-budgeted project
  -- cannot drag an average toward zero as though it had spent nothing.
  IF(
    COALESCE(p.budget_capacity, 0) > 0,
    ROUND(100 * SAFE_DIVIDE(p.budget_used, p.budget_capacity), 1),
    NULL
  ) AS pct_of_budget_used,
  (
    COALESCE(p.budget_capacity, 0) > 0
    AND COALESCE(p.budget_used, 0) > p.budget_capacity
  ) AS is_over_budget,

  -- task rollup. Counts every task the pipeline holds for the project; the
  -- tasks table covers active projects in full, so this is complete here.
  COALESCE(pt.task_count, 0) AS task_count,
  COALESCE(pt.open_task_count, 0) AS open_task_count,
  COALESCE(pt.completed_task_count, 0) AS completed_task_count,

  -- time rollup.
  --
  -- CAVEAT: these count only time the pipeline has LOADED, which currently
  -- begins 2026-01-01, so any project worked before then is understated.
  -- They also do not reconcile with budget_used above and are not meant to:
  -- budget_used is Teamwork's own budget tracking, while these are summed
  -- from our timelogs. Use budget_used for budget consumption and these for
  -- "what did we actually record against this project".
  COALESCE(ptm.logged_hours, 0) AS logged_hours,
  COALESCE(ptm.billable_logged_hours, 0) AS billable_logged_hours,
  COALESCE(ptm.time_entry_count, 0) AS time_entry_count,
  ptm.last_time_logged_date,
  IF(
    ptm.last_time_logged_date IS NOT NULL,
    DATE_DIFF(
      CURRENT_DATE('{REPORTING_TIMEZONE}'), ptm.last_time_logged_date, DAY
    ),
    NULL
  ) AS days_since_last_time,

  p.synced_at
FROM {projects} p
LEFT JOIN {users} owner ON owner.user_id = p.owner_id
LEFT JOIN {users} creator ON creator.user_id = p.created_by
LEFT JOIN {users} completer ON completer.user_id = p.completed_by
LEFT JOIN project_tasks pt ON pt.project_id = p.project_id
LEFT JOIN project_time ptm ON ptm.project_id = p.project_id
WHERE p.archived_at IS NULL
"""

    # One row per client per month: billable hours, billable revenue, and the
    # monthly budget in force that month. Built so "% of budget" works over a
    # DYNAMIC date range.
    #
    # The grain is the whole point. A blend that joins a client-level budget to
    # month-level revenue contributes the budget ONCE no matter how many months
    # the reader selects, so revenue scales with the range and the denominator
    # does not -- three months of revenue over one month of budget. Putting the
    # monthly budget on each month's row makes the denominator scale on its
    # own: SUM(billable_revenue) / SUM(monthly_budget) is correct for one
    # month, a quarter, or year-to-date, with no per-card arithmetic.
    #
    # So the ADDITIVE columns are the contract. pct_of_budget on a row is a
    # convenience for single-month display only -- summing or averaging it
    # across months is wrong. Divide the two sums instead.
    #
    # Month spine: one row per month each budgeted project is live, bounded by
    # its own start_date and end_date per your instruction, clamped to
    # CLIENT_MONTH_HISTORY_FLOOR at the bottom and the current month at the
    # top. A project with no end_date is treated as ongoing. Budget therefore
    # accrues only while the engagement is live -- charging a client for months
    # before they onboarded would make the percentage meaningless.
    #
    # Budget is the CURRENT recurring budget repeated across months, because
    # transform.pick_current_budget() keeps only the active one and the
    # warehouse holds no budget history. Accepted deliberately (the feature is
    # new in Teamwork); if a recurring budget is ever edited, past months
    # silently re-base. See README.
    views["v_client_month"] = f"""
CREATE OR REPLACE VIEW {fqn("v_client_month")} AS
WITH bounds AS (
  SELECT
    DATE_TRUNC(DATE('{CLIENT_MONTH_HISTORY_FLOOR}'), MONTH) AS floor_month,
    DATE_TRUNC(CURRENT_DATE('{REPORTING_TIMEZONE}'), MONTH) AS current_month
),
-- Every non-archived project that carries a budget, with the month window it
-- is live for. Projects without a budget are absent here on purpose: they
-- contribute no denominator. They still contribute revenue below.
budgeted_projects AS (
  SELECT
    p.project_id,
    p.client_name,
    p.budget_capacity AS monthly_budget,
    GREATEST(
      DATE_TRUNC(COALESCE(p.start_date, b.floor_month), MONTH),
      b.floor_month
    ) AS first_month,
    LEAST(
      DATE_TRUNC(COALESCE(p.end_date, b.current_month), MONTH),
      b.current_month
    ) AS last_month
  FROM {projects} p
  CROSS JOIN bounds b
  WHERE p.archived_at IS NULL
    AND COALESCE(p.budget_capacity, 0) > 0
    AND p.client_name IS NOT NULL
),
-- Expand each budgeted project across the months it is live. A project whose
-- window is empty (ended before the floor) yields no rows, not an error.
project_months AS (
  SELECT
    bp.client_name,
    bp.project_id,
    bp.monthly_budget,
    month_start
  FROM budgeted_projects bp,
  UNNEST(
    GENERATE_DATE_ARRAY(bp.first_month, bp.last_month, INTERVAL 1 MONTH)
  ) AS month_start
),
client_month_budget AS (
  SELECT
    client_name,
    month_start,
    SUM(monthly_budget) AS monthly_budget,
    COUNT(DISTINCT project_id) AS budgeted_project_count
  FROM project_months
  GROUP BY client_name, month_start
),
client_month_actuals AS (
  SELECT
    d.client_name,
    d.log_month AS month_start,
    SUM(d.minutes) / 60 AS logged_hours,
    SUM(IF(d.is_billable, d.minutes, 0)) / 60 AS billable_hours,
    -- billable_amount is already NULL on non-billable entries, so SUM skips
    -- them without a guard.
    SUM(d.billable_amount) AS billable_revenue,
    COUNT(*) AS time_entry_count,
    COUNT(DISTINCT d.project_id) AS project_count
  FROM {fqn("v_timelog_detail")} d
  WHERE d.client_name IS NOT NULL
  GROUP BY d.client_name, d.log_month
)
SELECT
  -- FULL OUTER JOIN so neither side is lost: a budgeted month with no time
  -- still consumes budget, and revenue on an unbudgeted project still shows.
  COALESCE(b.client_name, a.client_name) AS client_name,
  COALESCE(b.month_start, a.month_start) AS month_start,
  FORMAT_DATE('%Y-%m', COALESCE(b.month_start, a.month_start)) AS month_label,

  -- additive: safe to SUM over any date range
  COALESCE(b.monthly_budget, 0) AS monthly_budget,
  COALESCE(a.billable_revenue, 0) AS billable_revenue,
  COALESCE(a.billable_hours, 0) AS billable_hours,
  COALESCE(a.logged_hours, 0) AS logged_hours,
  COALESCE(a.time_entry_count, 0) AS time_entry_count,
  -- Rollout check. While budgets are still being added to active projects,
  -- project_count exceeding budgeted_project_count means this client-month's
  -- revenue includes work from projects contributing no denominator, so any
  -- budget percentage computed from it reads high. Converges as budgets land.
  COALESCE(b.budgeted_project_count, 0) AS budgeted_project_count,
  COALESCE(a.project_count, 0) AS project_count,
  (COALESCE(b.monthly_budget, 0) > 0) AS has_budget

  -- DELIBERATELY NO pct_of_budget OR is_over_budget COLUMN.
  --
  -- Every column above is additive and safe to SUM over any date range. A
  -- row-level percentage is not, and shipping one here was a mistake that was
  -- removed on 2026-09-21:
  --
  --   1. It cannot be summed or averaged across months, so it is wrong for
  --      every range except a single month -- and a dynamic range is the one
  --      thing this view exists to support. A column that looks usable and is
  --      not is worse than no column.
  --   2. This repo's pct_* columns are already multiplied by 100, so applying
  --      Looker Studio's native Percent type multiplies again: a real 128%
  --      rendered as 12,840%. Observed in a live report.
  --
  -- Compute it in the report instead, as an AGGREGATE over the additive
  -- columns. This is correct for one month, a quarter or year-to-date, and
  -- returns a ratio that formats natively as Percent:
  --
  --   SUM(billable_revenue) / SUM(monthly_budget)
  --
  -- "Over budget" is the same expression compared to 1.
FROM client_month_budget b
FULL OUTER JOIN client_month_actuals a
  ON a.client_name = b.client_name
 AND a.month_start = b.month_start
"""

    # One row per logged date x user x client x Activity, with the hours split
    # four ways: PTO, internal, client billable and CNB (client non-billable).
    #
    # The rule, per your definition, tested in this order:
    #   - all time on the PTO task (PTO_TASK_ID) is PTO, whatever its client or
    #     billable flag. It is tested FIRST so leave never lands in
    #     internal_hours, which it otherwise would, being on the FFI client;
    #   - all other time on the client INTERNAL_CLIENT_COMPANY_ID (FFI) is
    #     internal, BILLABLE OR NOT;
    #   - time on any other client is external, and splits on the entry's own
    #     billable flag into client_billable_hours and cnb_hours.
    # So a billable entry on the internal client lands in internal_hours, not
    # client_billable_hours. v_exception_billable_time_internal_projects is the
    # place that flags such entries; this view only sorts time.
    #
    # Classified on company_id rather than client_name so a rename in Teamwork
    # cannot silently reclassify internal time.
    #
    # Two cases the three-way split cannot place, handled explicitly rather
    # than guessed:
    #   - A project with NO client (company_id NULL). It is not the internal
    #     client and not an external one, so it goes to its own column,
    #     no_client_hours, instead of being silently counted as either. The
    #     five hour columns therefore always sum to total_hours. If those
    #     projects turn out to be internal, fold them in by changing the CASE
    #     below -- not in the report.
    #   - An external entry whose billable flag is NULL. It counts as CNB: only
    #     time Teamwork positively marks billable is billable, so revenue-side
    #     numbers are never inflated by an unknown.
    #
    # PTO is often posted ahead of the leave, so pto_hours can appear on future
    # dates. That is deliberate: this view is unbounded, and future PTO is
    # real planned leave, not an error.
    #
    # client_type still describes the CLIENT, so a PTO row's client_type is
    # whatever its project's client is (FFI: Internal). Filter or stack on the
    # hour columns, not client_type, to separate PTO from internal work.
    #
    # Daily grain, with week_start (the Sunday that begins the entry's week)
    # alongside so Looker can roll days up to weeks. It is the same Sunday-start
    # week as every other report, taken from v_timelog_detail.log_week_start,
    # never Looker's own Monday-based ISO week. week_label is its text form,
    # for chart axes (a DATE dimension makes Looker plot a daily axis).
    #
    # Built on v_timelog_detail, so client, Activity and the minutes-based
    # hours all mean exactly what they mean in every other time report. Must
    # therefore be created after v_timelog_detail.
    #
    # activity is the TASK's Activity, so it is NULL for project-level time
    # (no task) and for tasks outside the tasks-table scope. Those rows still
    # carry their hours; they just group under a blank Activity.
    #
    # The classification and the derived labels are computed once in the CTE,
    # so the outer GROUP BY lists plain column names only. BigQuery will not
    # accept an expression in the SELECT that is derived from a grouped column
    # unless the expression itself is grouped (see the week_label failure in
    # README "Known gaps").
    #
    # Unbounded, like v_user_daily_billable_hours_base: every loaded day.
    views["v_user_daily_time_split"] = f"""
CREATE OR REPLACE VIEW {fqn("v_user_daily_time_split")} AS
WITH classified AS (
  SELECT
    d.log_date,
    d.log_week_start AS week_start,
    FORMAT_DATE('%Y-%m-%d', d.log_week_start) AS week_label,
    d.user_id,
    d.user_name,
    d.user_email,
    d.company_id,
    d.client_name,
    d.activity,
    d.minutes,
    CASE
      WHEN d.company_id IS NULL THEN 'No client'
      WHEN d.company_id = {int(INTERNAL_CLIENT_COMPANY_ID)} THEN 'Internal'
      ELSE 'External'
    END AS client_type,
    -- Order matters. PTO comes first, so leave on the FFI client is PTO, not
    -- internal. The client test comes BEFORE the billable test, so billable
    -- time on the internal client is still internal.
    CASE
      WHEN d.task_id = {int(PTO_TASK_ID)} THEN 'PTO'
      WHEN d.company_id IS NULL THEN 'No client'
      WHEN d.company_id = {int(INTERNAL_CLIENT_COMPANY_ID)} THEN 'Internal'
      WHEN d.is_billable IS TRUE THEN 'Client Billable'
      ELSE 'CNB'
    END AS time_class
  FROM {fqn("v_timelog_detail")} d
)
SELECT
  log_date,
  week_start,
  week_label,
  user_id,
  user_name,
  user_email,
  company_id,
  client_name,
  client_type,
  activity,
  SUM(IF(time_class = 'PTO', minutes, 0)) / 60 AS pto_hours,
  SUM(IF(time_class = 'Internal', minutes, 0)) / 60 AS internal_hours,
  SUM(IF(time_class = 'Client Billable', minutes, 0)) / 60 AS client_billable_hours,
  SUM(IF(time_class = 'CNB', minutes, 0)) / 60 AS cnb_hours,
  SUM(IF(time_class = 'No client', minutes, 0)) / 60 AS no_client_hours,
  SUM(minutes) / 60 AS total_hours,
  COUNT(*) AS time_entry_count
FROM classified
GROUP BY log_date, week_start, week_label, user_id, user_name, user_email, company_id, client_name, client_type, activity
"""

    # One row, and the only view here whose subject is the pipeline itself
    # rather than the business. It exists so a Looker Studio report can show
    # "last updated" without every report re-deriving it, and because
    # synced_at is a BigQuery TIMESTAMP: Looker renders those in UTC, so a
    # 09:53 UTC sync reads as a mid-morning refresh when it was actually
    # 05:53 ET. The conversion belongs here, where DATETIME(ts, tz) resolves
    # DST off the tz database. The Looker-side alternative is a fixed-offset
    # DATETIME_SUB(synced_at, INTERVAL 4 HOUR), which is silently an hour
    # wrong every November -- the same shape as the CURRENT_DATE() bug.
    #
    # TWO THINGS THAT LOOK LIKE MISTAKES AND ARE NOT:
    #
    # 1. MAX within each table, then MIN across them. MAX within is required
    #    because timelogs is a *windowed* replace -- only the rolling window's
    #    rows get a fresh synced_at, so an untouched January row keeps a
    #    months-old stamp and MIN within that table would report it as the
    #    sync time. MIN across the four is the honest headline: the pipeline
    #    fails a stage rather than writing bad data, so a partial failure
    #    leaves one table stale while the rest are current, and MAX across
    #    would claim a freshness the dashboard does not have. last_synced_at
    #    therefore means "every table is at least this fresh".
    #
    # 2. CURRENT_TIMESTAMP() carries no timezone argument, unlike every
    #    CURRENT_DATE() in this file. That is correct, not an oversight: a
    #    TIMESTAMP is an absolute instant and TIMESTAMP_DIFF between two of
    #    them is timezone-independent. Adding a zone here would be a no-op at
    #    best. The timezone only matters when rendering, which is what the
    #    DATETIME() and FORMAT_TIMESTAMP() calls below are for.
    views["v_data_freshness"] = f"""
CREATE OR REPLACE VIEW {fqn("v_data_freshness")} AS
WITH per_table AS (
  SELECT 'projects' AS table_name, MAX(synced_at) AS synced_at FROM {projects}
  UNION ALL
  SELECT 'tasks' AS table_name, MAX(synced_at) AS synced_at FROM {tasks}
  UNION ALL
  SELECT 'users' AS table_name, MAX(synced_at) AS synced_at FROM {users}
  UNION ALL
  SELECT 'timelogs' AS table_name, MAX(synced_at) AS synced_at FROM {timelogs}
),
rolled AS (
  SELECT
    -- MIN/MAX skip NULLs, so an empty table would vanish from both rather
    -- than dragging the headline down. tables_reporting is the guard: it
    -- should always be 4, and anything less means a table this view thinks
    -- it is vouching for is empty.
    COUNTIF(synced_at IS NOT NULL) AS tables_reporting,
    MIN(synced_at) AS oldest_synced_at,
    MAX(synced_at) AS newest_synced_at,
    MAX(IF(table_name = 'projects', synced_at, NULL)) AS projects_synced_at,
    MAX(IF(table_name = 'tasks', synced_at, NULL)) AS tasks_synced_at,
    MAX(IF(table_name = 'users', synced_at, NULL)) AS users_synced_at,
    MAX(IF(table_name = 'timelogs', synced_at, NULL)) AS timelogs_synced_at
  FROM per_table
)
SELECT
  -- THE DISPLAY COLUMN. A BigQuery DATETIME has no timezone, so Looker
  -- shows it verbatim instead of re-interpreting it as UTC.
  DATETIME(r.oldest_synced_at, '{REPORTING_TIMEZONE}') AS last_synced_at_et,
  -- Pre-formatted for a scorecard that wants a sentence rather than a date
  -- widget, e.g. "Sep 21, 2026 at 05:53 AM".
  FORMAT_TIMESTAMP(
    '%b %d, %Y at %I:%M %p', r.oldest_synced_at, '{REPORTING_TIMEZONE}'
  ) AS last_updated_label,
  r.oldest_synced_at AS last_synced_at_utc,

  -- Age. "05:53 AM" means nothing to a reader who does not know what normal
  -- looks like; the measured gap between scheduled syncs is 9.5-15.1h.
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.oldest_synced_at, MINUTE)
    AS minutes_since_sync,
  ROUND(
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.oldest_synced_at, SECOND) / 3600, 1
  ) AS hours_since_sync,
  (
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.oldest_synced_at, HOUR)
      >= {DATA_FRESHNESS_STALE_AFTER_HOURS}
  ) AS is_stale,

  -- Diagnostics. Within one healthy run the four stages land seconds to a
  -- couple of minutes apart, so stage_skew_minutes is small. Hours of skew
  -- means a stage failed and its table was never rewritten -- exactly the
  -- case the MIN above is protecting the headline from.
  r.tables_reporting,
  DATETIME(r.newest_synced_at, '{REPORTING_TIMEZONE}') AS newest_synced_at_et,
  TIMESTAMP_DIFF(r.newest_synced_at, r.oldest_synced_at, MINUTE)
    AS stage_skew_minutes,
  DATETIME(r.projects_synced_at, '{REPORTING_TIMEZONE}') AS projects_synced_at_et,
  DATETIME(r.tasks_synced_at, '{REPORTING_TIMEZONE}') AS tasks_synced_at_et,
  DATETIME(r.users_synced_at, '{REPORTING_TIMEZONE}') AS users_synced_at_et,
  DATETIME(r.timelogs_synced_at, '{REPORTING_TIMEZONE}') AS timelogs_synced_at_et
FROM rolled r
"""

    return views


def list_orphan_views(bq_client, project_id, dataset):
    """Views that exist in the dataset but are no longer in VIEW_NAMES.

    `create_or_replace_views()` never drops anything, so a view retired from
    VIEW_NAMES stays live in BigQuery, frozen at whatever SQL it last had —
    while still querying the live tables. That is the dangerous shape: it
    returns fresh-looking numbers from obsolete logic, and nothing announces
    it. Two of these were found by eye in a console screenshot months after
    the fact; one of them still carried a Monday-start business week and a
    bare CURRENT_DATE(), both long since fixed everywhere else.

    Only VIEWS are listed, so the externally-managed Google Sheet table
    (ANCILLARY_USER_INFO_TABLE) and the four native tables never appear here.

    Returns a sorted list, [] if the dataset is clean, or None if the check
    itself could not run — reporting a false "clean" would be worse than
    admitting the check failed.
    """
    sql = (
        f"SELECT table_name FROM `{project_id}.{dataset}.INFORMATION_SCHEMA.VIEWS` "
        "ORDER BY table_name"
    )
    try:
        existing = {row[0] for row in bq_client.query(sql).result()}
    except Exception as exc:
        logger.warning(
            "Could not list existing views to check for orphans (%s) — "
            "this is informational only and does not affect the views just created.",
            exc,
        )
        return None
    return sorted(existing - set(VIEW_NAMES))


def create_or_replace_views(bq_client, project_id, dataset):
    """Creates (or updates) every view in VIEW_NAMES. Views are just saved
    queries — this is cheap and safe to re-run any time the rule constants
    above change or the underlying tables' schema changes.
    """
    views = build_view_sql(project_id, dataset)
    results = {}
    for view_name, sql in views.items():
        try:
            bq_client.query(sql).result()
            logger.info("View ready: %s.%s.%s", project_id, dataset, view_name)
            results[view_name] = "ok"
        except Exception as exc:
            logger.error("Failed to create/replace view %s: %s", view_name, exc)
            results[view_name] = f"failed: {exc}"
    return results
