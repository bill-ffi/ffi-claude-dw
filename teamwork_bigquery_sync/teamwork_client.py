"""Thin client for the Teamwork Projects REST API (v3).

Endpoint status as of the last real full sync against this account:
- PROJECTS_PATH, TASKS_PATH, TIMELOGS_PATH, PROJECT_BUDGETS_PATH, USERS_PATH,
  CUSTOM_FIELDS_PATH, TASK_CUSTOM_FIELDS_PATH_TEMPLATE: all confirmed
  working end-to-end against this account (real data pulled/loaded).
- Notable quirk: several v3 endpoints use inconsistent key casing —
  customfields.json returns items under "customfields" (lowercase f), the
  per-task custom field values endpoint returns "customfieldTasks", while
  most other list endpoints use camelCase ("projects", "tasks", "people").
  Confirmed live rather than assumed; don't "fix" the casing below.
- includeCustomFields=true on tasks.json (and projects.json) sideloads
  custom field values in bulk under included["customfieldTasks"] (or
  included["customfieldProjects"]) — confirmed via Teamwork's own public
  github.com/Teamwork/Teamwork.com-API-Request-Examples repo, NOT a guess.
  This means the per-task fetching in get_task_custom_field_values_bulk()
  is a fallback, not the primary path — see sync.py's
  enrich_tasks_with_activity() for which one actually ran.
- projects.json's included["projectCategories"] sideload is UNRELIABLE:
  confirmed present when sampled via other tooling, but confirmed EMPTY on
  every page of every real production run of this script (same params).
  Root cause not pinned down. category_name now comes from a dedicated
  PROJECT_CATEGORIES_PATH call instead (list_project_categories()) — same
  pattern already used for budgets/custom fields, not dependent on
  whatever does or doesn't ride along with the main projects pull.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger(__name__)

PROJECTS_PATH = "/projects/api/v3/projects.json"
TASKS_PATH = "/projects/api/v3/tasks.json"
TIMELOGS_PATH = "/projects/api/v3/time.json"
PROJECT_BUDGETS_PATH = "/projects/api/v3/budgets.json"
USERS_PATH = "/projects/api/v3/people.json"
CUSTOM_FIELDS_PATH = "/projects/api/v3/customfields.json"
TASK_CUSTOM_FIELDS_PATH_TEMPLATE = "/projects/api/v3/tasks/{task_id}/customfields.json"
PROJECT_CATEGORIES_PATH = "/projects/api/v3/projectcategories.json"
COMPANIES_PATH = "/projects/api/v3/companies.json"

# How many task-custom-field-value requests to have in flight at once when
# fetching per-task (no bulk endpoint exists for this). Kept modest given
# we've already been rate-limited once by this API.
CUSTOM_FIELD_FETCH_WORKERS = 8

PAGE_SIZE = 250
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 2
# Hard backstop against a runaway pagination loop (e.g. if `hasMore` never
# goes false for some reason) — 500 pages * PAGE_SIZE is far more than any
# expected dataset here, so hitting this means something is genuinely wrong
# and should fail loudly rather than hammer the API indefinitely.
MAX_PAGES = 500

# tasks.json enforces a hard ceiling on how deep page-based offset
# pagination can go — confirmed live 2026-09-03: page 200 at PAGE_SIZE=250
# (offset 50,000) returned HTTP 400 "You have exceeded our maximum offset
# request limit" once showCompletedLists=true pushed the unscoped
# site-wide total to 55,355 tasks (previously 19,577, comfortably under
# the ceiling). list_tasks() now scopes each query to a batch of project
# ids via projectIds= (a comma-joined list, confirmed working with 605 ids
# in one call during investigation) so no single paginated query's total
# gets anywhere near that ceiling, and — as a bonus — no longer fetches
# tasks for projects the caller doesn't want in the first place. Chosen
# well under the 605 already confirmed to work, as a safety margin.
TASK_PROJECT_BATCH_SIZE = 300


class PaginationLimitExceeded(RuntimeError):
    def __init__(self, path, max_pages):
        super().__init__(
            f"GET {path}: exceeded MAX_PAGES ({max_pages}) while paginating — "
            "aborting to avoid a runaway loop. This usually means the API's "
            "hasMore flag isn't behaving as expected; investigate before "
            "raising MAX_PAGES."
        )


class TeamworkAPIError(RuntimeError):
    def __init__(self, method, url, status_code, body):
        super().__init__(f"{method} {url} -> HTTP {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


class TeamworkClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (api_key, "x")
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path, params):
        url = f"{self.base_url}{path}"
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            response = self.session.get(url, params=params, timeout=60)
            if response.status_code == 200:
                return response.json()
            if response.status_code in (429, 502, 503, 504):
                last_error = TeamworkAPIError(
                    "GET", url, response.status_code, response.text
                )
                sleep_for = RETRY_BACKOFF_SECONDS * attempt
                logger.warning(
                    "Teamwork API %s on attempt %d/%d, retrying in %ds: %s",
                    response.status_code,
                    attempt,
                    MAX_RETRIES,
                    sleep_for,
                    url,
                )
                time.sleep(sleep_for)
                continue
            raise TeamworkAPIError("GET", url, response.status_code, response.text)
        raise last_error

    def _paginate(self, path, params, item_key):
        """Yields every item across all pages for a Teamwork v3 list endpoint.

        Teamwork v3 pages by `page` (1-based page number) + `pageSize`, NOT
        the page[offset]/page[size] bracket syntax — using the wrong names
        silently gets ignored by the API rather than erroring, so a bug here
        looks like "it fetched the same page forever" rather than a clean
        failure. Hence MAX_PAGES as a backstop.
        """
        page_number = 1
        while True:
            page_params = dict(params)
            page_params["page"] = page_number
            page_params["pageSize"] = PAGE_SIZE
            payload = self._get(path, page_params)
            items = payload.get(item_key, [])
            logger.info(
                "Fetched %s page %d: %d items",
                item_key,
                page_number,
                len(items),
            )
            for item in items:
                yield item
            page_meta = payload.get("meta", {}).get("page", {})
            has_more = page_meta.get("hasMore", False)
            if not has_more or not items:
                break
            page_number += 1
            if page_number > MAX_PAGES:
                raise PaginationLimitExceeded(path, MAX_PAGES)

    def list_projects(self):
        """All non-deleted projects, regardless of status (active, current,
        late, upcoming, completed, archived). Deleted projects are filtered
        out client-side in transform.normalize_project as a safety net,
        since the exact API flag for "exclude deleted" wasn't verifiable.

        Returns (projects, included) where `included` is the merged
        `included` block from every page (e.g. included["projectCategories"]
        for resolving category_id -> name).
        """
        params = {
            "includeArchivedProjects": "true",
            "includeCompletedProjects": "true",
        }
        projects = []
        included = {}
        page_number = 1
        while True:
            page_params = dict(params)
            page_params["page"] = page_number
            page_params["pageSize"] = PAGE_SIZE
            payload = self._get(PROJECTS_PATH, page_params)
            items = payload.get("projects", [])
            logger.info("Fetched projects page %d: %d items", page_number, len(items))
            projects.extend(items)
            for included_type, records in (payload.get("included") or {}).items():
                included.setdefault(included_type, {}).update(records)
            page_meta = payload.get("meta", {}).get("page", {})
            if not page_meta.get("hasMore", False) or not items:
                break
            page_number += 1
            if page_number > MAX_PAGES:
                raise PaginationLimitExceeded(PROJECTS_PATH, MAX_PAGES)
        return projects, included

    def list_tasks(self, project_ids):
        """All tasks belonging to the given project ids, including completed
        ones and tasks in completed tasklists. `project_ids` is required —
        see TASK_PROJECT_BATCH_SIZE above for why (tasks.json's offset
        pagination ceiling): the caller passes exactly the project ids it
        wants (typically sync_projects()'s task_pull_project_ids, which
        already applies the archived-project cutoff — see sync.py's
        ARCHIVED_PROJECT_TASKS_CUTOFF and README "Known gaps"), and this
        method fetches only those, in batches of TASK_PROJECT_BATCH_SIZE via
        projectIds=, rather than pulling everything site-wide and filtering
        client-side afterward.

        includeArchivedProjects=true confirmed live 2026-09-02: without it,
        tasks.json silently excludes every task belonging to an archived
        project (mirrors the same flag already needed on projects.json).
        includeArchivedTasks=true (a plausible-sounding alternative name)
        was also tried and confirmed to be a no-op on this account.

        showCompletedLists=true confirmed live 2026-09-03: without it,
        tasks.json silently excludes every task belonging to a COMPLETED
        tasklist, even when includeCompletedTasks=true and the task itself
        is neither deleted nor archived-project-excluded — confirmed via a
        real task (49325131) that was invisible to every list-based query
        without this flag, but resolved fine by direct single-task GET.
        Sourced from Teamwork's own official example repo
        (Teamwork/Teamwork.com-API-Request-Examples,
        getRequests/tasks/Get all tasks.js), confirmed verbatim, then tested
        live: it only has an effect when combined with
        includeCompletedTasks=true — added alone, or with status=all
        instead, it's a no-op. With the full combination, unscoped: site-
        wide count went from 19,577 to 55,355 (+35,778 tasks) — this is what
        first surfaced the offset-pagination ceiling above. See README
        "Known gaps" for the full investigation, including a plausible-
        sounding third-party parameter-name guess that (without this
        combination) tested as a no-op — a reminder to verify any unsourced
        API claim against the real API before trusting or dismissing it.

        Also requests includeCustomFields=true — confirmed real via
        Teamwork's own public API-Request-Examples repo (not guessed) — so
        the per-task custom field values come back sideloaded under
        included["customfieldTasks"] in the SAME response, no per-task call
        needed. Returns (tasks, included) like list_projects().
        """
        base_params = {
            "includeCompletedTasks": "true",
            "includeCustomFields": "true",
            "includeArchivedProjects": "true",
            "showCompletedLists": "true",
        }
        tasks = []
        included = {}
        sorted_ids = sorted(project_ids)
        for batch_start in range(0, len(sorted_ids), TASK_PROJECT_BATCH_SIZE):
            batch = sorted_ids[batch_start : batch_start + TASK_PROJECT_BATCH_SIZE]
            params = dict(base_params)
            params["projectIds"] = ",".join(str(project_id) for project_id in batch)
            page_number = 1
            while True:
                page_params = dict(params)
                page_params["page"] = page_number
                page_params["pageSize"] = PAGE_SIZE
                payload = self._get(TASKS_PATH, page_params)
                items = payload.get("tasks", [])
                logger.info(
                    "Fetched tasks page %d (project batch %d-%d of %d): %d items",
                    page_number,
                    batch_start + 1,
                    batch_start + len(batch),
                    len(sorted_ids),
                    len(items),
                )
                tasks.extend(items)
                for included_type, records in (payload.get("included") or {}).items():
                    if isinstance(records, dict):
                        included.setdefault(included_type, {}).update(records)
                    elif isinstance(records, list):
                        included.setdefault(included_type, []).extend(records)
                page_meta = payload.get("meta", {}).get("page", {})
                if not page_meta.get("hasMore", False) or not items:
                    break
                page_number += 1
                if page_number > MAX_PAGES:
                    raise PaginationLimitExceeded(TASKS_PATH, MAX_PAGES)
        return tasks, included

    def list_timelogs(self, start_date, end_date):
        """Timelogs with a log date in [start_date, end_date], both
        YYYY-MM-DD, inclusive.
        """
        params = {"startDate": start_date, "endDate": end_date}
        return list(self._paginate(TIMELOGS_PATH, params, "timelogs"))

    def list_project_budgets(self):
        return list(self._paginate(PROJECT_BUDGETS_PATH, {}, "budgets"))

    def list_users(self):
        """All people (Teamwork's term for users) on the account, including
        deactivated ones — deleted/deactivated users can still be referenced
        by historical timelogs and tasks, so we keep them (flagged via
        is_deleted) rather than filtering them out.
        """
        return list(self._paginate(USERS_PATH, {}, "people"))

    def list_custom_fields(self):
        """All custom field *definitions* (not values) site-wide. No filter
        params are sent since the exact accepted filter syntax wasn't
        verifiable — callers should filter the (small) result client-side.

        Confirmed live: response items are under "customfields" (all
        lowercase) — NOT "customFields".
        """
        return list(self._paginate(CUSTOM_FIELDS_PATH, {}, "customfields"))

    def list_project_categories(self):
        """All project category definitions (id, name), via a dedicated
        endpoint rather than projects.json's sideloaded
        included["projectCategories"] block — that sideload came back
        completely empty in real production runs (every page, every run)
        despite reliably showing up when sampled through other tooling
        with matching params; the discrepancy was never root-caused, so
        this sidesteps it entirely rather than depending on it.

        Response item-key casing for this specific endpoint has NOT been
        directly observed (apidocs.teamwork.com unreachable from here to
        confirm); tries the lowercase-URL-matching key first
        ("projectcategories", matching the customfields.json precedent),
        then the camelCase form as a fallback, and logs+returns [] with the
        real top-level keys visible if neither matches — see --dry-run's
        diagnostic for this endpoint.
        """
        payload = self._get(PROJECT_CATEGORIES_PATH, {"page": 1, "pageSize": PAGE_SIZE})
        item_key = None
        for candidate in ("projectcategories", "projectCategories"):
            if candidate in payload:
                item_key = candidate
                break
        if item_key is None:
            logger.warning(
                "projectcategories.json response has neither 'projectcategories' nor "
                "'projectCategories' — top-level keys were: %s",
                list(payload.keys()),
            )
            return []

        categories = list(payload.get(item_key) or [])
        page_number = 1
        while (payload.get("meta", {}).get("page", {}) or {}).get("hasMore", False):
            page_number += 1
            payload = self._get(PROJECT_CATEGORIES_PATH, {"page": page_number, "pageSize": PAGE_SIZE})
            categories.extend(payload.get(item_key) or [])
            if page_number > MAX_PAGES:
                raise PaginationLimitExceeded(PROJECT_CATEGORIES_PATH, MAX_PAGES)
        return categories

    def list_companies(self):
        """All companies (Teamwork's term for clients), for resolving a
        project's company_id -> a client name. Same defensive key-detection
        as list_project_categories() — this endpoint's item-key casing
        hasn't been directly observed either, and this API's track record
        on casing (see module docstring) makes guessing risky.
        """
        payload = self._get(COMPANIES_PATH, {"page": 1, "pageSize": PAGE_SIZE})
        item_key = None
        for candidate in ("companies", "Companies"):
            if candidate in payload:
                item_key = candidate
                break
        if item_key is None:
            logger.warning(
                "companies.json response has neither 'companies' nor 'Companies' — "
                "top-level keys were: %s",
                list(payload.keys()),
            )
            return []

        companies = list(payload.get(item_key) or [])
        page_number = 1
        while (payload.get("meta", {}).get("page", {}) or {}).get("hasMore", False):
            page_number += 1
            payload = self._get(COMPANIES_PATH, {"page": page_number, "pageSize": PAGE_SIZE})
            companies.extend(payload.get(item_key) or [])
            if page_number > MAX_PAGES:
                raise PaginationLimitExceeded(COMPANIES_PATH, MAX_PAGES)
        return companies

    def get_task_custom_field_values(self, task_id):
        """The custom field values set on one specific task. There is no
        documented bulk/site-wide equivalent — this is a single-task call.

        Confirmed live: response items are under "customfieldTasks", each
        shaped like {"customfield": {"id": ...}, "customfieldId": ...,
        "value": "<raw label string>", ...}.
        """
        path = TASK_CUSTOM_FIELDS_PATH_TEMPLATE.format(task_id=task_id)
        payload = self._get(path, {})
        return payload.get("customfieldTasks", [])

    def get_task_custom_field_values_bulk(self, task_ids, on_progress=None):
        """Fetches custom field values for many tasks concurrently (there's
        no bulk endpoint, so this is many single-task requests in parallel).
        Returns {task_id: values_list_or_None}; a None value means that
        task's fetch failed after retries — logged but not fatal, so one
        bad task doesn't sink the whole run.
        """
        results = {}
        completed = 0
        with ThreadPoolExecutor(max_workers=CUSTOM_FIELD_FETCH_WORKERS) as executor:
            future_to_task_id = {
                executor.submit(self.get_task_custom_field_values, task_id): task_id
                for task_id in task_ids
            }
            for future in as_completed(future_to_task_id):
                task_id = future_to_task_id[future]
                try:
                    results[task_id] = future.result()
                except Exception:
                    logger.warning(
                        "Failed to fetch custom field values for task %d", task_id,
                        exc_info=True,
                    )
                    results[task_id] = None
                completed += 1
                if on_progress and completed % 500 == 0:
                    on_progress(completed, len(task_ids))
        return results
