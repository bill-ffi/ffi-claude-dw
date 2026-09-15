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

    def test_no_single_argument_date_on_a_timestamp_column(self, sql):
        """DATE(ts) converts in UTC; DATE(ts, tz) is required.

        Same trap as bare CURRENT_DATE(), one level down: subtracting a
        UTC-derived date from CURRENT_DATE(REPORTING_TIMEZONE) reads a day off
        for anything stamped 20:00-23:59 ET. Every TIMESTAMP column in this
        schema ends in _at, so a single-argument DATE() over one is the bug.
        """
        offenders = []
        for name, body in sql.items():
            if re.search(r"DATE\(\s*\w+\.\w*_at\s*\)", body):
                offenders.append(name)
        assert offenders == [], f"DATE(timestamp) without a timezone in: {offenders}"

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

    def test_exempt_task_ids_are_excluded_from_the_long_entry_rule(self, sql):
        body = sql["v_exception_long_time_entries"]
        for task_id in views.LONG_ENTRY_EXEMPT_TASK_IDS:
            assert str(task_id) in body
        assert "NOT IN UNNEST(" in body

    def test_long_entry_task_exemption_is_null_safe(self, sql):
        """timelogs.task_id is NULLABLE -- project-level time carries no task.

        `NULL NOT IN (...)` is NULL, not TRUE, so a bare NOT IN would drop
        every no-task entry over the threshold out of this report. The
        exclusion must compare a COALESCEd value, never the raw column.
        """
        body = sql["v_exception_long_time_entries"]
        assert "COALESCE(tl.task_id, -1) NOT IN UNNEST(" in body
        assert "tl.task_id NOT IN UNNEST(" not in body

    def test_long_entry_sentinel_cannot_collide_with_a_real_task_id(self):
        """The COALESCE sentinel must be a value no Teamwork task_id can take."""
        assert all(task_id > 0 for task_id in views.LONG_ENTRY_EXEMPT_TASK_IDS)

    def test_exempt_task_list_empty_emits_no_predicate(self, monkeypatch):
        """UNNEST([]) has no inferable element type and fails to compile."""
        monkeypatch.setattr(views, "LONG_ENTRY_EXEMPT_TASK_IDS", [])
        body = views.build_view_sql("p", "d")["v_exception_long_time_entries"]
        assert "NOT IN UNNEST(" not in body
        assert "UNNEST([])" not in body

    def test_task_exemption_applies_only_to_the_long_entry_rule(self, sql):
        """PTO is exempt from the long-entry rule, not hidden account-wide."""
        for name, body in sql.items():
            if name == "v_exception_long_time_entries":
                continue
            for task_id in views.LONG_ENTRY_EXEMPT_TASK_IDS:
                assert str(task_id) not in body, name

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


class TestMissingEstimateHasParentTask:
    """has_parent_task on v_exception_missing_estimate: TRUE for a sub-task,
    FALSE for a top-level task."""

    NAME = "v_exception_missing_estimate"

    def test_column_is_present(self, sql):
        assert "AS has_parent_task" in sql[self.NAME]

    def test_derived_from_parent_task_id_not_sequence_id(self, sql):
        # sequence_id is the recurring-series identifier and unrelated:
        # sub-tasks inherit recurrence and carry no sequence_id of their own,
        # so deriving from it would make this column close to the inverse of
        # what its name says.
        body = sql[self.NAME]
        assert "(t.parent_task_id IS NOT NULL) AS has_parent_task" in body
        assert "sequence_id IS NOT NULL) AS has_parent_task" not in body

    def test_agrees_with_the_recurring_rule_on_what_a_parent_is(self, sql):
        # v_exception_recurring_compliance uses `parent_task_id IS NULL` to
        # mean "top-level". This is that test inverted; the two must not
        # drift apart.
        assert "t.parent_task_id IS NULL" in sql["v_exception_recurring_compliance"]
        assert "(t.parent_task_id IS NOT NULL) AS has_parent_task" in sql[self.NAME]

    def test_is_a_boolean_expression_not_the_raw_id(self, sql):
        # A bare parent_task_id would surface as a number in Looker rather
        # than a Yes/No dimension.
        assert "t.parent_task_id," not in sql[self.NAME]

    def test_column_is_confined_to_an_explicit_allowlist(self, sql):
        """Originally scoped to the missing-estimate rule alone.

        v_task_review was added later and carries the same column as a filter
        dimension, deliberately. This stays an allowlist rather than being
        dropped: the point is that the column appears only where someone
        decided it should, so a third view picking it up by copy-paste still
        fails here and has to be justified.
        """
        allowed = {self.NAME, "v_task_review"}
        others = [n for n, b in sql.items()
                  if n not in allowed and "has_parent_task" in b]
        assert others == [], others

    def test_every_view_with_the_column_derives_it_identically(self, sql):
        """Two views answering "is this a sub-task" must not drift apart."""
        carriers = [n for n, b in sql.items() if "AS has_parent_task" in b]
        assert len(carriers) >= 2
        for name in carriers:
            assert "(t.parent_task_id IS NOT NULL) AS has_parent_task" in sql[name], name


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

    def test_exposes_billable_rate_but_never_cost(self, sql):
        # cost_rate is comp-adjacent; excluding it lets this view be shared
        # more widely than the users table.
        body = sql["v_timelog_detail"]
        assert "tl.billable_rate" in body
        # Check the column reference, not the bare word: prose in a comment
        # is not an exposure.
        for col in ("cost_rate", "user_cost", "user_rate"):
            assert f"tl.{col}" not in body and f"u.{col}" not in body, col

    def test_billable_amount_only_counts_billable_entries(self, sql):
        # Non-billable time must not contribute revenue, and a missing rate
        # must yield NULL rather than a zero that looks like real data.
        body = sql["v_timelog_detail"]
        assert "WHEN tl.is_billable IS TRUE THEN (tl.minutes / 60) * tl.billable_rate" in body
        assert "AS billable_amount" in body
        start = body.index("WHEN tl.is_billable IS TRUE THEN (tl.minutes")
        amount = body[start:body.index("AS billable_amount")]
        assert "ELSE" not in amount, "an ELSE branch would fabricate a value for non-billable time"

    def test_unverified_rate_units_are_flagged_in_the_source(self, sql):
        # billableRate is passed through unconverted while the sibling
        # userRate was confirmed to arrive in cents. Until that is checked,
        # the warning must stay next to the column.
        import inspect
        source = inspect.getsource(views.build_view_sql)
        assert "UNITS ARE UNVERIFIED" in source


class TestTimeWithoutTaskView:
    """Exception rule: time posted to a project with no task at all."""

    NAME = "v_exception_time_without_task"

    def test_is_registered_and_renders(self, sql):
        assert self.NAME in views.VIEW_NAMES
        assert self.NAME in sql

    def test_is_created_after_the_view_it_reads_from(self, sql):
        # BigQuery needs the referenced view to exist already, and
        # create_or_replace_views() iterates this dict in insertion order.
        order = list(sql)
        assert order.index("v_timelog_detail") < order.index(self.NAME)

    def test_layers_on_the_detail_view_rather_than_re_deriving(self, sql):
        body = sql[self.NAME]
        assert "FROM `radiant-rig-284611.teamwork_data.v_timelog_detail` d" in body
        # Re-joining the base tables here would duplicate the definition of
        # "no task" and let the two drift.
        for table in ("timelogs", "tasks", "users", "projects"):
            assert f".{table}` " not in body, f"re-derives from {table}"

    def test_filters_on_the_column_not_the_label(self, sql):
        # Same population as task_join_status = 'No task (project-level
        # time)', but cannot break if that wording is edited.
        body = sql[self.NAME]
        assert "WHERE d.task_id IS NULL" in body
        assert "task_join_status" not in body

    def test_window_starts_at_the_prior_quarter(self, sql):
        body = sql[self.NAME]
        assert "INTERVAL 1 QUARTER" in body
        assert "AS prior_quarter_start" in body
        assert "d.log_date >= b.prior_quarter_start" in body

    def test_window_has_no_upper_bound(self, sql):
        # Deliberate: a future-dated timelog is itself an anomaly and an
        # exception report should surface it, not hide it behind a
        # CURRENT_DATE ceiling.
        body = sql[self.NAME]
        assert "log_date <=" not in body and "log_date <" not in body

    def test_quarters_are_evaluated_in_the_reporting_timezone(self, sql):
        # A bare CURRENT_DATE() would flip the quarter boundary early every
        # evening, exactly as it did in the user-hours views.
        body = sql[self.NAME]
        assert not re.search(r"CURRENT_DATE\(\s*\)", body)
        zones = re.findall(r"CURRENT_DATE\('([^']+)'\)", body)
        assert zones and set(zones) == {views.REPORTING_TIMEZONE}

    def test_splits_current_qtd_from_prior_quarter(self, sql):
        body = sql[self.NAME]
        assert "'Current QTD'" in body and "'Prior quarter'" in body
        assert "AS quarter_bucket" in body
        assert "d.log_date >= b.current_quarter_start THEN 'Current QTD'" in body

    def test_is_site_wide_not_category_scoped(self, sql):
        # Follows v_exception_long_time_entries: untasked time on an
        # internal project is as much a gap as on a billable one.
        body = sql[self.NAME]
        for category in views.MONITORED_CATEGORIES:
            assert f"'{category}'" not in body

    def test_carries_the_columns_a_follow_up_needs(self, sql):
        body = sql[self.NAME]
        for col in ("user_name", "user_email", "project_name", "client_name",
                    "proj_owner", "hours", "billable_status",
                    "timelog_description", "quarter_label"):
            assert col in body, col


class TestOrphanViewDetection:
    """create_or_replace_views() never drops anything, so a view retired from
    VIEW_NAMES stays live in BigQuery, frozen at its last SQL but still
    querying live tables. Two went unnoticed for months until they turned up
    in a console screenshot."""

    class FakeQueryJob:
        def __init__(self, rows, raises=None):
            self._rows, self._raises = rows, raises

        def result(self):
            if self._raises:
                raise self._raises
            return [(name,) for name in self._rows]

    class FakeClient:
        def __init__(self, rows=(), raises=None):
            self.rows, self.raises, self.sql = rows, raises, None

        def query(self, sql):
            self.sql = sql
            return TestOrphanViewDetection.FakeQueryJob(self.rows, self.raises)

    def test_clean_dataset_reports_an_empty_list(self):
        client = self.FakeClient(rows=views.VIEW_NAMES)
        assert views.list_orphan_views(client, "p", "d") == []

    def test_identifies_views_missing_from_view_names(self):
        client = self.FakeClient(
            rows=list(views.VIEW_NAMES) + ["v_exception_missing_activity",
                                           "v_user_daily_billable_hours_trend"]
        )
        assert views.list_orphan_views(client, "p", "d") == [
            "v_exception_missing_activity",
            "v_user_daily_billable_hours_trend",
        ]

    def test_a_managed_view_absent_from_bigquery_is_not_an_orphan(self):
        # Orphans are one-directional: extra views in the dataset, not
        # missing ones. A missing managed view is recreated on the next run.
        client = self.FakeClient(rows=list(views.VIEW_NAMES)[:-1])
        assert views.list_orphan_views(client, "p", "d") == []

    def test_queries_information_schema_for_views_only(self):
        # Restricting to VIEWS keeps the four native tables and the
        # externally-managed Google Sheet table out of the result.
        client = self.FakeClient(rows=views.VIEW_NAMES)
        views.list_orphan_views(client, "myproj", "mydata")
        assert "`myproj.mydata.INFORMATION_SCHEMA.VIEWS`" in client.sql

    def test_returns_none_rather_than_a_false_clean_when_the_check_fails(self):
        # Reporting "no orphans" because the query errored would be worse
        # than admitting the check could not run.
        client = self.FakeClient(raises=RuntimeError("permission denied"))
        assert views.list_orphan_views(client, "p", "d") is None


class TestSqlStringArray:
    def test_renders_a_bigquery_array_literal(self):
        assert views._sql_string_array(["a", "b"]) == "['a', 'b']"

    def test_escapes_embedded_quotes(self):
        assert "\\'" in views._sql_string_array(["O'Brien"])


class TestTaskReviewView:
    """v_task_review: a wide, one-row-per-task hygiene surface.

    Scope was specified as every task -- open AND completed -- whose project
    is not archived, at one row per task with assignees concatenated.
    """

    NAME = "v_task_review"

    def test_view_is_registered(self):
        assert self.NAME in views.VIEW_NAMES

    def test_requested_filter_dimensions_are_all_present(self, sql):
        """Every dimension named in the request must be filterable."""
        body = sql[self.NAME]
        for col in (
            "AS proj_owner",
            "AS project_name",
            "AS assignee_names",
            "p.client_name",
            "p.category_name",
            "t.tasklist_name",
            "AS task_name",
            "AS is_recurring",
            "AS has_estimate",
            "AS has_time_logged",
        ):
            assert col in body, col

    def test_scope_is_non_archived_projects_with_no_status_filter(self, sql):
        """Open and completed tasks both, projects not archived."""
        body = sql[self.NAME]
        assert "WHERE p.archived_at IS NULL" in body
        # A task-status filter would silently drop the completed half.
        assert "t.status != 'completed'" not in body
        assert "COALESCE(t.status, '') != 'completed'\nWHERE" not in body
        # ...but status must still be exposed so it can be filtered in Looker.
        assert "t.status AS task_status" in body

    def test_grain_is_one_row_per_task(self, sql):
        """Assignees resolve in a correlated subquery, never a joined UNNEST.

        A top-level UNNEST of assignee_user_ids would fan out one row per
        assignee and silently double every task count on multi-assignee tasks.
        """
        body = sql[self.NAME]
        head = body.split("FROM ", 1)[1] if "FROM " in body else body
        tail = head[head.index("`") :] if "`" in head else head
        assert "JOIN UNNEST(t.assignee_user_ids)" not in tail
        assert "CROSS JOIN UNNEST" not in tail
        assert "AS assignee_names" in body

    def test_boolean_flags_are_null_safe(self, sql):
        """A NULL status or description must not swallow the row's flag."""
        body = sql[self.NAME]
        assert "COALESCE(t.status, '') = 'completed'" in body
        assert "COALESCE(t.description, '') != ''" in body
        assert "COALESCE(ARRAY_LENGTH(t.assignee_user_ids), 0)" in body

    def test_estimate_comparisons_are_null_not_zero_without_an_estimate(self, sql):
        """An un-estimated task must not read as exactly on budget.

        Emitting 0 would let SUM(estimate_variance_hours) pull toward zero as
        though those tasks had come in on target; NULL is skipped instead.
        """
        body = sql[self.NAME]
        assert "AS estimate_variance_hours" in body
        assert "AS pct_of_estimate_used" in body
        assert "SAFE_DIVIDE(" in body, "a plain / risks divide-by-zero"

    def test_time_columns_come_from_one_aggregate_not_per_column_subqueries(self, sql):
        """Six correlated subqueries would re-scan timelogs six times a task."""
        body = sql[self.NAME]
        assert "WITH task_time AS (" in body
        assert "LEFT JOIN task_time tt ON tt.task_id = t.task_id" in body

    def test_zero_minute_entries_do_not_count_as_time_logged(self, sql):
        body = sql[self.NAME]
        assert "tl.minutes > 0" in body

    def test_uses_the_reporting_timezone_for_every_date_question(self, sql):
        """Overdue and staleness are "as of today" and must not ask UTC."""
        body = sql[self.NAME]
        assert f"CURRENT_DATE('{views.REPORTING_TIMEZONE}')" in body
        assert f"DATE(t.updated_at, '{views.REPORTING_TIMEZONE}')" in body

    def test_completed_tasks_are_never_overdue(self, sql):
        """Overdue means actionable; a late-but-finished task is not a finding."""
        body = sql[self.NAME]
        overdue = body[body.index("AS is_overdue") - 400 : body.index("AS is_overdue")]
        assert "COALESCE(t.status, '') != 'completed'" in overdue

    def test_comp_adjacent_columns_are_not_exposed(self, sql):
        """Same caution as v_timelog_detail: this view gets shared widely."""
        body = sql[self.NAME]
        for col in ("u.cost_rate", "tl.cost_rate", "user_cost", "user_rate"):
            assert col not in body, col

    def _gap_expression(self, sql):
        """The CAST terms summed into hygiene_gap_count, comment excluded."""
        body = sql[self.NAME]
        stop = body.index("AS hygiene_gap_count")
        begin = body.rindex("CAST(", 0, stop)
        begin = body.rindex("(", 0, body.rindex("CAST(COALESCE(ARRAY_LENGTH", 0, stop))
        return body[begin:stop]

    def test_hygiene_gap_count_sums_exactly_the_four_documented_gaps(self, sql):
        gap = self._gap_expression(sql)
        for term in ("assignee_user_ids", "estimate_minutes", "activity", "due_date"):
            assert term in gap, term
        assert gap.count("CAST(") == 4, gap

    def test_near_constant_flags_stay_out_of_the_count(self, sql):
        """A term that is almost always 1 offsets every score rather than
        discriminating between tasks, which is what the count is for.

        description: ~90% of open tasks have none (measured 2026-09-14).
        time logged: "none yet" is normal for a task not yet started, and it
        inherits the loaded-history floor. Both stay as filter columns.
        """
        gap = self._gap_expression(sql)
        assert "description" not in gap
        assert "logged_hours" not in gap
        assert "has_time_logged" not in gap

    def test_excluded_flags_are_still_available_as_columns(self, sql):
        """Dropping them from the score must not drop them from the view."""
        body = sql[self.NAME]
        assert "AS has_description" in body
        assert "AS has_time_logged" in body
