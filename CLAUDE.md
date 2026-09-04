# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A standalone Python pipeline (`teamwork_bigquery_sync/`) that pulls Projects, Tasks, Users, and Time logs from Teamwork (project management SaaS) into BigQuery (`radiant-rig-284611.teamwork_data`), plus a second, independent layer of BigQuery views on top of that data for leadership/QC reporting via Looker Studio. Runs on a schedule via GitHub Actions (`.github/workflows/teamwork-bigquery-sync.yml`) — not meant to be run inside a chat session for production use.

`teamwork_bigquery_sync/README.md` is the authoritative, continuously-maintained record of every endpoint quirk, bug fix, and design decision in this project — read it before making non-trivial changes. This file is a map to help you navigate faster, not a replacement for it.

## Commands

All commands run from `teamwork_bigquery_sync/`.

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in TEAMWORK_API_KEY, TEAMWORK_BASE_URL, GOOGLE_APPLICATION_CREDENTIALS, SYNC_TIMEZONE

python sync.py --dry-run                          # connectivity + payload-shape check, no BigQuery writes
python sync.py                                     # full sync: projects/tasks/users (full replace) + current-month timelogs
python sync.py --backfill-months 2026-01,2026-02   # re-pull only these months' timelogs; leaves other months and projects/tasks untouched
python sync.py --create-views                      # (re)create the BigQuery reporting views from views.py; no Teamwork calls, no table writes
python sync.py --explain-task-scope                # print which projects the tasks pull covers + exact task count per candidate scope; reads only, writes nothing
```

There is no automated test suite (no pytest, no test directory). Verification is done by:
- Running `--dry-run` and checking its per-endpoint diagnostics (pagination sanity check, Activity custom-field diagnostic, project-category diagnostic, client/company diagnostic all print `[OK]`/`[FAIL]`).
- Running a real sync and inspecting the `RUN_SUMMARY` JSON log line at the end for per-table row counts and resolution stats (e.g. `rows_with_category_name`, `clients_resolved`, `activity.method`).
- For `views.py` changes: compile-check with `python -m py_compile views.py`, then render and eyeball the generated SQL via `views.build_view_sql(project_id, dataset)` before running `--create-views` against the live BigQuery project — there's no local BigQuery emulator, so SQL correctness is verified by reading the rendered text and, for date/branching logic, simulating it in a throwaway Python script before shipping.

## Architecture

### Two independent subsystems sharing one dataset

Task scope is decided in one place — `sync.py`'s `select_task_pull_projects()`, driven by `ACTIVE_PROJECT_STATUSES` and `ARCHIVED_PROJECT_TASKS_CUTOFF`. Change the rule via those constants, never by editing the filter inline, and confirm the resulting row count with `--explain-task-scope` before running a real sync. `archived_at` must go through `transform.parse_archived_at()` rather than being string-compared.

Measured 2026-09-04: the expected `tasks` row count is **~37,226** (823 in-scope projects), made up of 13,930 tasks from 218 active projects plus 23,296 from 605 projects archived on/after the cutoff. `status` and `archived_at` are perfectly collinear on this account (`active` ⟺ never archived), so `ACTIVE_PROJECT_STATUSES` currently changes nothing. Note that `WHERE p.archived_at >= '2026-01-01'` measures only the archived half (23,296) and is *not* a check on the table's total size — see README "Known gaps" before concluding the scope is wrong.

1. **Ingestion pipeline** (`config.py` → `teamwork_client.py` → `transform.py` → `bigquery_sync.py`, orchestrated by `sync.py`): pulls from the Teamwork REST API v3 and writes to four native tables (`projects`, `tasks`, `users`, `timelogs`). `projects`/`tasks`/`users` are full truncate-and-reload every run; `timelogs` only deletes+reinserts the *current calendar month* (via a staging-table + multi-statement transaction in `bigquery_sync.replace_current_month_timelogs`), leaving prior months untouched. This is the twice-daily scheduled workflow.

2. **Reporting views layer** (`views.py`, invoked via `sync.py --create-views`): pure BigQuery `CREATE OR REPLACE VIEW` SQL built in Python, run independently of the ingestion schedule. Reads from the native tables above plus one external dependency (see below). Not part of the scheduled cron job — triggered manually or via the `create_views` workflow_dispatch input.

These two subsystems don't share code and can be changed independently, but both write into `radiant-rig-284611.teamwork_data`.

### `views.py`'s layered design

The user-hours reporting views follow a base-then-union pattern, not one monolithic query:

- `v_user_daily_billable_hours_base` — the single place the "billable timelogs → weekday bucket → week" aggregation logic lives. Sparse (no scaffolding), unbounded (all history). Any new report needing per-weekday-per-week actual hours should read from this view rather than re-deriving from `timelogs` directly.
- `v_user_weekly_billable_hours` — built as a `UNION ALL` of two mutually-exclusive branches: `actual` (already-elapsed weeks/days, read from the base view) and `minimum`/`plug` (the current week's not-yet-elapsed days, projected off each user's `daily_min_bill` target). The business week runs **Sunday-through-Saturday** (`week_start` is always a Sunday, via BigQuery's default `WEEK` truncation) — this is a deliberate, non-obvious choice; don't "fix" it back to Monday-start.

Anything in `views.py` that asks "what is today?" must use `CURRENT_DATE(REPORTING_TIMEZONE)`, never bare `CURRENT_DATE()` — the bare form returns the UTC date and misclassified 17% of every week (all of 20:00-23:59 ET) until 2026-09-04. See README "Known gaps".

The five exception/QC rule views (`v_exception_*`) are simpler and independent of each other — each is a standalone `CREATE OR REPLACE VIEW` string in `views.py`, parameterized by module-level constants (`MONITORED_CATEGORIES`, `INTERNAL_CATEGORIES`, `ESTIMATE_EXEMPT_TASKLISTS`, `RECURRING_REQUIRED_CATEGORY`, `LONG_ENTRY_THRESHOLD_HOURS`) — change the rule's scope via these constants, not by hand-editing the generated SQL.

`views.py`'s `create_or_replace_views()` only creates/updates views listed in `VIEW_NAMES` — it never drops a view removed from that list. Retiring a view means both removing it from `VIEW_NAMES` *and* manually running `DROP VIEW` in BigQuery, and checking first whether anything in Looker Studio is still pointed at the old name.

### The external Google Sheet dependency

`v_usermins` joins Teamwork users to per-person minimum-billing figures that live outside Teamwork entirely, in a Google Sheet ("FFI Compensation Database and Budget", named range `mins4bq`), exposed to BigQuery as an external table (`gs_minimum_user_info`, `ANCILLARY_USER_INFO_TABLE` in `views.py`). That external table was created manually via the BigQuery console under the repo owner's own Google identity — it is **not** managed by this pipeline, `ensure_all_tables()` doesn't touch it, and the GitHub Actions service account deliberately has no Drive access to it. If this external table or its named range is ever renamed, update the constant in `views.py` and re-run `--create-views`; there's nothing to fix in the ingestion pipeline.

### Teamwork API v3 quirks worth knowing before touching `teamwork_client.py`

- Pagination uses `page` (1-based) + `pageSize` query params — not `page[size]`/`page[offset]`. Getting this wrong doesn't error, it silently reruns the same page forever (this happened once; see README).
- Item-key casing is inconsistent across endpoints (`customfields.json` → `"customfields"`, not `"customFields"`; most other endpoints use camelCase). Where casing hasn't been directly confirmed for an endpoint, the client defensively tries multiple candidate keys and logs a warning rather than guessing — follow that pattern for any new endpoint rather than hardcoding a casing assumption.
- Sideloaded data in a response's `included` block has been unreliable in production even when it worked in direct testing (`category_name` from `projects.json`'s sideload was one such case) — prefer a dedicated endpoint call over relying on `included` for anything new.

See `teamwork_bigquery_sync/README.md`'s "Known gaps" section for the full, specific history of each of these.
