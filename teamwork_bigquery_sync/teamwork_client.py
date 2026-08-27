"""Thin client for the Teamwork Projects REST API (v3).

Endpoint status as of the last real full sync against this account:
- PROJECTS_PATH, TASKS_PATH, TIMELOGS_PATH, PROJECT_BUDGETS_PATH: confirmed
  working end-to-end (real data loaded into BigQuery).
- USERS_PATH: confirmed live via a direct sample call (16 people returned,
  key "people") when this was added, but not yet exercised through a full
  `python sync.py` run. Re-run `--dry-run` and confirm [OK] before relying
  on it.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

PROJECTS_PATH = "/projects/api/v3/projects.json"
TASKS_PATH = "/projects/api/v3/tasks.json"
TIMELOGS_PATH = "/projects/api/v3/time.json"
PROJECT_BUDGETS_PATH = "/projects/api/v3/budgets.json"
USERS_PATH = "/projects/api/v3/people.json"

PAGE_SIZE = 250
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 2
# Hard backstop against a runaway pagination loop (e.g. if `hasMore` never
# goes false for some reason) — 500 pages * PAGE_SIZE is far more than any
# expected dataset here, so hitting this means something is genuinely wrong
# and should fail loudly rather than hammer the API indefinitely.
MAX_PAGES = 500


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

    def list_tasks(self):
        """All tasks site-wide, including completed ones. Filtered down to
        tasks belonging to in-scope projects by the caller.
        """
        params = {"includeCompletedTasks": "true"}
        return list(self._paginate(TASKS_PATH, params, "tasks"))

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
