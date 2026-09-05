#!/usr/bin/env python3
"""Entrypoint: pulls Projects, Tasks, Users, and Timelogs from Teamwork and
syncs them into BigQuery. Meant to be triggered by an external scheduler
(cron, GitHub Actions, or Cloud Scheduler + Cloud Run) — see README.md.

Usage:
    python sync.py             # full sync (projects, tasks, users, and a rolling
                               # two-month timelogs window: the current calendar
                               # month plus TIMELOG_SYNC_MONTHS_BACK before it)
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
    python sync.py --explain-task-scope
                                # prints which projects the tasks pull covers
                                # and the exact task count each candidate
                                # scope definition returns, without writing
                                # anything or fetching task rows. Run this to
                                # confirm the tasks table will land on the
                                # expected size before a real sync.
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
import views
from config import load_config
from teamwork_client import (
    COMPANIES_PATH,
    CUSTOM_FIELDS_PATH,
    PROJECT_BUDGETS_PATH,
    PROJECT_CATEGORIES_PATH,
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

# Tasks belonging to archived projects are now pulled (see
# teamwork_client.list_tasks()'s includeArchivedProjects=true), but archived
# projects going back to the start of this account (2023) would add ~11,800
# mostly-stale task rows on every run for very little reporting value.
# Confirmed live 2026-09-02 (before the showCompletedLists=true fix that
# followed — see teamwork_client.list_tasks() and README "Known gaps" for
# that one; the row counts below predate it and are now understated):
# pulling every archived project's tasks brings the tasks table from 7,689
# to 19,527 rows; scoping to projects archived on or after this cutoff
# brings in only the recently-archived ones (605 projects, 4,471 tasks) for
# a total of ~12,160. Projects archived before this date are excluded from
# the tasks pull entirely — not from the projects table, which still
# carries every non-deleted project regardless of archive date. This same
# scoping applies regardless of whether a task's own tasklist is completed
# — per your instruction, an active or recently-archived project's tasks
# should all come through, completed tasklist or not.
ARCHIVED_PROJECT_TASKS_CUTOFF = "2026-01-01"
ARCHIVED_PROJECT_TASKS_CUTOFF_DATE = date.fromisoformat(ARCHIVED_PROJECT_TASKS_CUTOFF)

# Which project `status` values count as "active" for the tasks pull.
#
# The requirement is "all tasks for all ACTIVE projects, plus any project
# archived on or after ARCHIVED_PROJECT_TASKS_CUTOFF". Until now the code
# only ever tested `archived_at` — so a project that was never archived
# was in scope no matter its status, and every dormant, completed, or
# inactive project contributed its entire task history to the tasks table.
# That is the difference between "all non-archived projects" and "all
# active projects", and on this account (per README, `status` is only ever
# 'active'/'inactive', with large dormant categories such as Books [1,056
# projects] and Tax/Compliance [253]) it is a large difference.
#
# Set to {"active", "inactive"} to restore the old "everything that isn't
# archived" behaviour. `python sync.py --explain-task-scope` prints the
# project and task counts under both definitions without writing anything,
# so the choice can be checked against a known-good number first.
ACTIVE_PROJECT_STATUSES = {"active"}


def select_task_pull_projects(
    project_rows,
    active_statuses=ACTIVE_PROJECT_STATUSES,
    cutoff=ARCHIVED_PROJECT_TASKS_CUTOFF_DATE,
):
    """Which projects the tasks pull should cover, plus a breakdown of how
    every project row was classified.

    Scope = (never archived AND status in `active_statuses`)
            OR archived on/after `cutoff` (whatever its status).

    The breakdown is returned rather than just the ids so RUN_SUMMARY can
    say exactly why the tasks table is the size it is — the absence of
    that visibility is what made an over-broad scope hard to spot.
    """
    in_scope = set()
    breakdown = {
        "in_scope_active": 0,
        "in_scope_archived_on_or_after_cutoff": 0,
        "excluded_archived_before_cutoff": 0,
        "excluded_not_active": 0,
        "excluded_statuses": {},
    }
    for row in project_rows:
        archived_on = transform.parse_archived_at(row.get("archived_at"))
        status = (row.get("status") or "").strip().lower()
        if archived_on is not None:
            if archived_on >= cutoff:
                in_scope.add(row["project_id"])
                breakdown["in_scope_archived_on_or_after_cutoff"] += 1
            else:
                breakdown["excluded_archived_before_cutoff"] += 1
        elif status in active_statuses:
            in_scope.add(row["project_id"])
            breakdown["in_scope_active"] += 1
        else:
            breakdown["excluded_not_active"] += 1
            key = status or "(null)"
            breakdown["excluded_statuses"][key] = breakdown["excluded_statuses"].get(key, 0) + 1
    breakdown["projects_considered"] = len(project_rows)
    breakdown["projects_in_scope"] = len(in_scope)
    return in_scope, breakdown


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


# How many completed calendar months before the current one the normal sync
# re-pulls and replaces, on top of the current month.
#
# 1 means a rolling TWO-month window (previous month + current month).
#
# Why this isn't 0: the timelogs replace only ever touches the window it is
# given, so with a current-month-only window the previous month froze the
# instant the calendar rolled over in SYNC_TIMEZONE. Two things were lost:
# time logged on the last day of a month after that day's final run, and —
# far more commonly — any entry made or edited RETROACTIVELY against a
# closed month, which is routine in timekeeping (writing up last week on
# Monday, recoding a mistyped entry days later). Neither was ever ingested;
# both needed a manual --backfill-months to appear.
#
# Residual gap: a retroactive edit made more than `months_back` months
# after the fact still falls outside the window. Raising this constant
# widens the safety margin at the cost of re-pulling and rewriting that
# many extra months of timelogs on every run — the pull is one extra
# paginated range per month, and the rewrite stays a single atomic
# transaction regardless.
TIMELOG_SYNC_MONTHS_BACK = 1


def _shift_month(year, month, delta):
    """(year, month) shifted by `delta` months, handling year boundaries."""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _months_in_window(window_start, window_end_exclusive):
    """The YYYY-MM labels a window spans, for RUN_SUMMARY visibility."""
    labels = []
    year, month = window_start.year, window_start.month
    while (year, month) < (window_end_exclusive.year, window_end_exclusive.month):
        labels.append(f"{year:04d}-{month:02d}")
        year, month = _shift_month(year, month, 1)
    return labels


def timelog_window(tz_name, months_back=TIMELOG_SYNC_MONTHS_BACK):
    """Returns (window_start, window_end_exclusive) covering the current
    calendar month plus the `months_back` months before it, in the given
    timezone. months_back=0 reproduces the old current-month-only window.
    """
    now = datetime.now(ZoneInfo(tz_name))
    start_year, start_month = _shift_month(now.year, now.month, -months_back)
    window_start, _ = month_bounds(start_year, start_month)
    _, window_end_exclusive = month_bounds(now.year, now.month)
    return window_start, window_end_exclusive


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

    # Project category diagnostic — replaces the old approach of relying on
    # projects.json's included["projectCategories"] sideload, which was
    # confirmed empty on every page of every real production run despite
    # working fine in direct sampling. Uses the dedicated endpoint instead.
    print("\n--- Project category diagnostic ---")
    try:
        categories = client.list_project_categories()
        print(f"[OK] projectcategories.json ({PROJECT_CATEGORIES_PATH}) — {len(categories)} categories")
        if categories:
            print(f"     sample: {json.dumps(categories[0], default=str)}")
        else:
            print(
                "     WARNING: 0 categories returned. Either this account genuinely has none, "
                "or the item-key guess in list_project_categories() is wrong for this account — "
                "check the logged 'top-level keys were' warning if this looks unexpected."
            )
    except Exception as exc:
        any_failed = True
        print(f"[FAIL] project category diagnostic — {exc}")

    # Client (Teamwork "company") diagnostic — same pattern as project
    # categories, since companies.json's item-key casing hasn't been
    # directly observed either.
    print("\n--- Client (company) diagnostic ---")
    try:
        companies = client.list_companies()
        print(f"[OK] companies.json ({COMPANIES_PATH}) — {len(companies)} companies")
        if companies:
            print(f"     sample: {json.dumps(companies[0], default=str)}")
        else:
            print(
                "     WARNING: 0 companies returned. Either this account genuinely has none, "
                "or the item-key guess in list_companies() is wrong for this account — check "
                "the logged 'top-level keys were' warning if this looks unexpected."
            )
    except Exception as exc:
        any_failed = True
        print(f"[FAIL] client (company) diagnostic — {exc}")

    print()
    if any_failed:
        print("One or more checks failed. Fix TEAMWORK_BASE_URL/API key or the")
        print("*_PATH constants / pagination params in teamwork_client.py before scheduling this.")
        return 1
    print("All endpoints reachable and pagination looks correct.")
    return 0


def run_explain_task_scope(cfg):
    """Explains exactly which projects the tasks pull covers, and how many
    tasks each candidate scope definition would produce — without writing
    anything to BigQuery and without fetching a single task row (counts come
    from tasks.json's meta.page.count at pageSize=1).

    Use this to confirm the tasks table will land on the expected number
    BEFORE running a real sync, and to see which bucket any excess is
    coming from.
    """
    tw_client = TeamworkClient(cfg.teamwork_base_url, cfg.teamwork_api_key)
    print("Task scope explanation — no BigQuery writes, no task rows fetched.\n")

    raw_projects, _ = tw_client.list_projects()
    rows = []
    for raw in raw_projects:
        row = transform.normalize_project(raw, {}, {}, {})
        if row is not None:
            rows.append(row)
    print(f"Projects returned by projects.json (non-deleted): {len(rows)}")

    # How archivedAt actually arrives on this account. If "sentinel/
    # unparseable" is non-zero, the old `archived_at >= '2026-01-01'`
    # string comparison was mis-classifying those projects.
    never = sum(1 for r in rows if not r.get("archived_at"))
    sentinel = sum(
        1
        for r in rows
        if r.get("archived_at") and transform.parse_archived_at(r["archived_at"]) is None
    )
    real = len(rows) - never - sentinel
    print(f"  archivedAt null/empty      : {never}")
    print(f"  archivedAt sentinel/unparseable: {sentinel}")
    print(f"  archivedAt a real date     : {real}")
    if sentinel:
        print("  ^ these were previously read as 'archived long ago' and DROPPED from the tasks pull")

    print("\n--- projects by status x archive bucket ---")
    crosstab = {}
    for r in rows:
        status = (r.get("status") or "(null)").strip().lower()
        archived_on = transform.parse_archived_at(r.get("archived_at"))
        if archived_on is None:
            bucket = "never archived"
        elif archived_on >= ARCHIVED_PROJECT_TASKS_CUTOFF_DATE:
            bucket = f"archived >= {ARCHIVED_PROJECT_TASKS_CUTOFF}"
        else:
            bucket = f"archived <  {ARCHIVED_PROJECT_TASKS_CUTOFF}"
        crosstab[(status, bucket)] = crosstab.get((status, bucket), 0) + 1
    for (status, bucket), count in sorted(crosstab.items(), key=lambda kv: -kv[1]):
        print(f"  {status:<12} {bucket:<28} {count:>6}")

    def ids_where(predicate):
        return {r["project_id"] for r in rows if predicate(r)}

    def archived_on(r):
        return transform.parse_archived_at(r.get("archived_at"))

    def status_of(r):
        return (r.get("status") or "").strip().lower()

    recently_archived = lambda r: (
        archived_on(r) is not None and archived_on(r) >= ARCHIVED_PROJECT_TASKS_CUTOFF_DATE
    )

    candidates = [
        (
            "A  active only (never archived, status=active)",
            ids_where(lambda r: archived_on(r) is None and status_of(r) == "active"),
        ),
        (
            "B  active + archived>=cutoff  [CURRENT DEFAULT]",
            ids_where(lambda r: (archived_on(r) is None and status_of(r) == "active") or recently_archived(r)),
        ),
        (
            "C  any non-archived + archived>=cutoff  [OLD BEHAVIOUR]",
            ids_where(lambda r: archived_on(r) is None or recently_archived(r)),
        ),
        ("D  every non-deleted project", ids_where(lambda r: True)),
    ]

    print("\n--- task counts per candidate scope (exact, via meta.page.count) ---")
    for label, ids in candidates:
        count = tw_client.count_tasks(ids)
        shown = f"{count:,}" if count is not None else "unavailable (no count in meta.page)"
        print(f"  {label:<56} {len(ids):>5} projects -> {shown} tasks")

    site_wide = tw_client.count_tasks(None)
    if site_wide is not None:
        print(f"\n  site-wide, no projectIds filter{'':<26} -> {site_wide:,} tasks")
        smallest_label, smallest_ids = min(candidates, key=lambda c: len(c[1]))
        smallest_count = tw_client.count_tasks(smallest_ids)
        if smallest_count is not None and smallest_ids and smallest_count == site_wide:
            print(
                "\n[FAIL] the narrowest scope returns the same count as no filter at all —\n"
                "       tasks.json is IGNORING projectIds=, so every batch pulls the whole\n"
                "       site. Scoping is not working; do not trust a sync until this is fixed."
            )
        else:
            print("\n[OK] projectIds= is honoured (a narrower scope returns fewer tasks).")

    print(
        "\nPick the scope that matches the requirement and set ACTIVE_PROJECT_STATUSES\n"
        "in sync.py accordingly ({'active'} = B, {'active','inactive'} = C)."
    )
    return 0


def sync_projects(tw_client, bq_client, dataset_ref, allow_shrink=False):
    raw_projects, included = tw_client.list_projects()
    raw_budgets = tw_client.list_project_budgets()
    raw_categories = tw_client.list_project_categories()
    raw_companies = tw_client.list_companies()
    category_names = transform.build_category_name_map(raw_categories)
    client_names = transform.build_client_name_map(raw_companies)
    budgets_by_project = transform.build_budgets_by_project(raw_budgets)

    logger.info(
        "Client resolution: companies fetched=%d, resolved sample=%s",
        len(client_names),
        dict(list(client_names.items())[:3]),
    )
    projects_with_company_id = sum(1 for raw in raw_projects if raw.get("companyId") or raw.get("company"))
    logger.info(
        "%d/%d raw projects have a company ref (companyId or company set)",
        projects_with_company_id,
        len(raw_projects),
    )

    # Diagnostic kept from the original bug investigation: confirms
    # projects.json's sideload really is empty in production (informational
    # — no longer what category_name depends on) alongside the dedicated
    # endpoint's real resolution numbers.
    logger.info(
        "Category resolution: projects.json included types=%s (sideload, unused), "
        "categories from dedicated endpoint=%d, resolved sample=%s",
        list(included.keys()),
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
        row = transform.normalize_project(raw, category_names, budgets_by_project, client_names)
        if row is not None:
            rows.append(row)

    rows_with_category_name = sum(1 for row in rows if row.get("category_name") is not None)
    rows_with_client_name = sum(1 for row in rows if row.get("client_name") is not None)
    logger.info(
        "%d/%d final rows have a non-null category_name",
        rows_with_category_name,
        len(rows),
    )
    logger.info(
        "%d/%d final rows have a non-null client_name",
        rows_with_client_name,
        len(rows),
    )

    written = bigquery_sync.truncate_and_load(
        bq_client, dataset_ref, schemas.PROJECTS_TABLE, schemas.PROJECTS_SCHEMA, rows,
        allow_shrink=allow_shrink,
    )

    # Tasks are pulled for active projects plus projects archived on or
    # after ARCHIVED_PROJECT_TASKS_CUTOFF — see select_task_pull_projects().
    # This does NOT affect the projects table above, which keeps every
    # non-deleted project regardless of status or archive date.
    task_pull_project_ids, scope_breakdown = select_task_pull_projects(rows)
    logger.info("Task-pull project scope: %s", json.dumps(scope_breakdown, default=str))
    return {
        "status": "success",
        "rows_pulled": len(raw_projects),
        "rows_written": written,
        "categories_resolved": len(category_names),
        "rows_with_category_name": rows_with_category_name,
        "clients_resolved": len(client_names),
        "rows_with_client_name": rows_with_client_name,
        "task_pull_project_scope": scope_breakdown,
    }, task_pull_project_ids


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


def sync_tasks(tw_client, bq_client, dataset_ref, task_pull_project_ids, allow_shrink=False):
    if task_pull_project_ids is None:
        logger.warning(
            "Skipping tasks sync: projects sync did not complete successfully "
            "this run, so there is no reliable set of in-scope project ids."
        )
        return {"status": "skipped", "reason": "projects sync failed"}

    if not task_pull_project_ids:
        logger.error(
            "Skipping tasks sync: the project scope came back empty. Writing "
            "now would truncate the tasks table to zero rows."
        )
        return {"status": "failed", "reason": "empty project scope"}

    raw_tasks, tasks_included = tw_client.list_tasks(task_pull_project_ids)

    # Accounted for explicitly rather than folded into a single
    # normalize_task() -> None: when the tasks table comes out the wrong
    # size, the reason needs to be in RUN_SUMMARY, not inferred later.
    rows_by_task_id = {}
    dropped = {"deleted": 0, "no_project_id": 0, "out_of_scope": 0, "duplicate": 0}
    for raw in raw_tasks:
        if raw.get("deletedAt"):
            dropped["deleted"] += 1
            continue
        project_id = transform.task_project_id(raw)
        if project_id is None:
            dropped["no_project_id"] += 1
            continue
        if project_id not in task_pull_project_ids:
            dropped["out_of_scope"] += 1
            continue
        row = transform.normalize_task(raw, task_pull_project_ids)
        if row is None:
            continue
        if row["task_id"] in rows_by_task_id:
            dropped["duplicate"] += 1
        rows_by_task_id[row["task_id"]] = row
    rows = list(rows_by_task_id.values())

    # projectIds= is a query param this API would silently ignore if it
    # ever stopped honouring it (exactly how the page[size] bug behaved).
    # A task coming back for a project we did not ask about is the tell.
    if dropped["out_of_scope"]:
        logger.error(
            "%d of %d tasks came back for projects OUTSIDE the requested "
            "projectIds= scope — tasks.json may be ignoring the parameter and "
            "returning site-wide results. Scope counts below are unreliable.",
            dropped["out_of_scope"],
            len(raw_tasks),
        )
    if dropped["duplicate"]:
        logger.warning(
            "%d duplicate task rows collapsed by task_id before load",
            dropped["duplicate"],
        )
    logger.info(
        "Tasks: %d pulled -> %d rows after scope/dedupe (dropped: %s)",
        len(raw_tasks),
        len(rows),
        json.dumps(dropped),
    )

    try:
        activity_stats = enrich_tasks_with_activity(tw_client, rows, tasks_included)
    except Exception:
        logger.error("Activity enrichment failed, continuing without it:\n%s", traceback.format_exc())
        activity_stats = {"field_found": False, "error": "enrichment raised an exception, see logs"}

    written = bigquery_sync.truncate_and_load(
        bq_client, dataset_ref, schemas.TASKS_TABLE, schemas.TASKS_SCHEMA, rows,
        allow_shrink=allow_shrink,
    )
    return {
        "status": "success",
        "rows_pulled": len(raw_tasks),
        "rows_written": written,
        "projects_in_scope": len(task_pull_project_ids),
        "rows_dropped": dropped,
        "activity": activity_stats,
    }


def sync_users(tw_client, bq_client, dataset_ref, allow_shrink=False):
    raw_users = tw_client.list_users()
    rows = [transform.normalize_user(raw) for raw in raw_users]

    written = bigquery_sync.truncate_and_load(
        bq_client, dataset_ref, schemas.USERS_TABLE, schemas.USERS_SCHEMA, rows,
        allow_shrink=allow_shrink,
    )
    return {
        "status": "success",
        "rows_pulled": len(raw_users),
        "rows_written": written,
    }


def sync_timelogs_for_window(tw_client, bq_client, gcp_project_id, dataset_id, window_start, window_end_exclusive):
    """Deletes+reinserts the timelogs rows for exactly [window_start,
    window_end_exclusive). Used both for the normal run's rolling
    multi-month window (see timelog_window) and for backfilling a single
    past month.
    """
    last_day_inclusive = window_end_exclusive - timedelta(days=1)

    raw_timelogs = tw_client.list_timelogs(
        window_start.isoformat(), last_day_inclusive.isoformat()
    )
    rows = []
    for raw in raw_timelogs:
        row = transform.normalize_timelog(raw)
        if row is not None:
            rows.append(row)

    written = bigquery_sync.replace_timelogs_window(
        bq_client,
        gcp_project_id,
        dataset_id,
        rows,
        window_start,
        window_end_exclusive,
    )
    return {
        "status": "success",
        "rows_pulled": len(raw_timelogs),
        "rows_written": written,
        "window": [window_start.isoformat(), window_end_exclusive.isoformat()],
        "months_covered": _months_in_window(window_start, window_end_exclusive),
    }


def run_full_sync(cfg, allow_shrink=False):
    tw_client = TeamworkClient(cfg.teamwork_base_url, cfg.teamwork_api_key)
    bq_client = bigquery_sync.get_client(cfg.gcp_project_id)
    dataset_ref = bigquery_sync.ensure_dataset(
        bq_client, cfg.gcp_project_id, cfg.bq_dataset, cfg.bq_location
    )
    bigquery_sync.ensure_all_tables(bq_client, dataset_ref)

    stages = {}
    started_at = datetime.now(timezone.utc)

    task_pull_project_ids = None
    try:
        stages["projects"], task_pull_project_ids = sync_projects(
            tw_client, bq_client, dataset_ref, allow_shrink=allow_shrink
        )
    except Exception:
        logger.error("Projects sync failed:\n%s", traceback.format_exc())
        stages["projects"] = {"status": "failed", "error": traceback.format_exc()}

    try:
        stages["tasks"] = sync_tasks(
            tw_client, bq_client, dataset_ref, task_pull_project_ids,
            allow_shrink=allow_shrink,
        )
    except Exception:
        logger.error("Tasks sync failed:\n%s", traceback.format_exc())
        stages["tasks"] = {"status": "failed", "error": traceback.format_exc()}

    try:
        stages["users"] = sync_users(
            tw_client, bq_client, dataset_ref, allow_shrink=allow_shrink
        )
    except Exception:
        logger.error("Users sync failed:\n%s", traceback.format_exc())
        stages["users"] = {"status": "failed", "error": traceback.format_exc()}

    try:
        window_start, window_end_exclusive = timelog_window(cfg.sync_timezone)
        logger.info(
            "Timelogs window: [%s, %s) — current month plus %d previous",
            window_start,
            window_end_exclusive,
            TIMELOG_SYNC_MONTHS_BACK,
        )
        stages["timelogs"] = sync_timelogs_for_window(
            tw_client, bq_client, cfg.gcp_project_id, cfg.bq_dataset,
            window_start, window_end_exclusive,
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


def run_create_views(cfg):
    """(Re)creates the five exception/QC reporting views. Safe to re-run —
    views are just saved queries, this doesn't touch table data. Does not
    require the full sync's tables to have just been refreshed; it only
    needs them to exist (which ensure_all_tables() guarantees).
    """
    bq_client = bigquery_sync.get_client(cfg.gcp_project_id)
    dataset_ref = bigquery_sync.ensure_dataset(
        bq_client, cfg.gcp_project_id, cfg.bq_dataset, cfg.bq_location
    )
    bigquery_sync.ensure_all_tables(bq_client, dataset_ref)

    results = views.create_or_replace_views(bq_client, cfg.gcp_project_id, cfg.bq_dataset)
    for view_name, status in results.items():
        print(f"  {view_name}: {status}")

    summary = {
        "mode": "create_views",
        "gcp_project_id": cfg.gcp_project_id,
        "bq_dataset": cfg.bq_dataset,
        "views": results,
    }
    logger.info("RUN_SUMMARY %s", json.dumps(summary, default=str))

    all_ok = all(status == "ok" for status in results.values())
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
            stages[label] = sync_timelogs_for_window(
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
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="Permit a full-replace table to shrink by more than half. Only for a "
             "deliberate scope reduction — the guard exists because a partial "
             "Teamwork pull would otherwise silently gut a table.",
    )
    parser.add_argument(
        "--explain-task-scope",
        action="store_true",
        help="Explain which projects the tasks pull covers and how many tasks each "
             "candidate scope returns. Reads only; no BigQuery writes.",
    )
    parser.add_argument(
        "--create-views",
        action="store_true",
        help="(Re)create the exception/QC reporting views (see views.py). "
             "Does not pull from Teamwork or touch table data.",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.explain_task_scope:
        sys.exit(run_explain_task_scope(cfg))

    if args.create_views:
        sys.exit(run_create_views(cfg))

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

    sys.exit(run_full_sync(cfg, allow_shrink=args.allow_shrink))


if __name__ == "__main__":
    main()
