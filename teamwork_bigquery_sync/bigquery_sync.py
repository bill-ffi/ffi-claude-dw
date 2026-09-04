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


def truncate_and_load(client, dataset_ref, table_name, schema, rows):
    """Full replace: atomically swaps the table contents for `rows`."""
    table_ref = dataset_ref.table(table_name)
    if not rows:
        logger.warning("No rows to load for %s — table will be emptied.", table_name)
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    job = client.load_table_from_json(rows, table_ref, job_config=job_config)
    job.result()
    if job.errors:
        raise RuntimeError(f"Load job errors for {table_name}: {job.errors}")
    logger.info("Truncate+load complete for %s: %d rows", table_name, len(rows))
    return len(rows)


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
