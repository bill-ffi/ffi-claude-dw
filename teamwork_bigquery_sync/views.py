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
    "v_exception_missing_activity_with_time",
    "v_exception_missing_activty_no_time",
    "v_exception_missing_estimate",
    "v_exception_billable_time_internal_projects",
    "v_exception_long_time_entries",
    "v_exception_recurring_compliance",
    "v_usermins",
    "v_user_daily_billable_hours_base",
    "v_user_weekly_billable_hours",
]


def _sql_string_array(values):
    return "[" + ", ".join("'" + v.replace("'", "\\'") + "'" for v in values) + "]"


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
  AND NOT {has_no_activity_time_col}
GROUP BY p.project_id, p.name, p.category_name, p.client_name, proj_owner, t.tasklist_id, t.tasklist_name
HAVING COUNT(*) >= 3
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
  SUM(tl.hours) AS hours
FROM {timelogs} tl
WHERE tl.is_billable = TRUE
GROUP BY user_id, day_bucket, day_order, week_start
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
      DATE_SUB(DATE_TRUNC(CURRENT_DATE(), QUARTER), INTERVAL 3 MONTH),
      WEEK
    ) AS earliest_week_start,
    DATE_TRUNC(CURRENT_DATE(), WEEK) AS current_week_start,
    -- Today's weekday mapped onto the same 1-5 scale as day_order.
    -- Sunday -> 0 (the coming week hasn't started yet -- everything
    -- through Friday is still ahead). Saturday -> 6 (past Friday --
    -- the whole business week just finished).
    CASE EXTRACT(DAYOFWEEK FROM CURRENT_DATE())
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
  SELECT base.user_id, base.day_bucket, base.week_start, base.hours
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
    ) AS non_friday_total
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
  ws.week_start, db.day_bucket, db.day_order, m.daily_min_bill,
  'actual' AS value_type,
  COALESCE(ah.hours, 0) AS hours,
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
  b.current_week_start AS week_start, db.day_bucket, db.day_order, m.daily_min_bill,
  CASE WHEN db.day_order = 5 THEN 'plug' ELSE 'minimum' END AS value_type,
  CASE WHEN db.day_order = 5
    THEN GREATEST(m.daily_min_bill * 5 - cwp.non_friday_total, 0)
    ELSE m.daily_min_bill
  END AS hours,
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

    return views


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
