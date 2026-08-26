"""Thin client for the Teamwork Projects REST API (v3).

Endpoint status as of the last real dry-run against this account:
- PROJECTS_PATH, TASKS_PATH: confirmed working (returned real data).
- TIMELOGS_PATH, PROJECT_BUDGETS_PATH: the original guesses 404'd; these
  were corrected using Teamwork's public API docs (found via search, since
  apidocs.teamwork.com itself is unreachable from this environment's
  network policy) but have NOT yet been live-tested against this account.
  Re-run `python sync.py --dry-run` after any endpoint change and confirm
  [OK] on all four checks before relying on this.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

PROJECTS_PATH = "/projects/api/v3/projects.json"
TASKS_PATH = "/projects/api/v3/tasks.json"
TIMELOGS_PATH = "/projects/api/v3/time.json"
PROJECT_BUDGETS_PATH = "/projects/api/v3/budgets.json"

PAGE_SIZE = 250
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 2


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
        """Yields every item across all pages for a Teamwork v3 list endpoint."""
        offset = 0
        page_num = 0
        while True:
            page_params = dict(params)
            page_params["page[size]"] = PAGE_SIZE
            page_params["page[offset]"] = offset
            payload = self._get(path, page_params)
            items = payload.get(item_key, [])
            page_num += 1
            logger.info(
                "Fetched %s page %d: %d items (offset=%d)",
                item_key,
                page_num,
                len(items),
                offset,
            )
            for item in items:
                yield item
            page_meta = payload.get("meta", {}).get("page", {})
            has_more = page_meta.get("hasMore", False)
            if not has_more or not items:
                break
            offset += PAGE_SIZE

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
        offset = 0
        page_num = 0
        while True:
            page_params = dict(params)
            page_params["page[size]"] = PAGE_SIZE
            page_params["page[offset]"] = offset
            payload = self._get(PROJECTS_PATH, page_params)
            items = payload.get("projects", [])
            page_num += 1
            logger.info(
                "Fetched projects page %d: %d items (offset=%d)",
                page_num,
                len(items),
                offset,
            )
            projects.extend(items)
            for included_type, records in (payload.get("included") or {}).items():
                included.setdefault(included_type, {}).update(records)
            page_meta = payload.get("meta", {}).get("page", {})
            if not page_meta.get("hasMore", False) or not items:
                break
            offset += PAGE_SIZE
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
