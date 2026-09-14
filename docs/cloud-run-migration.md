# Migrating the scheduled sync to Cloud Run Jobs + Cloud Scheduler

**Status: planned, not started.** Nothing in this document has been built.
It exists so the reasoning survives the session that produced it.

Written 2026-09-14 against `main` @ `bbc489c`.

## Why

GitHub Actions' scheduler has never once fired this workflow on time.
Measured over 19 consecutive firings, 2026-09-05 to 2026-09-14:

| | before (`0 5,17`, 9 runs) | after (`15 4,16`, 19 runs) |
|---|---|---|
| median delay | 4h 46m | 4h 22m |
| minimum delay | 0h 10m | 1h 59m |
| fired within 30 min | 1 of 9 | 0 of 19 |

Moving the cron off `:00` was the cheap documented mitigation. It did not
work — see README "Known gaps" for the full table. The effective schedule
is roughly **05:00 and 15:00 ET**, not the intended midnight and noon.

There is **no correctness problem**: all 19 runs succeeded in 72-159s,
`rows_dropped` all zero, and the rolling two-month timelog window makes
delay harmless at month boundaries. The cost is freshness and
predictability — worst case the data is ~15 hours stale.

So this migration is a **quality-of-service** change, not a bug fix. It
should be scheduled accordingly: there is no emergency here.

## Two benefits beyond timing

1. **DST handled properly.** Cloud Scheduler takes a native timezone, so
   `0 0,12 * * *` in `America/New_York` means true midnight and noon ET
   *year-round*. That removes the hour of winter drift the README currently
   documents as deliberately accepted.
2. **The GCP key stops being a GitHub secret.** A Cloud Run Job runs as an
   attached service account. `GCP_SA_KEY_JSON` can be deleted once nothing
   in Actions needs it — see the open question about manual modes below.

## Architecture

```
Cloud Scheduler  --OIDC-->  Cloud Run Job execution
  0 0,12 * * *                 |
  TZ America/New_York          +-- attached service account --> BigQuery
                               +-- Secret Manager ------------> TEAMWORK_API_KEY
                               +-- Cloud Logging -------------> RUN_SUMMARY
```

**A Job, not a Service.** `sync.py` is finite batch work that exits — the
exact shape a Cloud Run Job is for. A Service would need an HTTP wrapper,
request authentication, and would fight the request-timeout model for no
benefit.

**Cloud Build, not GitHub Actions, for the image.** If Actions built and
pushed the image it would need GCP credentials again, defeating benefit 2.
A Cloud Build trigger on push to `main` means GitHub holds *zero* GCP
credentials. (Workload Identity Federation is the alternative if builds
should stay in Actions.)

`.github/workflows/tests.yml` is unaffected and stays where it is.

## Blocker: the shared staging table

**This must be fixed before cutover. It is not a follow-up.**

`bigquery_sync.replace_timelogs_window()` truncates and loads a single
fixed table, `timelogs__staging` (`schemas.TIMELOGS_STAGING_TABLE`), then
runs its delete+insert transaction against it.

Today this is safe only because the workflow's `concurrency` group
guarantees runs never overlap. **Cloud Run Jobs have no equivalent by
default** — two executions can run at once.

The failure is not theoretical. A scheduled run overlapping a manual
`--backfill-months 2026-01`:

1. Backfill stages January rows into `timelogs__staging`.
2. Scheduled run truncates it and stages August-September rows.
3. Backfill's transaction deletes the January window and inserts
   **August-September rows into it**.

Silent corruption of a month nobody is looking at.

Fix options, cheapest first:

- **Per-execution staging table** — suffix the name with Cloud Run's
  `CLOUD_RUN_EXECUTION` env var (or a uuid when running locally), and drop
  it in a `finally`. Small, self-contained, no new infrastructure.
- **Explicit lock** — a BigQuery row or GCS object taken at start. More
  machinery, and needs careful expiry handling so a crashed run does not
  wedge the schedule.

Recommend the first. Either way it needs a test, and per CLAUDE.md a
mutation test confirming the suite fails without it.

## Phases

| Phase | Work | Owner |
|---|---|---|
| 0 | Settle the open questions below | Repo owner |
| 1 | Per-execution staging table + tests; `Dockerfile`; prove the image runs `--dry-run` locally | Claude |
| 2 | Artifact Registry repo, Secret Manager entries, service accounts + IAM | Repo owner (`gcloud`) |
| 3 | Cloud Run Job definition; Cloud Build trigger | Claude writes config, owner applies |
| 4 | Cloud Scheduler jobs; **run in parallel with Actions for ~3 days**, diff `RUN_SUMMARY` | Both |
| 5 | Disable the Actions cron, delete `GCP_SA_KEY_JSON`, add Cloud Monitoring alerting | Repo owner |

**Phase 4 is not optional.** Running both schedulers side by side and
comparing row counts is what proves the container behaves identically
before anything is switched off. Expect `tasks.rows_written` within normal
drift of the Actions run on the same day, `rows_dropped` all zero, and
`activity.method` still `bulk_sideload`.

## What the container needs

From `config.py`, the runtime reads:

| Variable | Source in the new setup |
|---|---|
| `TEAMWORK_API_KEY` | Secret Manager (required) |
| `TEAMWORK_BASE_URL` | Secret Manager or plain env var (required) |
| `GCP_PROJECT_ID` | env var, defaults to `radiant-rig-284611` |
| `BQ_DATASET` | env var, defaults to `teamwork_data` |
| `BQ_LOCATION` | env var, defaults to `US` |
| `SYNC_TIMEZONE` | env var — **must stay `America/New_York`** |
| `GOOGLE_APPLICATION_CREDENTIALS` | **not set** — ADC picks up the attached service account |

Base image: `python:3.11-slim`, matching CI. Runtime deps are only
`requests`, `google-cloud-bigquery`, `python-dotenv`.

Service account roles: `roles/bigquery.dataEditor` and
`roles/bigquery.jobUser` on `radiant-rig-284611`, plus
`roles/secretmanager.secretAccessor` on the two secrets. The Scheduler's own
service account needs `roles/run.invoker` on the job.

Note the external Google Sheet table `gs_minimum_user_info` is **not**
affected: it is queried through `v_usermins` under the *reader's* identity,
and the pipeline service account still has no Drive access to it. That
stays true after the migration.

## Job settings to mirror today's behaviour

| Today (Actions) | Cloud Run Job equivalent |
|---|---|
| `timeout-minutes: 60` | `--task-timeout=3600s` |
| `concurrency` group | see blocker above — *not* provided by the platform |
| implicit no-retry | `--max-retries=0` (a retried partial sync is worse than a failed one) |

## Alerting

The current `gh issue create` step has no GitHub context on Cloud Run.
Replace it with a **Cloud Monitoring alert on job-execution failure**.

That is strictly better than what exists now: it also catches the job never
*starting*, which the current setup structurally cannot — a scheduled run
that never fires produces no failure, so nothing alerts. Given the delay
data above, that is a real gap.

Consider a second, log-based alert on the absence of a `RUN_SUMMARY` line
within N hours, which catches silent non-execution directly.

## Open questions

1. **Do the manual modes move too?** `--dry-run`, `--create-views`,
   `--backfill-months`, `--explain-task-scope` and `--allow-shrink`
   currently have a checkbox UI in the Actions tab. Moving them to
   `gcloud run jobs execute --args=...` is a genuine usability downgrade;
   keeping them in Actions means keeping `GCP_SA_KEY_JSON` there and losing
   half of benefit 2. **Suggested:** move the scheduled full sync only in
   phase 1, and revisit once it has proven itself.
2. **Where should failure alerts go?** Email is the default; anything else
   needs a notification channel configured first.
3. **Region.** The BigQuery dataset is `US` multi-region; `us-central1` is
   the sensible default for the job.

## Cost

Effectively nil. ~2 vCPU-minutes per day on Cloud Run (within free tier),
Cloud Scheduler's first 3 jobs free, and cents of Artifact Registry storage
for one small image.

## Cheaper stopgap, if this is deferred

Add more cron slots — four a day instead of two cuts worst-case staleness
from ~15h to ~7h. It does nothing for predictability, and it is not a step
toward this migration, but it is minutes of work.

Do **not** attempt to tune the cron to compensate for the delay: it varies
by 2-3.5 hours *within* each slot, so it cannot be targeted, and doing so
would encode a dependency on GitHub's current congestion pattern.
