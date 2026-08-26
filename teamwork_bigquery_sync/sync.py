#!/usr/bin/env python3
"""Entrypoint: pulls Projects, Tasks, and Timelogs from Teamwork and syncs
them into BigQuery. Meant to be triggered by an external scheduler (cron,
GitHub Actions, or Cloud Scheduler + Cloud Run) — see README.md.

Usage:
    python sync.py             # full sync (projects, tasks, current-month timelogs)
    python sync.py --dry-run   # connectivity + payload-shape check only,
                                # does not touch BigQuery or write anything
    python sync.py --backfill-months 2026-01,2026-02,2026-03
                                # re-pulls and replaces ONLY the timelogs for
                                # each listed month (YYYY-MM), leaving all
                                # other months untouched. Does not touch
                                # projects/tasks. Use this to load history
                                # that predates when this script started
                                # running, or to fix a month you know is
                                # wrong. Safe to re-run for the same month.
"""

import argparse
import json
import logging
import re
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


MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def month_bounds(year, month):
    """Returns (month_start, month_end_exclusive) as date objects for the
    given calendar month.
    """
    month_start = date(year, month, 1)
    if month == 12:
        month_end_exclusive = date(year + 1, 1, 1)
    else:
        month_end_exclusive = date(year, month + 1, 1)
    return month_start, month_end_exclusive


def month_window(tz_name):
    """Returns (month_start, month_end_exclusive) for "this calendar month"
    in the given timezone.
    """
    now = datetime.now(ZoneInfo(tz_name))
    return month_bounds(now.year, now.month)


def parse_backfill_months(raw_value):
    """Parses a comma-separated "YYYY-MM,YYYY-MM,..." string into a sorted,
    de-duplicated list of (year, month) tuples. Raises ValueError on any
    malformed entry, naming the bad one, rather than silently skipping it.
    """
    months = []
    for token in raw_value.split(","):
        token = token.strip()
        if not token:
            continue
        if not MONTH_RE.match(token):
            raise ValueError(f"Invalid month '{token}' — expected format YYYY-MM")
        year_str, month_str = token.split("-")
        months.append((int(year_str), int(month_str)))
    if not months:
        raise ValueError("--backfill-months given but no months parsed")
    return sorted(set(months))


def run_dry_run(client):
    print("Dry run — probing Teamwork endpoints (no BigQuery writes).\n")
    checks = [
        ("projects", PROJECTS_PATH, {"page": 1, "pageSize": 1}, "projects"),
        ("tasks", TASKS_PATH, {"page": 1, "pageSize": 1}, "tasks"),
        (
            "timelogs",
            TIMELOGS_PATH,
            {
                "page": 1,
                "pageSize": 1,
                "startDate": date.today().isoformat(),
                "endDate": date.today().isoformat(),
            },
            "timelogs",
        ),
        (
            "project budgets",
            PROJECT_BUDGETS_PATH,
            {"page": 1, "pageSize": 1},
            "budgets",
        ),
    ]
    any_failed = False
    for label, path, params, item_key in checks:
        try:
            payload = client._get(path, params)
            items = payload.get(item_key, [])
            sample_keys = sorted(items[0].keys()) if items else []
            requested_size = params.get("pageSize")
            actual_size = len(items)
            print(f"[OK] {label} ({path}) — {actual_size} item(s) returned")
            if requested_size is not None and actual_size > requested_size:
                print(
                    f"     WARNING: asked for pageSize={requested_size} but got "
                    f"{actual_size} back — the API may be ignoring the pageSize "
                    "param. If this is unexpected, treat pagination as unverified."
                )
            if sample_keys:
                print(f"     sample fields: {', '.join(sample_keys)}")
            if label == "projects" and items and "health" not in sample_keys:
                print("     note: 'health' key not present on this payload (expected — see README)")
        except Exception as exc:
            any_failed = True
            print(f"[FAIL] {label} ({path}) — {exc}")

    # Pagination sanity check: fetch page 1 and page 2 (small pageSize) for
    # projects, and confirm page 2 actually returns *different* rows. This
    # is what would have caught the page[offset]-vs-page bug before it ever
    # ran for real and got rate-limited.
    try:
        page1 = client._get(PROJECTS_PATH, {"page": 1, "pageSize": 3}).get("projects", [])
        page2 = client._get(PROJECTS_PATH, {"page": 2, "pageSize": 3}).get("projects", [])
        page1_ids = {p.get("id") for p in page1}
        page2_ids = {p.get("id") for p in page2}
        if page2 and page1_ids & page2_ids:
            any_failed = True
            print(
                f"\n[FAIL] pagination sanity check — page 1 and page 2 of "
                f"projects returned overlapping ids ({page1_ids & page2_ids}). "
                "Pagination params are likely wrong; fix before running for real."
            )
        elif page2:
            print("\n[OK] pagination sanity check — page 1 and page 2 returned distinct rows")
        else:
            print("\n[OK] pagination sanity check — fewer than 2 pages of projects exist, nothing to compare")
    except Exception as exc:
        any_failed = True
        print(f"\n[FAIL] pagination sanity check — {exc}")

    print()
    if any_failed:
        print("One or more checks failed. Fix TEAMWORK_BASE_URL/API key or the")
        print("*_PATH constants / pagination params in teamwork_client.py before scheduling this.")
        return 1
    print("All endpoints reachable and pagination looks correct.")
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


def sync_timelogs_for_month(tw_client, bq_client, gcp_project_id, dataset_id, month_start, month_end_exclusive):
    """Deletes+reinserts the timelogs rows for exactly [month_start,
    month_end_exclusive). Used both for "this calendar month" (the normal
    run) and for backfilling an arbitrary past month.
    """
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
        month_start, month_end_exclusive = month_window(cfg.sync_timezone)
        stages["timelogs"] = sync_timelogs_for_month(
            tw_client, bq_client, cfg.gcp_project_id, cfg.bq_dataset,
            month_start, month_end_exclusive,
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


def run_backfill(cfg, months):
    """Re-pulls and replaces timelogs for each (year, month) in `months`,
    one at a time. Projects/tasks are untouched — they're always a full
    replace on the normal run, so there's nothing to "backfill" there.
    """
    tw_client = TeamworkClient(cfg.teamwork_base_url, cfg.teamwork_api_key)
    bq_client = bigquery_sync.get_client(cfg.gcp_project_id)
    dataset_ref = bigquery_sync.ensure_dataset(
        bq_client, cfg.gcp_project_id, cfg.bq_dataset, cfg.bq_location
    )
    bigquery_sync.ensure_all_tables(bq_client, dataset_ref)

    started_at = datetime.now(timezone.utc)
    stages = {}
    for year, month in months:
        label = f"{year:04d}-{month:02d}"
        month_start, month_end_exclusive = month_bounds(year, month)
        print(f"Backfilling timelogs for {label} ({month_start} to {month_end_exclusive}, exclusive)...")
        try:
            stages[label] = sync_timelogs_for_month(
                tw_client, bq_client, cfg.gcp_project_id, cfg.bq_dataset,
                month_start, month_end_exclusive,
            )
            print(f"  done: {stages[label]['rows_written']} rows written")
        except Exception:
            logger.error("Backfill for %s failed:\n%s", label, traceback.format_exc())
            stages[label] = {"status": "failed", "error": traceback.format_exc()}
            print(f"  FAILED — see log above")

    finished_at = datetime.now(timezone.utc)
    summary = {
        "mode": "backfill",
        "run_started_at": started_at.isoformat(),
        "run_finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "gcp_project_id": cfg.gcp_project_id,
        "bq_dataset": cfg.bq_dataset,
        "months": stages,
    }
    logger.info("RUN_SUMMARY %s", json.dumps(summary, default=str))

    all_ok = all(stage.get("status") == "success" for stage in stages.values())
    return 0 if all_ok else 1


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe Teamwork endpoints and print payload shapes; no BigQuery writes.",
    )
    parser.add_argument(
        "--backfill-months",
        metavar="YYYY-MM[,YYYY-MM,...]",
        help="Re-pull and replace ONLY the timelogs for these calendar months. "
             "Does not touch projects/tasks or any other month.",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.backfill_months:
        try:
            months = parse_backfill_months(args.backfill_months)
        except ValueError as exc:
            print(f"error: {exc}")
            sys.exit(2)
        sys.exit(run_backfill(cfg, months))

    if args.dry_run:
        tw_client = TeamworkClient(cfg.teamwork_base_url, cfg.teamwork_api_key)
        sys.exit(run_dry_run(tw_client))

    sys.exit(run_full_sync(cfg))


if __name__ == "__main__":
    main()
