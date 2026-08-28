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
  - Rule 3 ("internal projects") is defined as the inverse: any project
    whose category is NOT in MONITORED_CATEGORIES (including projects with
    no category at all). There's no explicit "Internal" category value to
    key off of, so this is a judgment call, not a confirmed mapping.
  - Rule 4 (long time entries) is intentionally NOT scoped to monitored
    categories — it checks every timelog site-wide, since excess time on an
    internal task is arguably just as worth a look as on a billable one.

Update the constants below (not the SQL) if any of these lists change.
"""

import logging

logger = logging.getLogger(__name__)

# The project categories that make up "all of the billable projects we are
# monitoring" per your instruction. Rules 1, 2, and 5 are scoped to these;
# rule 3 is defined as everything NOT in this list.
#
# "Non-Monthly & Payroll" was originally treated as one category here, but
# turned out to be two distinct real Teamwork categories ("Non-Monthly" and
# "Payroll") — confirmed via your direct edit of the missing_estimate view
# in BigQuery. The combined string never matched any real project, so
# rules 1, 2, and 5 were silently skipping both categories entirely until
# this was split out.
MONITORED_CATEGORIES = ["Books Maintenance", "Monthly Close", "Non-Monthly", "Payroll"]

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

VIEW_NAMES = [
    "v_exception_missing_activity",
    "v_exception_missing_estimate",
    "v_exception_billable_time_internal_projects",
    "v_exception_long_time_entries",
    "v_exception_recurring_compliance",
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
    exempt_tasklists = _sql_string_array(ESTIMATE_EXEMPT_TASKLISTS)

    views = {}

    views["v_exception_missing_activity"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_missing_activity")} AS
SELECT
  p.project_id,
  p.name AS project_name,
  p.category_name,
  p.client_name,
  t.tasklist_id,
  t.tasklist_name,
  t.task_id,
  t.name AS task_name,
  t.status,
  assignee.user_id AS assignee_user_id,
  assignee.full_name AS assignee_name,
  t.web_link,
  t.synced_at
FROM {tasks} t
JOIN {projects} p ON p.project_id = t.project_id
LEFT JOIN UNNEST(t.assignee_user_ids) AS assignee_user_id_raw
LEFT JOIN {users} assignee ON assignee.user_id = assignee_user_id_raw
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
  t.tasklist_id,
  t.tasklist_name,
  t.task_id,
  t.name AS task_name,
  t.status,
  t.estimate_minutes,
  assignee.user_id AS assignee_user_id,
  assignee.full_name AS assignee_name,
  t.due_date,
  t.start_date,
  t.created_at,
  t.updated_at,
  t.synced_at
FROM {tasks} t
JOIN {projects} p ON p.project_id = t.project_id
LEFT JOIN UNNEST(t.assignee_user_ids) AS assignee_user_id_raw
LEFT JOIN {users} assignee ON assignee.user_id = assignee_user_id_raw
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
  tk.tasklist_id,
  tk.tasklist_name,
  tl.task_id,
  tk.name AS task_name,
  tl.timelog_id,
  tl.user_id,
  u.full_name AS user_name,
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
WHERE tl.is_billable = TRUE
  AND (p.category_name IS NULL OR p.category_name NOT IN UNNEST({monitored}))
"""

    views["v_exception_long_time_entries"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_long_time_entries")} AS
SELECT
  p.project_id,
  p.name AS project_name,
  p.category_name,
  p.client_name,
  tk.tasklist_id,
  tk.tasklist_name,
  tl.task_id,
  tk.name AS task_name,
  tl.timelog_id,
  tl.user_id,
  u.full_name AS user_name,
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
WHERE tl.hours > {LONG_ENTRY_THRESHOLD_HOURS}
"""

    views["v_exception_recurring_compliance"] = f"""
CREATE OR REPLACE VIEW {fqn("v_exception_recurring_compliance")} AS
SELECT
  p.project_id,
  p.name AS project_name,
  p.category_name,
  p.client_name,
  t.tasklist_id,
  t.tasklist_name,
  t.task_id,
  t.name AS task_name,
  t.status,
  t.sequence_id,
  t.parent_task_id,
  assignee.user_id AS assignee_user_id,
  assignee.full_name AS assignee_name,
  t.web_link,
  t.synced_at
FROM {tasks} t
JOIN {projects} p ON p.project_id = t.project_id
LEFT JOIN UNNEST(t.assignee_user_ids) AS assignee_user_id_raw
LEFT JOIN {users} assignee ON assignee.user_id = assignee_user_id_raw
WHERE p.category_name = '{RECURRING_REQUIRED_CATEGORY}'
  AND t.parent_task_id IS NULL
  AND t.sequence_id IS NULL
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
