"""views.py — the generated BigQuery SQL.

There is no local BigQuery emulator, so correctness is checked by rendering
the SQL and asserting on its text. That is exactly how the UTC CURRENT_DATE()
bug survived: nothing ever looked at the rendered output.
"""

import re

import pytest

import views

PROJECT, DATASET = "radiant-rig-284611", "teamwork_data"


@pytest.fixture(scope="module")
def sql():
    return views.build_view_sql(PROJECT, DATASET)


class TestViewInventory:
    def test_view_names_matches_what_is_actually_rendered(self, sql):
        # VIEW_NAMES is documented as the authoritative list but nothing in
        # the code reads it, so it can drift silently.
        assert set(views.VIEW_NAMES) == set(sql)

    def test_every_view_is_a_create_or_replace(self, sql):
        for name, body in sql.items():
            assert body.lstrip().startswith("CREATE OR REPLACE VIEW"), name
            assert f"`{PROJECT}.{DATASET}.{name}`" in body, name

    def test_dependencies_are_created_before_their_dependents(self, sql):
        # BigQuery requires a referenced view to exist already, and
        # create_or_replace_views iterates this dict in order.
        order = list(sql)
        assert order.index("v_usermins") < order.index("v_user_weekly_billable_hours")
        assert order.index("v_user_daily_billable_hours_base") < order.index("v_user_weekly_billable_hours")

    def test_no_unrendered_placeholders_survive(self, sql):
        for name, body in sql.items():
            leftover = re.findall(r"\{[A-Za-z_]+\}", body)
            assert not leftover, f"{name} has unrendered placeholders: {leftover}"


class TestReportingTimezone:
    """CURRENT_DATE() takes no timezone in BigQuery and returns the UTC date,
    which misclassified every evening after 20:00 ET."""

    def test_no_bare_current_date_anywhere(self, sql):
        offenders = [n for n, b in sql.items() if re.search(r"CURRENT_DATE\(\s*\)", b)]
        assert offenders == [], f"bare CURRENT_DATE() in: {offenders}"

    def test_every_current_date_is_timezone_qualified(self, sql):
        body = sql["v_user_weekly_billable_hours"]
        zones = re.findall(r"CURRENT_DATE\('([^']+)'\)", body)
        assert zones and set(zones) == {views.REPORTING_TIMEZONE}

    def test_all_three_bounds_calls_are_qualified(self, sql):
        body = sql["v_user_weekly_billable_hours"]
        assert len(re.findall(r"CURRENT_DATE\('[^']+'\)", body)) == 3

    def test_reporting_timezone_is_a_real_iana_name(self):
        from zoneinfo import ZoneInfo
        ZoneInfo(views.REPORTING_TIMEZONE)  # raises if bogus


class TestRuleConstantsReachTheSql:
    """The rules are meant to be changed via constants, never by editing the
    generated SQL by hand."""

    def test_monitored_categories_are_interpolated(self, sql):
        body = sql["v_exception_missing_activity_with_time"]
        for category in views.MONITORED_CATEGORIES:
            assert f"'{category}'" in body

    def test_internal_categories_scope_the_internal_time_rule(self, sql):
        body = sql["v_exception_billable_time_internal_projects"]
        for category in views.INTERNAL_CATEGORIES:
            assert f"'{category}'" in body

    def test_monitored_and_internal_sets_do_not_overlap(self):
        assert not set(views.MONITORED_CATEGORIES) & set(views.INTERNAL_CATEGORIES)

    def test_long_entry_threshold_is_interpolated(self, sql):
        assert f"> {views.LONG_ENTRY_THRESHOLD_HOURS}" in sql["v_exception_long_time_entries"]

    def test_estimate_exemption_is_scoped_to_its_category(self, sql):
        body = sql["v_exception_missing_estimate"]
        assert f"'{views.ESTIMATE_EXEMPT_CATEGORY}'" in body
        for tasklist in views.ESTIMATE_EXEMPT_TASKLISTS:
            assert f"'{tasklist}'" in body

    def test_recurring_rule_is_scoped_to_its_category(self, sql):
        assert f"'{views.RECURRING_REQUIRED_CATEGORY}'" in sql["v_exception_recurring_compliance"]

    def test_external_sheet_table_is_referenced_by_constant(self, sql):
        assert views.ANCILLARY_USER_INFO_TABLE in sql["v_usermins"]


class TestBusinessWeekShape:
    def test_week_starts_on_sunday(self, sql):
        # DATE_TRUNC(..., WEEK) is BigQuery's Sunday-start default. A
        # WEEK(MONDAY) here would silently reshape every report.
        body = sql["v_user_daily_billable_hours_base"]
        assert "DATE_TRUNC(tl.log_date, WEEK)" in body
        assert "WEEK(MONDAY)" not in body

    def test_only_billable_time_reaches_the_hours_base(self, sql):
        assert "tl.is_billable = TRUE" in sql["v_user_daily_billable_hours_base"]

    def test_projection_branch_labels_non_actual_values(self, sql):
        body = sql["v_user_weekly_billable_hours"]
        for label in ("'actual'", "'minimum'", "'plug'"):
            assert label in body

    def test_friday_plug_is_clamped_at_zero(self, sql):
        assert "GREATEST(" in sql["v_user_weekly_billable_hours"]


class TestNullSafePredicates:
    """In SQL a comparison against NULL yields NULL, not TRUE, and a WHERE
    clause keeps only rows that are TRUE — so an unguarded predicate
    silently drops rows whose column is NULL, which is the opposite of what
    an exception report should do.
    """

    def test_not_completed_filter_is_null_safe(self, sql):
        body = sql["v_exception_missing_activty_no_time"]
        assert "COALESCE(t.status, '') != 'completed'" in body

    def test_no_bare_status_inequality_survives(self, sql):
        # `t.status != 'completed'` dropped every NULL-status task.
        for name, body in sql.items():
            assert not re.search(r"(?<!\)\s)t\.status\s*(!=|<>)\s*'", body), name

    def test_estimate_exemption_is_null_safe(self, sql):
        # NULL IN (...) is NULL, so TRUE AND NULL is NULL, and NOT NULL is
        # NULL: a Non-Monthly task with no tasklist name was dropped rather
        # than flagged.
        body = sql["v_exception_missing_estimate"]
        assert "COALESCE(t.tasklist_name, '')" in body

    def test_no_column_is_compared_to_a_literal_without_a_null_guard(self, sql):
        """General guard against reintroducing this class anywhere."""
        offenders = []
        for name, body in sql.items():
            for line in body.splitlines():
                stripped = line.strip()
                if not re.search(r"(!=|<>)\s*'", stripped):
                    continue
                if "COALESCE" in stripped or "IS DISTINCT FROM" in stripped:
                    continue
                offenders.append(f"{name}: {stripped}")
        assert offenders == [], (
            "inequality against a literal with no NULL guard:\n" + "\n".join(offenders)
        )


class TestTimelogDetailView:
    """A wide drill-down over every time entry, for Looker Studio."""

    def test_is_registered_and_renders(self, sql):
        assert "v_timelog_detail" in views.VIEW_NAMES
        assert "v_timelog_detail" in sql

    def test_joins_all_four_tables(self, sql):
        body = sql["v_timelog_detail"]
        assert "FROM `radiant-rig-284611.teamwork_data.timelogs` tl" in body
        for table in ("projects", "tasks", "users"):
            assert f"`radiant-rig-284611.teamwork_data.{table}`" in body, table

    def test_every_join_is_a_left_join(self, sql):
        # An inner join anywhere would silently drop time entries — the one
        # thing a drill-down over timelogs must never do.
        body = sql["v_timelog_detail"]
        joins = re.findall(r"^(\w*\s*JOIN)\s", body, re.M)
        assert joins and all(j.strip().startswith("LEFT") for j in joins), joins

    def test_surfaces_the_reporting_columns_asked_for(self, sql):
        body = sql["v_timelog_detail"]
        for col in ("project_name", "user_name", "tasklist_name", "activity"):
            assert f"AS {col}" in body or f".{col}," in body, col

    def test_explains_why_task_columns_are_blank(self, sql):
        # Blank task fields mean either project-level time or a task outside
        # the tasks-table scope; a report cannot tell those apart otherwise.
        body = sql["v_timelog_detail"]
        assert "AS task_join_status" in body
        assert "No task (project-level time)" in body
        assert "Task outside tasks-table scope" in body

    def test_hours_are_derived_from_minutes_not_the_rounded_column(self, sql):
        # timelogs.hours is stored pre-rounded to 4dp; summing it across tens
        # of thousands of rows accumulates the rounding.
        body = sql["v_timelog_detail"]
        assert "tl.minutes / 60 AS hours" in body
        assert "tl.hours" not in body

    def test_billable_status_covers_the_null_case(self, sql):
        # A NULL boolean drops out of both sides of a Looker Yes/No filter.
        body = sql["v_timelog_detail"]
        assert "AS billable_status" in body
        assert "'Unknown'" in body

    def test_week_matches_the_house_sunday_start_convention(self, sql):
        body = sql["v_timelog_detail"]
        assert "DATE_TRUNC(tl.log_date, WEEK) AS log_week_start" in body
        assert "WEEK(MONDAY)" not in body

    def test_carries_user_email_for_looker_row_level_security(self, sql):
        # A timelog has exactly one user, so this is a clean exact-match
        # field for Looker's per-viewer filtering.
        assert "u.email AS user_email" in sql["v_timelog_detail"]

    def test_does_not_expose_rate_or_cost_columns(self, sql):
        # billable_rate/cost_rate are comp-adjacent; excluded deliberately
        # so this view can be shared more widely than the users table.
        body = sql["v_timelog_detail"]
        for col in ("billable_rate", "cost_rate", "user_cost", "user_rate"):
            assert col not in body, f"{col} present — see README on sensitivity"


class TestSqlStringArray:
    def test_renders_a_bigquery_array_literal(self):
        assert views._sql_string_array(["a", "b"]) == "['a', 'b']"

    def test_escapes_embedded_quotes(self):
        assert "\\'" in views._sql_string_array(["O'Brien"])
