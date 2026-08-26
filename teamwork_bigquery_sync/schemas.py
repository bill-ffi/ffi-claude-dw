from google.cloud import bigquery

PROJECTS_TABLE = "projects"
TASKS_TABLE = "tasks"
TIMELOGS_TABLE = "timelogs"
TIMELOGS_STAGING_TABLE = "timelogs__staging"

PROJECTS_SCHEMA = [
    bigquery.SchemaField("project_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("description", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("sub_status", "STRING"),
    bigquery.SchemaField("category_id", "INT64"),
    bigquery.SchemaField("category_name", "STRING"),
    bigquery.SchemaField("company_id", "INT64"),
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
