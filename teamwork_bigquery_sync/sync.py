#!/usr/bin/env python3
"""Entrypoint: pulls Projects, Tasks, and Timelogs from Teamwork and syncs
them into BigQuery. Meant to be triggered by an external scheduler (cron,
GitHub Actions, or Cloud Scheduler + Cloud Run) — see README.md.

Usage:
    python sync.py             # full sync
    python sync.py --dry-run   # connectivity + payload-shape check only,
                                # does not touch BigQuery or write anything
"""

import argparse
import json
import logging
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import bigquery_sync
import schemas
import transform
from config import load_config
from teamwork_client import (
    PROJECT_BUDGETS_PATH,
    PROJECTS_PATH,
    TASKS_PATH,
    TIMELOGS_PATH,
    TeamworkClient,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("sync")


def month_window(tz_name):
    """Returns (month_start, month_end_exclusive) as date objects for
    "this calendar month" in the given timezone.
    """
    now = datetime.now(ZoneInfo(tz_name))
    month_start = date(now.year, now.month, 1)
    if now.month == 12:
        month_end_exclusive = date(now.year + 1, 1, 1)
    else:
        month_end_exclusive = date(now.year, now.month + 1, 1)
    return month_start, month_end_exclusive


def run_dry_run(client):
    print("Dry run — probing Teamwork endpoints (no BigQuery writes).\n")
    checks = [
        ("projects", PROJECTS_PATH, {"page[size]": 1, "page[offset]": 0}, "projects"),
        ("tasks", TASKS_PATH, {"page[size]": 1, "page[offset]": 0}, "tasks"),
        (
            "timelogs",
            TIMELOGS_PATH,
            {
                "page[size]": 1,
                "page[offset]": 0,
                "startDate": date.today().isoformat(),
                "endDate": date.today().isoformat(),
            },
            "timelogs",
        ),
        (
            "project budgets",
            PROJECT_BUDGETS_PATH,
            {"page[size]": 1, "page[offset]": 0},
            "budgets",
        ),
    ]
    any_failed = False
    for label, path, params, item_key in checks:
        try:
            payload = client._get(path, params)
            items = payload.get(item_key, [])
            sample_keys = sorted(items[0].keys()) if items else []
            print(f"[OK] {label} ({path}) — {len(items)} item(s) returned")
            if sample_keys:
                print(f"     sample fields: {', '.join(sample_keys)}")
            if label == "projects" and items and "health" not in sample_keys:
                print("     note: 'health' key not present on this payload (expected — see README)")
        except Exception as exc:
            any_failed = True
            print(f"[FAIL] {label} ({path}) — {exc}")
    print()
    if any_failed:
        print("One or more endpoints failed. Fix TEAMWORK_BASE_URL/API key or the")
        print("*_PATH constants in teamwork_client.py before scheduling this.")
        return 1
    print("All endpoints reachable.")
    return 0


def sync_projects(tw_client, bq_client, dataset_ref):
    raw_projects, included = tw_client.list_projects()
    raw_budgets = tw_client.list_project_budgets()
    category_names = transform.build_category_name_map(included)
    budgets_by_project = transform.build_budgets_by_project(raw_budgets)

    rows = []
    for raw in raw_projects:
        row = transform.normalize_project(raw, category_names, budgets_by_project)
        if row is not None:
            rows.append(row)

    written = bigquery_sync.truncate_and_load(
        bq_client, dataset_ref, schemas.PROJECTS_TABLE, schemas.PROJECTS_SCHEMA, rows
    )
    valid_project_ids = {row["project_id"] for row in rows}
    return {
        "status": "success",
        "rows_pulled": len(raw_projects),
        "rows_written": written,
    }, valid_project_ids


def sync_tasks(tw_client, bq_client, dataset_ref, valid_project_ids):
    if valid_project_ids is None:
        logger.warning(
            "Skipping tasks sync: projects sync did not complete successfully "
            "this run, so there is no reliable set of in-scope project ids."
        )
        return {"status": "skipped", "reason": "projects sync failed"}

    raw_tasks = tw_client.list_tasks()
    rows = []
    for raw in raw_tasks:
        row = transform.normalize_task(raw, valid_project_ids)
        if row is not None:
            rows.append(row)

    written = bigquery_sync.truncate_and_load(
        bq_client, dataset_ref, schemas.TASKS_TABLE, schemas.TASKS_SCHEMA, rows
    )
    return {
        "status": "success",
        "rows_pulled": len(raw_tasks),
        "rows_written": written,
    }


def sync_timelogs(tw_client, bq_client, gcp_project_id, dataset_id, tz_name):
    month_start, month_end_exclusive = month_window(tz_name)
    last_day_inclusive = month_end_exclusive - timedelta(days=1)

    raw_timelogs = tw_client.list_timelogs(
        month_start.isoformat(), last_day_inclusive.isoformat()
    )
    rows = []
    for raw in raw_timelogs:
        row = transform.normalize_timelog(raw)
        if row is not None:
            rows.append(row)

    written = bigquery_sync.replace_current_month_timelogs(
        bq_client,
        gcp_project_id,
        dataset_id,
        rows,
        month_start,
        month_end_exclusive,
    )
    return {
        "status": "success",
        "rows_pulled": len(raw_timelogs),
        "rows_written": written,
        "window": [month_start.isoformat(), month_end_exclusive.isoformat()],
    }


def run_full_sync(cfg):
    tw_client = TeamworkClient(cfg.teamwork_base_url, cfg.teamwork_api_key)
    bq_client = bigquery_sync.get_client(cfg.gcp_project_id)
    dataset_ref = bigquery_sync.ensure_dataset(
        bq_client, cfg.gcp_project_id, cfg.bq_dataset, cfg.bq_location
    )
    bigquery_sync.ensure_all_tables(bq_client, dataset_ref)

    stages = {}
    started_at = datetime.now(timezone.utc)

    valid_project_ids = None
    try:
        stages["projects"], valid_project_ids = sync_projects(
            tw_client, bq_client, dataset_ref
        )
    except Exception:
        logger.error("Projects sync failed:\n%s", traceback.format_exc())
        stages["projects"] = {"status": "failed", "error": traceback.format_exc()}

    try:
        stages["tasks"] = sync_tasks(tw_client, bq_client, dataset_ref, valid_project_ids)
    except Exception:
        logger.error("Tasks sync failed:\n%s", traceback.format_exc())
        stages["tasks"] = {"status": "failed", "error": traceback.format_exc()}

    try:
        stages["timelogs"] = sync_timelogs(
            tw_client, bq_client, cfg.gcp_project_id, cfg.bq_dataset, cfg.sync_timezone
        )
    except Exception:
        logger.error("Timelogs sync failed:\n%s", traceback.format_exc())
        stages["timelogs"] = {"status": "failed", "error": traceback.format_exc()}

    finished_at = datetime.now(timezone.utc)
    summary = {
        "run_started_at": started_at.isoformat(),
        "run_finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "gcp_project_id": cfg.gcp_project_id,
        "bq_dataset": cfg.bq_dataset,
        "stages": stages,
    }
    logger.info("RUN_SUMMARY %s", json.dumps(summary, default=str))

    all_ok = all(stage.get("status") == "success" for stage in stages.values())
    return 0 if all_ok else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe Teamwork endpoints and print payload shapes; no BigQuery writes.",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.dry_run:
        tw_client = TeamworkClient(cfg.teamwork_base_url, cfg.teamwork_api_key)
        sys.exit(run_dry_run(tw_client))

    sys.exit(run_full_sync(cfg))


if __name__ == "__main__":
    main()
