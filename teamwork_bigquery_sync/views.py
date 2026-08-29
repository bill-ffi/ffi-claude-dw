"""BigQuery views for the exception / quality-control reporting layer.

Five views, one per rule agreed on for identifying where staff isn't using
Teamwork the way it's meant to be used. Each view is meant to be the direct
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
# user_id), first, last, email, as_of, min_bill, min_value. The last two
# are comp-adjacent (a per-person minimum billing figure) — same caution
# as users.user_cost/user_rate in schemas.py: worth restricting read
# access if this ever gets shared beyond its current audience.
ANCILLARY_USER_INFO_TABLE = "gs_minimum_user_info"

VIEW_NAMES = [
    "v_exception_missing_activity",
    "v_exception_missing_estimate",
    "v_exception_billable_time_internal_projects",
    "v_exception_long_time_entries",
    "v_exception_recurring_compliance",
    "v_usermins",
    "v_user_daily_billable_hours",
    "v_user_daily_billable_hours_trend",
]


def _sql_string_array(values):
    return "[" + ", ".join("'" + v.replace("'", "\\'") + "'" for v in values) + "]"


def build_view_sql(project_id, dataset):
    """Returns {view_name: CREATE OR REPLACE VIEW sql} for all five views."""

    def fqn(table):
        return f"`{project_id}.{dataset}.{table}`"

    projects = fqn("projects")
    tasks = fqn("tasks")
    timelogs = fqn("timelogs")
    users = fqn("users")

    monitored = _sql_string_array(MONITORED_CATEGORIES)
    internal = _sql_string_array(INTERNAL_CATEGORIES)
    exempt_tasklists = _sql_string_array(ESTIMATE_EXEMPT_TASKLISTS)

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

    views["v_exception_missing_activity"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_missing_activity")} AS
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
  {has_time_logged_col},
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
"""

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
    AND t.tasklist_name IN UNNEST({exempt_tasklists})
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
WHERE tl.hours > {LONG_ENTRY_THRESHOLD_HOURS}
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
    # every Teamwork user. min_bill/min_value are weekly figures in the
    # sheet; daily versions are derived (/5) alongside them.
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
"""

    # Per-user average daily billable hours, bucketed into 5 weekday
    # columns for a bar chart: Sunday's hours fold into Monday, Saturday's
    # fold into Friday (both within the SAME Mon-Sun week, via BigQuery's
    # WEEK(MONDAY) truncation — a Sunday is grouped with the Monday that
    # started its own week, not the next one). Two series per user/day:
    # "Current Week" (this week so far, live) and "Prior 4-Week Avg" (the
    # four full Mon-Sun weeks before this one, divided by 4 always — a
    # day with zero logged hours counts as 0, not excluded from the
    # average). Scoped to users present in v_usermins (i.e. who have a
    # daily_min_bill target), via CROSS JOIN, so every user always has all
    # 5 weekday buckets and both periods present, even at 0 hours — no
    # gaps for Looker Studio's chart to render oddly.
    views["v_user_daily_billable_hours"] = f"""
CREATE OR REPLACE VIEW {fqn("v_user_daily_billable_hours")} AS
WITH day_buckets AS (
  SELECT 'Monday' AS day_bucket, 1 AS day_order UNION ALL
  SELECT 'Tuesday', 2 UNION ALL
  SELECT 'Wednesday', 3 UNION ALL
  SELECT 'Thursday', 4 UNION ALL
  SELECT 'Friday', 5
),
billable AS (
  SELECT
    tl.user_id,
    tl.hours,
    DATE_TRUNC(tl.log_date, WEEK(MONDAY)) AS week_start,
    CASE EXTRACT(DAYOFWEEK FROM tl.log_date)
      WHEN 1 THEN 'Monday'    -- Sunday folds into Monday (same week)
      WHEN 2 THEN 'Monday'
      WHEN 3 THEN 'Tuesday'
      WHEN 4 THEN 'Wednesday'
      WHEN 5 THEN 'Thursday'
      WHEN 6 THEN 'Friday'
      WHEN 7 THEN 'Friday'    -- Saturday folds into Friday (same week)
    END AS day_bucket
  FROM {timelogs} tl
  WHERE tl.is_billable = TRUE
),
this_week AS (
  SELECT DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY)) AS week_start
),
current_week_hours AS (
  SELECT b.user_id, b.day_bucket, SUM(b.hours) AS hours
  FROM billable b, this_week w
  WHERE b.week_start = w.week_start
  GROUP BY b.user_id, b.day_bucket
),
prior_4wk_hours AS (
  SELECT b.user_id, b.day_bucket, SUM(b.hours) / 4 AS hours
  FROM billable b, this_week w
  WHERE b.week_start BETWEEN DATE_SUB(w.week_start, INTERVAL 4 WEEK)
                         AND DATE_SUB(w.week_start, INTERVAL 1 WEEK)
  GROUP BY b.user_id, b.day_bucket
),
scaffold AS (
  SELECT
    m.user_id,
    m.email AS user_email,
    m.first_name,
    m.last_name,
    m.daily_min_bill,
    db.day_bucket,
    db.day_order
  FROM {fqn("v_usermins")} m
  CROSS JOIN day_buckets db
)
SELECT
  s.user_id, s.user_email, s.first_name, s.last_name,
  s.day_bucket, s.day_order, s.daily_min_bill,
  'Current Week' AS period,
  COALESCE(cw.hours, 0) AS hours,
  SAFE_DIVIDE(COALESCE(cw.hours, 0), s.daily_min_bill) AS pct_of_min
FROM scaffold s
LEFT JOIN current_week_hours cw
  ON cw.user_id = s.user_id AND cw.day_bucket = s.day_bucket
UNION ALL
SELECT
  s.user_id, s.user_email, s.first_name, s.last_name,
  s.day_bucket, s.day_order, s.daily_min_bill,
  'Prior 4-Week Avg' AS period,
  COALESCE(p4.hours, 0) AS hours,
  SAFE_DIVIDE(COALESCE(p4.hours, 0), s.daily_min_bill) AS pct_of_min
FROM scaffold s
LEFT JOIN prior_4wk_hours p4
  ON p4.user_id = s.user_id AND p4.day_bucket = s.day_bucket
"""

    # Trend companion to v_user_daily_billable_hours: unpacks the "Prior
    # 4-Week Avg" single number into its 4 constituent weeks, so a
    # declining/improving pattern is visible instead of averaged away.
    # Same weekday-bucketing rule (Sunday -> Monday, Saturday -> Friday,
    # same Mon-Sun week) and the same user scaffold, but keyed by
    # week_start/weeks_ago instead of a single averaged period.
    views["v_user_daily_billable_hours_trend"] = f"""
CREATE OR REPLACE VIEW {fqn("v_user_daily_billable_hours_trend")} AS
WITH day_buckets AS (
  SELECT 'Monday' AS day_bucket, 1 AS day_order UNION ALL
  SELECT 'Tuesday', 2 UNION ALL
  SELECT 'Wednesday', 3 UNION ALL
  SELECT 'Thursday', 4 UNION ALL
  SELECT 'Friday', 5
),
billable AS (
  SELECT
    tl.user_id,
    tl.hours,
    DATE_TRUNC(tl.log_date, WEEK(MONDAY)) AS week_start,
    CASE EXTRACT(DAYOFWEEK FROM tl.log_date)
      WHEN 1 THEN 'Monday'    -- Sunday folds into Monday (same week)
      WHEN 2 THEN 'Monday'
      WHEN 3 THEN 'Tuesday'
      WHEN 4 THEN 'Wednesday'
      WHEN 5 THEN 'Thursday'
      WHEN 6 THEN 'Friday'
      WHEN 7 THEN 'Friday'    -- Saturday folds into Friday (same week)
    END AS day_bucket
  FROM {timelogs} tl
  WHERE tl.is_billable = TRUE
),
this_week AS (
  SELECT DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY)) AS week_start
),
week_list AS (
  -- weeks_ago: 1 = most recent complete week, 4 = oldest of the 4
  SELECT DATE_SUB(w.week_start, INTERVAL n WEEK) AS week_start, n AS weeks_ago
  FROM this_week w, UNNEST([1, 2, 3, 4]) AS n
),
weekly_hours AS (
  SELECT b.user_id, b.day_bucket, b.week_start, SUM(b.hours) AS hours
  FROM billable b
  WHERE b.week_start IN (SELECT week_start FROM week_list)
  GROUP BY b.user_id, b.day_bucket, b.week_start
),
scaffold AS (
  SELECT
    m.user_id,
    m.email AS user_email,
    m.first_name,
    m.last_name,
    m.daily_min_bill,
    db.day_bucket,
    db.day_order,
    wl.week_start,
    wl.weeks_ago
  FROM {fqn("v_usermins")} m
  CROSS JOIN day_buckets db
  CROSS JOIN week_list wl
)
SELECT
  s.user_id, s.user_email, s.first_name, s.last_name,
  s.day_bucket, s.day_order, s.daily_min_bill,
  s.week_start, s.weeks_ago,
  COALESCE(wh.hours, 0) AS hours,
  SAFE_DIVIDE(COALESCE(wh.hours, 0), s.daily_min_bill) AS pct_of_min
FROM scaffold s
LEFT JOIN weekly_hours wh
  ON wh.user_id = s.user_id
 AND wh.day_bucket = s.day_bucket
 AND wh.week_start = s.week_start
"""

    return views


def create_or_replace_views(bq_client, project_id, dataset):
    """Creates (or updates) all five exception-reporting views. Views are
    just saved queries — this is cheap and safe to re-run any time the
    rule constants above change or the underlying tables' schema changes.
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
