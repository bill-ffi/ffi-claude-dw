# Teamwork → BigQuery sync

Pulls Projects, Tasks, and Time logs from Teamwork and loads them into
BigQuery (`radiant-rig-284611.teamwork_data`). Meant to run on a schedule
(twice daily), not inside a chat session — see **Scheduling** below.

## What it does

- **projects**, **tasks**: full truncate + reload every run.
- **timelogs**: only the current calendar month's rows are deleted and
  reinserted each run (by `log_date`, derived from Teamwork's `timeLogged`
  field). Prior months are left untouched. The delete+insert is wrapped in a
  single BigQuery multi-statement transaction (via a staging table) so a
  mid-run failure can't leave the table half-deleted.
- Projects/tasks scope: all projects except deleted ones (active, current,
  late, upcoming, completed, and archived are all included, per your
  instruction to "pull everything but deleted"). Tasks are filtered to
  belong to one of those in-scope projects.
- Logs a `RUN_SUMMARY` JSON line at the end of every run with rows
  pulled/written per table and any errors, for auditability.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in:
   - `TEAMWORK_API_KEY` — your Teamwork API token
   - `TEAMWORK_BASE_URL` — e.g. `https://forwardfinancialintelligenceinc.teamwork.com`
   - `GOOGLE_APPLICATION_CREDENTIALS` — local path to the GCP service account
     JSON key (BigQuery Data Editor + BigQuery Job User on
     `radiant-rig-284611`). **Do not commit this file** — `.gitignore` at
     the repo root already excludes `.env` and `service-account*.json`, but
     double-check before pushing.
   - `SYNC_TIMEZONE` — the timezone "current calendar month" should be
     computed in (e.g. `America/New_York`). Defaults to UTC if unset —
     **pick the right one for your team before scheduling**, since it
     determines exactly when the timelogs window rolls over at month end.
3. Sanity check connectivity before touching BigQuery:
   ```
   python sync.py --dry-run
   ```
   This hits each Teamwork endpoint with a 1-row page and prints the field
   names it got back, without writing anything. See **Known gaps** below —
   run this first and fix `teamwork_client.py`'s `*_PATH` constants if any
   endpoint 404s.
4. Run a full sync manually:
   ```
   python sync.py
   ```
   Exits 0 if all three datasets synced successfully, 1 if any stage failed
   (check the logged `RUN_SUMMARY` / stderr for which one and why).

## Scheduling recommendation

Target cadence: daily at 5am and noon (pick the timezone via `SYNC_TIMEZONE`
and configure the scheduler in the same timezone — the two aren't linked
automatically).

Given this now lives in a GitHub repo, **GitHub Actions scheduled workflow**
is the simplest fit: no infra to stand up, secrets live in repo/org GitHub
Secrets (`TEAMWORK_API_KEY`, and the service-account JSON base64-encoded or
via Workload Identity Federation to avoid storing a long-lived key at all),
and a `cron` trigger covers twice-daily easily. Cloud Scheduler + Cloud Run
is the better fit only if this needs to live alongside other GCP-native
infra, needs sub-minute reliability guarantees, or the service-account key
should never leave GCP (Cloud Run can use the attached service account
directly with zero stored key material — arguably the more secure option
long-term). Plain cron is fine too if there's already an always-on host for
it, but adds a "did the box stay up" failure mode the other two don't have.

Not building the scheduler itself per your instructions — say the word and
I'll wire up whichever one you pick.

## Known gaps / things to verify before relying on this

- **Endpoint paths unverified against live docs.** `apidocs.teamwork.com`
  was unreachable from the sandbox this was built in (network policy
  blocked it). The paths in `teamwork_client.py` (`PROJECTS_PATH`,
  `TASKS_PATH`, `TIMELOGS_PATH`, `PROJECT_BUDGETS_PATH`) are based on
  Teamwork's documented v3 conventions and on response *shapes* (pagination
  `meta.page`, field names) observed via a live Teamwork connector call
  during development — not confirmed against the actual REST API docs.
  Run `python sync.py --dry-run` and fix any `*_PATH` constant that 404s.
- **`health` (project health)** is included as a column but is best-effort:
  it wasn't present in the standard project payload during testing (even
  though Teamwork lets you *filter* projects by health). The code just
  reads `raw.get("health")`, so it'll populate automatically if your
  account's API happens to return it, and silently stay `NULL` otherwise —
  it won't break the sync either way.
- **Portfolio boards / Portfolio columns** — not included. No endpoint for
  this surfaced anywhere in the Teamwork API surface available during
  development; it may require a separate Portfolio-specific endpoint. Let
  me know if you have a specific API path for it and I'll wire it in.
- **Budget capacity/used/left** are sourced from the project-budgets
  endpoint's `capacity`/`capacityUsed` fields (confirmed real via a live
  test pull). A project can have multiple budgets (e.g. recurring monthly
  time budgets) — the code picks the one with `status == "ACTIVE"`, latest
  `startDate` among ties. Worth a sanity-check against a project you know
  the numbers for.
- **BigQuery dataset location** defaults to `US` (multi-region) — change
  `BQ_LOCATION` in `.env` if you need a specific region.
- No `users` dimension table — `assignee_user_ids`/`user_id`/etc. are raw
  Teamwork user IDs, not resolved names. Easy to add later (a `users` table
  from Teamwork's people endpoint) if useful for reporting.

## Files

- `sync.py` — entrypoint (`--dry-run` or full sync)
- `config.py` — env var loading
- `teamwork_client.py` — Teamwork REST API client (auth, pagination, retries)
- `schemas.py` — BigQuery table schemas
- `transform.py` — raw Teamwork JSON → BigQuery row mapping
- `bigquery_sync.py` — dataset/table creation, truncate+load, the
  transactional current-month replace for timelogs
