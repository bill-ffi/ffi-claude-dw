#!/usr/bin/env python3
"""Entrypoint: pulls Projects, Tasks, Users, and Timelogs from Teamwork and
syncs them into BigQuery. Meant to be triggered by an external scheduler
(cron, GitHub Actions, or Cloud Scheduler + Cloud Run) — see README.md.

Usage:
    python sync.py             # full sync (projects, tasks, users, current-month timelogs)
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
    CUSTOM_FIELDS_PATH,
    PROJECT_BUDGETS_PATH,
    PROJECTS_PATH,
    TASKS_PATH,
    TIMELOGS_PATH,
    USERS_PATH,
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
        ("users", USERS_PATH, {"page": 1, "pageSize": 1}, "people"),
        ("custom fields", CUSTOM_FIELDS_PATH, {"page": 1, "pageSize": 50}, "customfields"),
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

    # Activity custom field diagnostic — confirmed live 2026-08-27: field is
    # named "ACTIVITY" (id 98742), site-wide (not project-scoped), items
    # under "customfields"/"customfieldTasks" (see teamwork_client.py). Kept
    # as a standing diagnostic (not just a one-time check) since it's cheap
    # and catches the field ever being renamed/removed in Teamwork.
    print("\n--- 'Activity' custom field diagnostic ---")
    try:
        custom_fields = client._get(CUSTOM_FIELDS_PATH, {"page": 1, "pageSize": 250}).get(
            "customfields", []
        )
        activity_field = transform.find_custom_field_by_name(custom_fields, ACTIVITY_FIELD_NAME)
        if activity_field is None:
            any_failed = True
            names = [cf.get("name") for cf in custom_fields]
            print(f"[FAIL] No custom field named '{ACTIVITY_FIELD_NAME}' found. Available: {names}")
        else:
            print(f"[OK] Found '{ACTIVITY_FIELD_NAME}' field, id={activity_field.get('id')}")
            print(f"     raw field definition: {json.dumps(activity_field, default=str)}")
            option_labels = transform.build_option_label_map(activity_field)
            print(f"     parsed options: {option_labels}")

            # Bulk sideload check (the fast, cheap path — confirmed real via
            # Teamwork's own public examples repo, but not yet verified live
            # on this account's tasks.json response).
            bulk_payload = client._get(
                TASKS_PATH,
                {"page": 1, "pageSize": 20, "includeCompletedTasks": "true", "includeCustomFields": "true"},
            )
            bulk_included = bulk_payload.get("included") or {}
            print(f"     bulk pull 'included' top-level keys: {list(bulk_included.keys())}")
            activity_map = transform.extract_activity_map_from_sideload(
                bulk_included, activity_field["id"], option_labels
            )
            if activity_map is None:
                print(
                    "     [FAIL] included.customfieldTasks is missing from a "
                    "tasks.json?includeCustomFields=true response — the bulk path won't "
                    "work on this account; the code will automatically fall back to the "
                    "slower per-task method (still verified working below)."
                )
            else:
                print(f"     [OK] bulk sideload present, {len(activity_map)} activity value(s) "
                      f"resolved from this 20-task sample: {activity_map}")

            sample_tasks = client._get(TASKS_PATH, {"page": 1, "pageSize": 1}).get("tasks", [])
            if sample_tasks:
                sample_task_id = sample_tasks[0]["id"]
                raw_values = client.get_task_custom_field_values(sample_task_id)
                print(f"     [per-task fallback check] sample task {sample_task_id} raw "
                      f"custom field values: {json.dumps(raw_values, default=str)}")
                resolved = transform.extract_activity_value(
                    raw_values, activity_field["id"], option_labels
                )
                print(f"     parsed activity value for that task (per-task method): {resolved!r}")
                if raw_values and resolved is None:
                    print(
                        "     note: this task has other custom field values set but none for "
                        "Activity specifically — that's likely just this task not having it "
                        "set, not a parsing bug. Only worry if a task you KNOW has an Activity "
                        "value set in the Teamwork UI resolves to None here."
                    )
            else:
                print("     no tasks available to sample a custom field value from")
    except Exception as exc:
        any_failed = True
        print(f"[FAIL] custom field diagnostic — {exc}")

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

    # Diagnostic: this table has a real bug report (category_name always
    # NULL despite category_id being populated) that offline testing and a
    # live API sample couldn't reproduce — so log hard numbers from the
    # actual production call itself rather than guessing further.
    included_types = list(included.keys())
    logger.info(
        "Category resolution: included types=%s, categories resolved=%d, sample=%s",
        included_types,
        len(category_names),
        dict(list(category_names.items())[:3]),
    )
    projects_with_category_id = sum(1 for raw in raw_projects if raw.get("categoryId") or raw.get("category"))
    logger.info(
        "%d/%d raw projects have a category ref (categoryId or category set)",
        projects_with_category_id,
        len(raw_projects),
    )

    rows = []
    for raw in raw_projects:
        row = transform.normalize_project(raw, category_names, budgets_by_project)
        if row is not None:
            rows.append(row)

    rows_with_category_name = sum(1 for row in rows if row.get("category_name") is not None)
    logger.info(
        "%d/%d final rows have a non-null category_name",
        rows_with_category_name,
        len(rows),
    )

    written = bigquery_sync.truncate_and_load(
        bq_client, dataset_ref, schemas.PROJECTS_TABLE, schemas.PROJECTS_SCHEMA, rows
    )
    valid_project_ids = {row["project_id"] for row in rows}
    return {
        "status": "success",
        "rows_pulled": len(raw_projects),
        "rows_written": written,
        "categories_resolved": len(category_names),
        "rows_with_category_name": rows_with_category_name,
    }, valid_project_ids


ACTIVITY_FIELD_NAME = "Activity"


def enrich_tasks_with_activity(tw_client, rows, tasks_included):
    """Mutates each row's "activity" in place. Non-fatal on any failure —
    the "Activity" field not being found, or values not resolving, just
    leaves those rows' activity as None rather than failing the whole
    tasks sync. Returns a small dict of stats for the run summary / dry-run
    visibility.

    Tries the bulk sideload first (included["customfieldTasks"], from
    tasks.json?includeCustomFields=true — confirmed real via Teamwork's own
    public API-Request-Examples repo, costs zero extra API calls since it
    rides along on the tasks pull already happening). Falls back to the
    slower one-request-per-task method only if that sideload is entirely
    missing from the response.
    """
    custom_fields = tw_client.list_custom_fields()
    activity_field = transform.find_custom_field_by_name(custom_fields, ACTIVITY_FIELD_NAME)
    if activity_field is None:
        logger.warning(
            "'%s' custom field not found (found: %s) — leaving activity as NULL "
            "for all tasks this run.",
            ACTIVITY_FIELD_NAME,
            [cf.get("name") for cf in custom_fields],
        )
        return {"field_found": False, "available_fields": [cf.get("name") for cf in custom_fields]}

    activity_field_id = activity_field["id"]
    option_labels = transform.build_option_label_map(activity_field)

    activity_by_task_id = transform.extract_activity_map_from_sideload(
        tasks_included, activity_field_id, option_labels
    )

    fetch_failed = 0
    if activity_by_task_id is not None:
        method = "bulk_sideload"
        logger.info(
            "Resolved Activity via bulk sideload: %d task values found in "
            "included.customfieldTasks (no extra API calls needed)",
            len(activity_by_task_id),
        )
    else:
        method = "per_task_fallback"
        logger.warning(
            "included.customfieldTasks missing from the tasks pull — bulk sideload "
            "isn't working as expected on this account, falling back to one request "
            "per task (slower, ~%d extra API calls).",
            len(rows),
        )
        task_ids = [row["task_id"] for row in rows]
        values_by_task = tw_client.get_task_custom_field_values_bulk(
            task_ids,
            on_progress=lambda done, total: logger.info("Activity fetch: %d/%d", done, total),
        )
        activity_by_task_id = {}
        for task_id, raw_values in values_by_task.items():
            if raw_values is None:
                fetch_failed += 1
                continue
            activity_by_task_id[task_id] = transform.extract_activity_value(
                raw_values, activity_field_id, option_labels
            )

    resolved = 0
    for row in rows:
        activity = activity_by_task_id.get(row["task_id"])
        if activity is not None:
            row["activity"] = activity
            resolved += 1

    stats = {
        "field_found": True,
        "field_id": activity_field_id,
        "method": method,
        "options": list(option_labels.values()),
        "tasks_resolved": resolved,
    }
    if method == "per_task_fallback":
        stats["tasks_fetch_failed"] = fetch_failed
    return stats


def sync_tasks(tw_client, bq_client, dataset_ref, valid_project_ids):
    if valid_project_ids is None:
        logger.warning(
            "Skipping tasks sync: projects sync did not complete successfully "
            "this run, so there is no reliable set of in-scope project ids."
        )
        return {"status": "skipped", "reason": "projects sync failed"}

    raw_tasks, tasks_included = tw_client.list_tasks()
    rows = []
    for raw in raw_tasks:
        row = transform.normalize_task(raw, valid_project_ids)
        if row is not None:
            rows.append(row)

    try:
        activity_stats = enrich_tasks_with_activity(tw_client, rows, tasks_included)
    except Exception:
        logger.error("Activity enrichment failed, continuing without it:\n%s", traceback.format_exc())
        activity_stats = {"field_found": False, "error": "enrichment raised an exception, see logs"}

    written = bigquery_sync.truncate_and_load(
        bq_client, dataset_ref, schemas.TASKS_TABLE, schemas.TASKS_SCHEMA, rows
    )
    return {
        "status": "success",
        "rows_pulled": len(raw_tasks),
        "rows_written": written,
        "activity": activity_stats,
    }


def sync_users(tw_client, bq_client, dataset_ref):
    raw_users = tw_client.list_users()
    rows = [transform.normalize_user(raw) for raw in raw_users]

    written = bigquery_sync.truncate_and_load(
        bq_client, dataset_ref, schemas.USERS_TABLE, schemas.USERS_SCHEMA, rows
    )
    return {
        "status": "success",
        "rows_pulled": len(raw_users),
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
        stages["users"] = sync_users(tw_client, bq_client, dataset_ref)
    except Exception:
        logger.error("Users sync failed:\n%s", traceback.format_exc())
        stages["users"] = {"status": "failed", "error": traceback.format_exc()}

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
