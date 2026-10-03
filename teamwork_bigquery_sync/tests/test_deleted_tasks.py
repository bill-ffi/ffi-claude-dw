"""Deleted tasks are loaded and flagged, not dropped (2026-10-03).

Teamwork keeps time logged to a task after the task is deleted. The tasks pull
skipped deleted tasks, so 67 such entries (49.8h) read with no task or
Activity -- the timelog_joins check found them on its first run. tasks.json
returns deleted tasks only with showDeleted=true. Views that review live tasks
must exclude them; time reporting must not.
"""

import inspect

import pytest

import sync
import teamwork_client as tc
import views
from conftest import FakeResponse


def page(items):
    return FakeResponse(payload={"tasks": items, "meta": {"page": {"hasMore": False}}})


class TestPullAsksForDeletedTasks:
    def test_scope_params_include_show_deleted(self):
        # includeDeleted / includeDeletedTasks are silently ignored (live test).
        assert tc.TeamworkClient.TASK_SCOPE_PARAMS["showDeleted"] == "true"
        assert "includeDeleted" not in tc.TeamworkClient.TASK_SCOPE_PARAMS

    def test_every_task_request_carries_it(self, client, no_sleep):
        c = client([page([{"id": 1, "projectId": 10}])])
        c.list_tasks({10})
        assert all(r["params"].get("showDeleted") == "true" for r in c.session.requests)

    def test_the_sync_no_longer_drops_them(self):
        source = inspect.getsource(sync.sync_tasks)
        assert 'dropped["deleted"]' not in source
        assert '"deleted_tasks_kept": sum(1 for row in rows if row.get("is_deleted")),' in source


LIVE = "t.is_deleted IS NOT TRUE"


class TestViewsThatReviewLiveTasksExcludeThem:
    """A deleted task cannot be fixed, so it must not be flagged for cleanup."""

    @pytest.mark.parametrize("name", [
        "v_exception_missing_activity_with_time",
        "v_exception_missing_activty_no_time",
        "v_exception_missing_estimate",
        "v_exception_recurring_compliance",
        "v_task_review",
    ])
    def test_task_rule_views_filter_deleted(self, sql, name):
        assert f"  AND {LIVE}\n" in sql[name]

    def test_project_task_counts_exclude_deleted(self, sql):
        cte = sql["v_project_detail"].split("FROM `p.d.tasks` t", 1)[1].split("GROUP BY", 1)[0]
        assert LIVE in cte


class TestTimeReportingKeepsThem:
    def test_timelog_detail_shows_the_task_and_flags_it(self, sql):
        body = sql["v_timelog_detail"]
        assert "  tk.is_deleted AS task_is_deleted,\n" in body
        assert "is_deleted IS NOT TRUE" not in body

    def test_time_entry_rules_do_not_filter_on_task_deletion(self, sql):
        for name in ("v_exception_billable_time_internal_projects",
                     "v_exception_long_time_entries"):
            assert "is_deleted" not in sql[name], name


@pytest.fixture(scope="module")
def sql():
    return views.build_view_sql("p", "d")
