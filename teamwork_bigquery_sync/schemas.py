from google.cloud import bigquery

PROJECTS_TABLE = "projects"
TASKS_TABLE = "tasks"
TIMELOGS_TABLE = "timelogs"
TIMELOGS_STAGING_TABLE = "timelogs__staging"
USERS_TABLE = "users"

PROJECTS_SCHEMA = [
    bigquery.SchemaField("project_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("description", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("sub_status", "STRING"),
    bigquery.SchemaField("category_id", "INT64"),
    bigquery.SchemaField("category_name", "STRING"),
    bigquery.SchemaField("company_id", "INT64"),
    bigquery.SchemaField("client_name", "STRING"),
    bigquery.SchemaField("owner_id", "INT64"),
    bigquery.SchemaField("is_billable", "BOOL"),
    bigquery.SchemaField("start_date", "DATE"),
    bigquery.SchemaField("end_date", "DATE"),
    # Best-effort: not present on the standard project payload in testing.
    # Populated only if your account's API actually returns a "health" key;
    # otherwise stays NULL. See README "Known gaps".
    bigquery.SchemaField("health", "STRING"),
    # Sourced from the project-budgets endpoint. A project can have more than
    # one budget (e.g. recurring monthly time budgets); we take the one with
    # status ACTIVE and the latest start date as "the current budget".
    #
    # DOLLARS. Teamwork returns `capacity`/`capacityUsed` as integer cents;
    # transform.cents_to_dollars() converts on the way in (confirmed by hand
    # against three projects, 2026-09-19). Do not divide again downstream.
    bigquery.SchemaField("budget_capacity", "FLOAT64"),
    bigquery.SchemaField("budget_used", "FLOAT64"),
    bigquery.SchemaField("budget_left", "FLOAT64"),
    bigquery.SchemaField("tag_ids", "INT64", mode="REPEATED"),
    bigquery.SchemaField("web_link", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("created_by", "INT64"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_by", "INT64"),
    bigquery.SchemaField("completed_at", "TIMESTAMP"),
    bigquery.SchemaField("completed_by", "INT64"),
    bigquery.SchemaField("archived_at", "TIMESTAMP"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]

TASKS_SCHEMA = [
    bigquery.SchemaField("task_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("project_id", "INT64"),
    bigquery.SchemaField("tasklist_id", "INT64"),
    bigquery.SchemaField("tasklist_name", "STRING"),
    bigquery.SchemaField("parent_task_id", "INT64"),
    # Teamwork's native recurring-task mechanism: all occurrences of a
    # recurring task share the same sequence_id. NULL for non-recurring
    # tasks. Confirmed real and in active use (see README).
    bigquery.SchemaField("sequence_id", "INT64"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("description", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("priority", "STRING"),
    bigquery.SchemaField("progress_pct", "INT64"),
    bigquery.SchemaField("estimate_minutes", "INT64"),
    bigquery.SchemaField("start_date", "DATE"),
    bigquery.SchemaField("due_date", "DATE"),
    bigquery.SchemaField("assignee_user_ids", "INT64", mode="REPEATED"),
    bigquery.SchemaField("tag_ids", "INT64", mode="REPEATED"),
    bigquery.SchemaField("is_private", "BOOL"),
    bigquery.SchemaField("is_archived", "BOOL"),
    # The "Activity" preset-list custom field, resolved to its option label.
    # See transform.extract_activity_value() — unverified shape as of when
    # this was added; NULL if the field wasn't found or a task's value
    # couldn't be resolved.
    bigquery.SchemaField("activity", "STRING"),
    bigquery.SchemaField("web_link", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("created_by", "INT64"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_by", "INT64"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]

TIMELOGS_SCHEMA = [
    bigquery.SchemaField("timelog_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("task_id", "INT64"),
    bigquery.SchemaField("project_id", "INT64"),
    bigquery.SchemaField("user_id", "INT64"),
    bigquery.SchemaField("logged_by_user_id", "INT64"),
    # Date portion of `timeLogged` — this is what the monthly replace window
    # filters on, NOT createdAt/updatedAt.
    bigquery.SchemaField("log_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("logged_at", "TIMESTAMP"),
    bigquery.SchemaField("minutes", "INT64"),
    bigquery.SchemaField("hours", "FLOAT64"),
    bigquery.SchemaField("is_billable", "BOOL"),
    bigquery.SchemaField("billable_rate", "FLOAT64"),
    bigquery.SchemaField("cost_rate", "FLOAT64"),
    bigquery.SchemaField("description", "STRING"),
    bigquery.SchemaField("is_locked", "BOOL"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]

USERS_SCHEMA = [
    bigquery.SchemaField("user_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("first_name", "STRING"),
    bigquery.SchemaField("last_name", "STRING"),
    bigquery.SchemaField("full_name", "STRING"),
    bigquery.SchemaField("email", "STRING"),
    bigquery.SchemaField("title", "STRING"),
    bigquery.SchemaField("user_type", "STRING"),
    bigquery.SchemaField("is_admin", "BOOL"),
    bigquery.SchemaField("company_id", "INT64"),
    # Deactivated/deleted users are kept (not filtered out) so historical
    # timelogs/tasks referencing them still resolve to a name — flagged here
    # instead.
    bigquery.SchemaField("is_deleted", "BOOL"),
    bigquery.SchemaField("last_login", "TIMESTAMP"),
    bigquery.SchemaField("timezone", "STRING"),
    # Internal cost rate and billing rate per hour. Included per explicit
    # confirmation — this is compensation-adjacent data; consider restricting
    # BigQuery read access to this table/these columns if that matters later.
    bigquery.SchemaField("user_cost", "FLOAT64"),
    bigquery.SchemaField("user_rate", "FLOAT64"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]


# Columns that should carry a value on every row, per table.
#
# Exists because tasks.web_link and projects.web_link were NULL on 100% of rows
# for months without anything noticing: they read a payload key Teamwork v3
# does not return, a missing dict key yields None, and the sync reported
# success every run. See README "Known gaps". sync.py measures the fill rate of
# these columns on the rows it is about to write and reports them in
# RUN_SUMMARY, warning when one is below MIN_FILL_RATE.
#
# This list is deliberately CONSERVATIVE. Only columns that are structurally
# always present belong here -- an id, a name, a timestamp the API always
# stamps, a value this pipeline constructs itself. Business-optional fields do
# not: category_name (1,885/1,890), activity, estimate_minutes, due_date and
# tasklist_name are all legitimately sparse, and listing them would produce a
# warning on every run, which trains everyone to ignore the warning that
# matters. Resolution-dependent counts that already have their own stat
# (rows_with_category_name, clients_resolved) stay where they are.
#
# If a column here turns out to have legitimate NULLs, REMOVE IT from this list
# as a documented decision rather than lowering MIN_FILL_RATE -- the threshold
# protects every other column too.
ALWAYS_POPULATED_COLUMNS = {
    PROJECTS_TABLE: ("project_id", "name", "status", "web_link", "created_at"),
    TASKS_TABLE: ("task_id", "project_id", "name", "status", "web_link", "created_at"),
    USERS_TABLE: ("user_id", "full_name"),
    TIMELOGS_TABLE: ("timelog_id", "user_id", "project_id", "log_date", "minutes"),
}

# Fill rate below which a column is reported as underfilled. 1.0 because the
# columns above are chosen to be always-present; anything less is a finding.
MIN_FILL_RATE = 1.0
