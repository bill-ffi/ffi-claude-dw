import logging

from google.cloud import bigquery

from schemas import (
    PROJECTS_SCHEMA,
    PROJECTS_TABLE,
    TASKS_SCHEMA,
    TASKS_TABLE,
    TIMELOGS_SCHEMA,
    TIMELOGS_STAGING_TABLE,
    TIMELOGS_TABLE,
    USERS_SCHEMA,
    USERS_TABLE,
)

logger = logging.getLogger(__name__)

# A full-replace load that would shrink a table below this fraction of its
# current size is refused rather than applied. projects/tasks/users are
# truncate-and-reload, so a bad pull doesn't corrupt them — it erases them,
# and every downstream view goes empty with the run still reporting success.
# Deliberate scope reductions (e.g. moving ARCHIVED_PROJECT_TASKS_CUTOFF
# forward) legitimately shrink a table: pass allow_shrink=True, or run with
# `--allow-shrink`, for those.
MIN_REPLACE_ROWS_RATIO = 0.5


class EmptyLoadRefused(RuntimeError):
    def __init__(self, table_name):
        super().__init__(
            f"Refusing to replace {table_name} with 0 rows — this would empty the "
            "table and every view built on it, while the run still reported "
            "success. An empty pull is far more likely to be an API blip than a "
            "real result. Investigate the source data; there is no flag to "
            "override this one."
        )


class SuspiciousShrinkRefused(RuntimeError):
    def __init__(self, table_name, new_count, existing_count):
        pct = (1 - new_count / existing_count) * 100
        super().__init__(
            f"Refusing to replace {table_name}: {existing_count:,} rows -> "
            f"{new_count:,} rows is a {pct:.0f}% drop, past the "
            f"{(1 - MIN_REPLACE_ROWS_RATIO) * 100:.0f}% guard. If this shrink is "
            "intended (e.g. a deliberate scope change), re-run with "
            "--allow-shrink; otherwise check the Teamwork pull before writing."
        )


def _existing_row_count(client, table_ref):
    """Current row count from table metadata — free, no query scanned.
    Returns None if the table doesn't exist yet.
    """
    from google.api_core.exceptions import NotFound

    try:
        return client.get_table(table_ref).num_rows
    except NotFound:
        return None


def get_client(gcp_project_id):
    return bigquery.Client(project=gcp_project_id)


def ensure_dataset(client, project_id, dataset_id, location):
    dataset_ref = bigquery.DatasetReference(project_id, dataset_id)
    dataset = bigquery.Dataset(dataset_ref)
    dataset.location = location
    client.create_dataset(dataset, exists_ok=True)
    logger.info("Dataset ready: %s.%s (%s)", project_id, dataset_id, location)
    return dataset_ref


def ensure_table(client, dataset_ref, table_name, schema):
    table_ref = dataset_ref.table(table_name)
    table = bigquery.Table(table_ref, schema=schema)
    client.create_table(table, exists_ok=True)
    return table_ref


def ensure_all_tables(client, dataset_ref):
    ensure_table(client, dataset_ref, PROJECTS_TABLE, PROJECTS_SCHEMA)
    ensure_table(client, dataset_ref, TASKS_TABLE, TASKS_SCHEMA)
    ensure_table(client, dataset_ref, TIMELOGS_TABLE, TIMELOGS_SCHEMA)
    ensure_table(client, dataset_ref, TIMELOGS_STAGING_TABLE, TIMELOGS_SCHEMA)
    ensure_table(client, dataset_ref, USERS_TABLE, USERS_SCHEMA)


def truncate_and_load(client, dataset_ref, table_name, schema, rows, allow_shrink=False):
    """Full replace: atomically swaps the table contents for `rows`.

    Refuses two cases rather than writing them, because WRITE_TRUNCATE is
    unrecoverable — there is no prior version to fall back to:
      - `rows` is empty. Nothing legitimately empties these tables.
      - `rows` is less than MIN_REPLACE_ROWS_RATIO of what the table already
        holds, unless `allow_shrink`. A partial pull that silently halves
        the tasks table is the failure this is here to catch.
    """
    table_ref = dataset_ref.table(table_name)

    if not rows:
        raise EmptyLoadRefused(table_name)

    existing_count = _existing_row_count(client, table_ref)
    if existing_count:
        ratio = len(rows) / existing_count
        if ratio < MIN_REPLACE_ROWS_RATIO and not allow_shrink:
            raise SuspiciousShrinkRefused(table_name, len(rows), existing_count)
        if ratio < MIN_REPLACE_ROWS_RATIO:
            logger.warning(
                "%s shrinking %d -> %d rows; allowed by --allow-shrink",
                table_name,
                existing_count,
                len(rows),
            )

    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    job = client.load_table_from_json(rows, table_ref, job_config=job_config)
    job.result()
    if job.errors:
        raise RuntimeError(f"Load job errors for {table_name}: {job.errors}")
    logger.info(
        "Truncate+load complete for %s: %d rows (was %s)",
        table_name,
        len(rows),
        f"{existing_count:,}" if existing_count is not None else "a new table",
    )
    return len(rows)


def _count_timelogs_in_window(client, project_id, dataset_id, start_date, end_date_exclusive):
    """Rows currently stored for a timelogs window."""
    sql = (
        f"SELECT COUNT(*) AS total_rows FROM "
        f"`{project_id}.{dataset_id}.{TIMELOGS_TABLE}` "
        "WHERE log_date >= @window_start AND log_date < @window_end"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("window_start", "DATE", start_date),
            bigquery.ScalarQueryParameter("window_end", "DATE", end_date_exclusive),
        ]
    )
    return list(client.query(sql, job_config=job_config).result())[0][0]


def replace_timelogs_window(
    client, project_id, dataset_id, rows, window_start_date, window_end_date_exclusive
):
    """Deletes existing timelogs rows whose log_date falls in
    [window_start_date, window_end_date_exclusive), then inserts the
    freshly-pulled `rows` for that window — as a single atomic BigQuery
    multi-statement script, so a mid-run failure can't leave the table with
    the window deleted but not reinserted. Rows outside the window are left
    untouched.

    The window is not necessarily one calendar month: the normal sync
    replaces a rolling multi-month span (see sync.timelog_window) so that
    timelogs entered retroactively against an already-closed month are
    picked up, and --backfill-months replaces exactly one month at a time.
    Named for the window rather than the month for that reason.
    """
    dataset_ref = bigquery.DatasetReference(project_id, dataset_id)
    staging_table_ref = dataset_ref.table(TIMELOGS_STAGING_TABLE)
    timelogs_table_ref = dataset_ref.table(TIMELOGS_TABLE)

    # An empty pull for a window that currently holds rows would delete them
    # and insert nothing. Unlike the full-replace tables an empty window can
    # be legitimate (backfilling a month with no activity), so this only
    # refuses when there is something to lose.
    if not rows:
        existing_in_window = _count_timelogs_in_window(
            client, project_id, dataset_id, window_start_date, window_end_date_exclusive
        )
        if existing_in_window:
            raise RuntimeError(
                f"Refusing to replace timelogs [{window_start_date}, "
                f"{window_end_date_exclusive}) with 0 rows: the window currently "
                f"holds {existing_in_window:,}. An empty pull over a populated "
                "window is far more likely to be an API blip than a real result."
            )
        logger.warning(
            "No timelogs returned for [%s, %s), and the window is already empty "
            "— nothing to do.",
            window_start_date,
            window_end_date_exclusive,
        )
        return 0

    job_config = bigquery.LoadJobConfig(
        schema=TIMELOGS_SCHEMA,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    load_job = client.load_table_from_json(rows, staging_table_ref, job_config=job_config)
    load_job.result()
    if load_job.errors:
        raise RuntimeError(f"Staging load errors for timelogs: {load_job.errors}")
    logger.info(
        "Staged %d timelogs rows for the [%s, %s) replace",
        len(rows),
        window_start_date,
        window_end_date_exclusive,
    )

    timelogs_fqn = f"`{project_id}.{dataset_id}.{TIMELOGS_TABLE}`"
    staging_fqn = f"`{project_id}.{dataset_id}.{TIMELOGS_STAGING_TABLE}`"
    columns = ", ".join(field.name for field in TIMELOGS_SCHEMA)

    script = f"""
    BEGIN TRANSACTION;
    DELETE FROM {timelogs_fqn}
    WHERE log_date >= @window_start AND log_date < @window_end;
    INSERT INTO {timelogs_fqn} ({columns})
    SELECT {columns} FROM {staging_fqn};
    COMMIT TRANSACTION;
    """
    query_job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("window_start", "DATE", window_start_date),
            bigquery.ScalarQueryParameter("window_end", "DATE", window_end_date_exclusive),
        ]
    )
    query_job = client.query(script, job_config=query_job_config)
    query_job.result()
    logger.info(
        "Replaced timelogs for [%s, %s): %d rows inserted",
        window_start_date,
        window_end_date_exclusive,
        len(rows),
    )
    return len(rows)
