"""teamwork_client.py — pagination, scoping, and retry behaviour.

No network: every test drives a scripted FakeSession from conftest.
"""

import pytest
import requests

import teamwork_client as tc
from conftest import FakeResponse

CONNECTION_ERROR = requests.exceptions.ConnectionError("connection reset by peer")
READ_TIMEOUT = requests.exceptions.ReadTimeout("read timed out")


def page(items, has_more=False, key="projects", count=None):
    meta_page = {"hasMore": has_more}
    if count is not None:
        meta_page["count"] = count
    return FakeResponse(payload={key: items, "meta": {"page": meta_page}})


class TestRetries:
    """_get retried four status codes and no transport failures at all, so a
    single dropped connection killed a whole stage."""

    def test_succeeds_without_retrying(self, client, no_sleep):
        c = client([page([{"id": 1}])])
        assert c._get("/p", {})["projects"] == [{"id": 1}]
        assert no_sleep == []

    @pytest.mark.parametrize("failure", [
        CONNECTION_ERROR,
        READ_TIMEOUT,
        requests.exceptions.ChunkedEncodingError("cut off"),
        FakeResponse(truncated=True),          # body cut short -> ValueError
        FakeResponse(status_code=500),
        FakeResponse(status_code=502),
        FakeResponse(status_code=503),
        FakeResponse(status_code=429),
    ])
    def test_recovers_from_transient_failures(self, client, no_sleep, failure):
        c = client([failure, page([{"id": 1}])])
        assert c._get("/p", {})["projects"] == [{"id": 1}]
        assert c.session.calls == 2

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_client_errors_raise_immediately(self, client, no_sleep, status):
        c = client([FakeResponse(status_code=status, body="nope")])
        with pytest.raises(tc.TeamworkAPIError) as exc:
            c._get("/p", {})
        assert exc.value.status_code == status
        assert c.session.calls == 1, "a bad request must not be retried"
        assert no_sleep == [], "and must not sleep"

    def test_backoff_is_exponential_with_no_trailing_sleep(self, client, no_sleep):
        c = client([CONNECTION_ERROR])
        with pytest.raises(requests.exceptions.ConnectionError):
            c._get("/p", {})
        assert c.session.calls == tc.MAX_RETRIES
        # One fewer sleep than attempts: the old code slept, then gave up.
        assert no_sleep == [2, 4, 8]

    def test_retry_after_header_overrides_the_computed_backoff(self, client, no_sleep):
        c = client([FakeResponse(status_code=429, headers={"Retry-After": "30"}),
                    page([{"id": 1}])])
        c._get("/p", {})
        assert no_sleep == [30]

    def test_retry_after_is_capped(self, client, no_sleep):
        c = client([FakeResponse(status_code=429, headers={"Retry-After": "9999"}),
                    page([{"id": 1}])])
        c._get("/p", {})
        assert no_sleep == [tc.MAX_RETRY_SLEEP_SECONDS]

    def test_a_smaller_retry_after_does_not_shorten_the_backoff(self, client, no_sleep):
        c = client([FakeResponse(status_code=429, headers={"Retry-After": "1"}),
                    page([{"id": 1}])])
        c._get("/p", {})
        assert no_sleep == [2]

    @pytest.mark.parametrize("header,expected", [
        ("30", 30), (" 30 ", 30), ("0", 0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),  # HTTP-date form not honoured
        ("garbage", None), (None, None),
    ])
    def test_retry_after_parsing(self, header, expected):
        headers = {} if header is None else {"Retry-After": header}
        assert tc._retry_after_seconds(FakeResponse(headers=headers)) == expected

    def test_both_connect_and_read_timeouts_are_set(self, client, no_sleep):
        c = client([page([])])
        c._get("/p", {})
        assert c.session.requests[0]["timeout"] == tc.REQUEST_TIMEOUT_SECONDS
        assert isinstance(tc.REQUEST_TIMEOUT_SECONDS, tuple), "a connect timeout is required"


class TestPagination:
    """The first real sync used page[size]/page[offset]; Teamwork silently
    ignored them, re-fetched page 1 forever, and got rate-limited."""

    def test_uses_page_and_pageSize_not_bracket_syntax(self, client, no_sleep):
        c = client([page([{"id": 1}])])
        list(c._paginate("/p", {}, "projects"))
        params = c.session.requests[0]["params"]
        assert params["page"] == 1 and params["pageSize"] == tc.PAGE_SIZE
        assert not any("[" in k for k in params), f"bracket-syntax params: {params}"

    def test_walks_pages_until_hasMore_is_false(self, client, no_sleep):
        c = client([page([{"id": 1}], has_more=True), page([{"id": 2}])])
        assert [i["id"] for i in c._paginate("/p", {}, "projects")] == [1, 2]
        assert [r["params"]["page"] for r in c.session.requests] == [1, 2]

    def test_stops_on_an_empty_page_even_if_hasMore_stays_true(self, client, no_sleep):
        c = client([page([], has_more=True)])
        assert list(c._paginate("/p", {}, "projects")) == []
        assert c.session.calls == 1

    def test_runaway_pagination_raises_rather_than_hammering_the_api(self, client, no_sleep, monkeypatch):
        monkeypatch.setattr(tc, "MAX_PAGES", 3)
        c = client([page([{"id": 1}], has_more=True)])  # never stops on its own
        with pytest.raises(tc.PaginationLimitExceeded):
            list(c._paginate("/p", {}, "projects"))
        assert c.session.calls <= 4


class TestListTasksScoping:
    """tasks.json rejects offset pagination past 50,000 rows, so the pull is
    scoped to explicit project ids in batches."""

    def test_sends_the_flags_that_widen_task_scope(self, client, no_sleep):
        c = client([page([], key="tasks")])
        c.list_tasks({1, 2})
        params = c.session.requests[0]["params"]
        for flag in ("includeCompletedTasks", "includeArchivedProjects", "showCompletedLists"):
            assert params[flag] == "true", f"{flag} missing — tasks would be silently excluded"
        assert params["includeCustomFields"] == "true"

    def test_scopes_by_projectIds(self, client, no_sleep):
        c = client([page([], key="tasks")])
        c.list_tasks({7, 3})
        assert c.session.requests[0]["params"]["projectIds"] == "3,7"

    def test_splits_large_scopes_into_batches(self, client, no_sleep, monkeypatch):
        monkeypatch.setattr(tc, "TASK_PROJECT_BATCH_SIZE", 2)
        c = client([page([], key="tasks")])
        c.list_tasks({1, 2, 3, 4, 5})
        sent = [r["params"]["projectIds"] for r in c.session.requests]
        assert sent == ["1,2", "3,4", "5"]

    def test_merges_the_included_sideload_across_batches(self, client, no_sleep, monkeypatch):
        monkeypatch.setattr(tc, "TASK_PROJECT_BATCH_SIZE", 1)
        c = client([
            FakeResponse(payload={"tasks": [{"id": 1}], "meta": {"page": {"hasMore": False}},
                                  "included": {"customfieldTasks": {"a": {"taskId": 1}}}}),
            FakeResponse(payload={"tasks": [{"id": 2}], "meta": {"page": {"hasMore": False}},
                                  "included": {"customfieldTasks": {"b": {"taskId": 2}}}}),
        ])
        tasks, included = c.list_tasks({1, 2})
        assert len(tasks) == 2
        assert set(included["customfieldTasks"]) == {"a", "b"}

    def test_an_empty_scope_makes_no_requests(self, client, no_sleep):
        c = client([page([], key="tasks")])
        tasks, included = c.list_tasks(set())
        assert tasks == [] and c.session.calls == 0


class TestCountTasks:
    def test_reads_the_exact_count_without_fetching_rows(self, client, no_sleep):
        c = client([page([{"id": 1}], key="tasks", count=13930)])
        assert c.count_tasks({1, 2}) == 13930
        assert c.session.requests[0]["params"]["pageSize"] == 1

    def test_sums_across_batches(self, client, no_sleep, monkeypatch):
        monkeypatch.setattr(tc, "TASK_PROJECT_BATCH_SIZE", 1)
        c = client([page([], key="tasks", count=100), page([], key="tasks", count=23)])
        assert c.count_tasks({1, 2}) == 123

    def test_returns_none_rather_than_guessing_when_no_count_is_present(self, client, no_sleep):
        c = client([page([], key="tasks")])
        assert c.count_tasks({1}) is None

    def test_site_wide_count_sends_no_projectIds(self, client, no_sleep):
        c = client([page([], key="tasks", count=55386)])
        assert c.count_tasks(None) == 55386
        assert "projectIds" not in c.session.requests[0]["params"]


class TestDefensiveKeyDetection:
    """Item-key casing is inconsistent across v3 endpoints, so these two
    endpoints try candidates and log rather than guessing."""

    @pytest.mark.parametrize("key", ["projectcategories", "projectCategories"])
    def test_project_categories_accepts_either_casing(self, client, no_sleep, key):
        c = client([FakeResponse(payload={key: [{"id": 1, "name": "Books"}], "meta": {"page": {}}})])
        assert c.list_project_categories() == [{"id": 1, "name": "Books"}]

    def test_project_categories_returns_empty_on_an_unknown_shape(self, client, no_sleep):
        c = client([FakeResponse(payload={"somethingElse": [], "meta": {"page": {}}})])
        assert c.list_project_categories() == []

    @pytest.mark.parametrize("key", ["companies", "Companies"])
    def test_companies_accepts_either_casing(self, client, no_sleep, key):
        c = client([FakeResponse(payload={key: [{"id": 3, "name": "Acme"}], "meta": {"page": {}}})])
        assert c.list_companies() == [{"id": 3, "name": "Acme"}]
