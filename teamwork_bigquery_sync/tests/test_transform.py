"""transform.py — raw Teamwork JSON to BigQuery rows.

Most cases here correspond to a bug this project actually hit; see the
README's "Known gaps" for the incident behind each.
"""

from datetime import date

import pytest

import transform


class TestParseArchivedAt:
    """The scope filter used to compare archivedAt as a raw string."""

    @pytest.mark.parametrize("value", [None, "", 0])
    def test_missing_means_never_archived(self, value):
        assert transform.parse_archived_at(value) is None

    def test_go_zero_time_is_not_a_real_archive_date(self):
        # Teamwork's v3 API is Go-based; an unset timestamp can serialise as
        # the zero time. A string compare read that as "archived in year 1"
        # and dropped the project from the tasks pull.
        assert transform.parse_archived_at("0001-01-01T00:00:00Z") is None

    def test_real_timestamp_parses_to_its_date(self):
        assert transform.parse_archived_at("2026-03-04T10:30:00Z") == date(2026, 3, 4)

    def test_unparseable_is_treated_as_never_archived(self):
        assert transform.parse_archived_at("05/01/2023") is None

    def test_cutoff_boundary_is_inclusive_on_the_day_itself(self):
        # "archived on or after 2026-01-01" must include midnight on the 1st.
        assert transform.parse_archived_at("2026-01-01T00:00:00Z") == date(2026, 1, 1)


class TestTaskProjectId:
    """Scope decisions once rested on one nested key; if it were ever absent
    every task would be silently dropped."""

    def test_reads_the_nested_tasklist_meta(self):
        raw = {"tasklist": {"meta": {"projectId": 42}}}
        assert transform.task_project_id(raw) == 42

    def test_falls_back_to_a_top_level_projectId(self):
        assert transform.task_project_id({"projectId": 7}) == 7

    def test_falls_back_to_a_project_relationship(self):
        assert transform.task_project_id({"project": {"id": 9, "type": "projects"}}) == 9

    def test_none_when_nothing_resolves(self):
        assert transform.task_project_id({"tasklist": {}}) is None


class TestNormalizeProject:
    def _raw(self, **over):
        raw = {
            "id": 1, "name": "P", "status": "active",
            "category": {"id": 5}, "companyId": 3,
            "startAt": "2026-01-02T00:00:00Z",
            "archivedAt": None,
        }
        raw.update(over)
        return raw

    def test_deleted_projects_are_dropped(self):
        assert transform.normalize_project(self._raw(status="deleted"), {}, {}) is None
        assert transform.normalize_project(self._raw(deletedAt="2026-01-01"), {}, {}) is None

    def test_resolves_category_and_client_names(self):
        row = transform.normalize_project(
            self._raw(), {5: "Monthly Close"}, {}, {3: "Acme"}
        )
        assert row["category_name"] == "Monthly Close"
        assert row["client_name"] == "Acme"

    def test_category_id_accepts_either_payload_shape(self):
        # companyId had a fallback chain; categoryId did not.
        assert transform.normalize_project(self._raw(categoryId=8, category=None), {}, {})["category_id"] == 8
        assert transform.normalize_project(self._raw(), {}, {})["category_id"] == 5

    def test_dates_are_truncated_to_the_day(self):
        assert transform.normalize_project(self._raw(), {}, {})["start_date"] == "2026-01-02"

    def test_budget_left_is_none_when_either_side_is_missing(self):
        budgets = {1: [{"status": "ACTIVE", "capacity": 100}]}
        assert transform.normalize_project(self._raw(), {}, budgets)["budget_left"] is None


class TestPickCurrentBudget:
    def test_prefers_an_active_budget(self):
        picked = transform.pick_current_budget(
            [{"status": "CLOSED", "startDate": "2026-09-01"},
             {"status": "ACTIVE", "startDate": "2026-01-01"}]
        )
        assert picked["status"] == "ACTIVE"

    def test_latest_start_date_breaks_a_tie(self):
        picked = transform.pick_current_budget(
            [{"status": "ACTIVE", "startDate": "2026-01-01"},
             {"status": "ACTIVE", "startDate": "2026-08-01"}]
        )
        assert picked["startDate"] == "2026-08-01"

    def test_none_when_there_are_no_budgets(self):
        assert transform.pick_current_budget([]) is None


class TestNormalizeTask:
    def _raw(self, **over):
        raw = {"id": 100, "tasklist": {"id": 3, "meta": {"projectId": 42, "name": "TL"}}}
        raw.update(over)
        return raw

    def test_deleted_tasks_are_dropped(self):
        assert transform.normalize_task(self._raw(deletedAt="2026-01-01"), None) is None

    def test_out_of_scope_projects_are_dropped(self):
        assert transform.normalize_task(self._raw(), {999}) is None
        assert transform.normalize_task(self._raw(), {42}) is not None

    def test_activity_defaults_to_none_for_later_enrichment(self):
        assert transform.normalize_task(self._raw(), None)["activity"] is None

    def test_repeated_fields_default_to_empty_lists_not_null(self):
        row = transform.normalize_task(self._raw(), None)
        assert row["assignee_user_ids"] == [] and row["tag_ids"] == []


class TestNormalizeTimelog:
    def _raw(self, **over):
        raw = {"id": 5, "minutes": 90, "timeLogged": "2026-08-14T13:45:00Z"}
        raw.update(over)
        return raw

    def test_log_date_comes_from_timeLogged_not_createdAt(self):
        # The replace window filters on log_date, so this mapping decides
        # which month a row belongs to.
        row = transform.normalize_timelog(self._raw(createdAt="2026-09-01T00:00:00Z"))
        assert row["log_date"] == "2026-08-14"

    def test_hours_derived_from_minutes(self):
        assert transform.normalize_timelog(self._raw())["hours"] == 1.5

    def test_hours_is_none_when_minutes_is_missing(self):
        assert transform.normalize_timelog(self._raw(minutes=None))["hours"] is None

    def test_is_billable_falls_back_when_the_key_is_present_but_null(self):
        # raw.get("isBillable", raw.get("billable")) returned None here,
        # so the fallback never fired.
        row = transform.normalize_timelog(self._raw(isBillable=None, billable=True))
        assert row["is_billable"] is True

    def test_is_billable_prefers_an_explicit_false(self):
        row = transform.normalize_timelog(self._raw(isBillable=False, billable=True))
        assert row["is_billable"] is False

    @pytest.mark.parametrize("flag", [{"deleted": True}, {"deletedAt": "2026-01-01"}])
    def test_deleted_timelogs_are_dropped(self, flag):
        assert transform.normalize_timelog(self._raw(**flag)) is None


class TestNormalizeUser:
    def test_cost_and_rate_convert_from_cents(self):
        row = transform.normalize_user({"id": 1, "userCost": 12500, "userRate": 20000})
        assert (row["user_cost"], row["user_rate"]) == (125.0, 200.0)

    def test_missing_cost_stays_none_rather_than_zero(self):
        row = transform.normalize_user({"id": 1})
        assert row["user_cost"] is None and row["user_rate"] is None

    def test_full_name_is_composed_and_trimmed(self):
        assert transform.normalize_user({"id": 1, "firstName": "Ada"})["full_name"] == "Ada"
        assert transform.normalize_user({"id": 1})["full_name"] is None


class TestActivityCustomField:
    FIELD_ID = 98742

    def test_option_map_handles_the_confirmed_dict_shape(self):
        labels = transform.build_option_label_map(
            {"options": {"choices": [{"value": "REVnCOGS"}, {"value": "PAYROLL"}]}}
        )
        assert labels == {"REVnCOGS": "REVnCOGS", "PAYROLL": "PAYROLL"}

    def test_option_map_also_accepts_a_bare_list(self):
        labels = transform.build_option_label_map(
            {"options": [{"id": 1, "label": "Books"}]}
        )
        assert labels == {1: "Books"}

    def test_extracts_a_tasks_value_by_field_id(self):
        entries = [{"customfield": {"id": self.FIELD_ID}, "value": "PAYROLL"},
                   {"customfield": {"id": 1}, "value": "other"}]
        assert transform.extract_activity_value(entries, self.FIELD_ID, {}) == "PAYROLL"

    def test_missing_sideload_key_signals_fallback_but_empty_does_not(self):
        # None tells the caller to fall back to per-task fetching; {} means
        # the sideload worked and simply matched nothing.
        assert transform.extract_activity_map_from_sideload({}, self.FIELD_ID, {}) is None
        assert transform.extract_activity_map_from_sideload(
            {"customfieldTasks": []}, self.FIELD_ID, {}
        ) == {}

    @pytest.mark.parametrize("shape", ["list", "dict"])
    def test_sideload_handles_both_list_and_dict_containers(self, shape):
        entry = {"customfield": {"id": self.FIELD_ID}, "taskId": 7, "value": "HR"}
        container = [entry] if shape == "list" else {"a": entry}
        got = transform.extract_activity_map_from_sideload(
            {"customfieldTasks": container}, self.FIELD_ID, {}
        )
        assert got == {7: "HR"}


class TestLookupMaps:
    def test_category_and_client_maps_skip_entries_without_an_id(self):
        assert transform.build_category_name_map(
            [{"id": 1, "name": "A"}, {"name": "no id"}]
        ) == {1: "A"}
        assert transform.build_client_name_map([{"id": 2, "name": "Acme"}]) == {2: "Acme"}

    def test_budgets_group_by_project(self):
        grouped = transform.build_budgets_by_project(
            [{"projectId": 1, "capacity": 5}, {"project": {"id": 1}}, {"capacity": 9}]
        )
        assert len(grouped[1]) == 2 and list(grouped) == [1]
