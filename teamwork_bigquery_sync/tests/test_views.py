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


class TestProjectDetailView:
    """v_project_detail: one row per ACTIVE project, for Looker Studio.

    Exists because the raw projects table is a poor direct source: bare user
    ids instead of names, and a REPEATED tag_ids column the connector cannot
    handle.
    """

    NAME = "v_project_detail"

    def test_view_is_registered(self):
        assert self.NAME in views.VIEW_NAMES

    def test_scope_matches_v_task_review_exactly(self, sql):
        """Both views must agree about which projects exist.

        status = 'active' and archived_at IS NULL are collinear on this
        account today. Picking the same spelling in both places means they
        cannot quietly diverge if that ever stops being true.
        """
        assert "WHERE p.archived_at IS NULL" in sql[self.NAME]
        assert "WHERE p.archived_at IS NULL" in sql["v_task_review"]
        assert "p.status = 'active'" not in sql[self.NAME]

    def test_raw_status_is_still_exposed_for_filtering(self, sql):
        assert "p.status AS project_status" in sql[self.NAME]

    def test_user_ids_are_resolved_to_names(self, sql):
        """The main reason the view exists: ids are useless as dimensions."""
        body = sql[self.NAME]
        assert "owner.full_name AS proj_owner" in body
        assert "creator.full_name AS created_by_name" in body
        assert "completer.full_name AS completed_by_name" in body

    def test_repeated_and_empty_columns_are_not_carried(self, sql):
        """tag_ids is REPEATED (Looker cannot read it) and has no name lookup;
        health is empty on every row."""
        body = sql[self.NAME]
        assert "tag_ids" not in body
        assert "p.health" not in body

    def test_comp_adjacent_columns_are_not_exposed(self, sql):
        body = sql[self.NAME]
        for col in ("cost_rate", "user_cost", "user_rate"):
            assert col not in body, col

    def test_rollups_are_grouped_so_they_cannot_fan_out(self, sql):
        """Each CTE is one row per project, keeping the view one row per project."""
        body = sql[self.NAME]
        assert "GROUP BY t.project_id" in body
        assert "GROUP BY tl.project_id" in body
        assert "LEFT JOIN project_tasks pt ON pt.project_id = p.project_id" in body
        assert "LEFT JOIN project_time ptm ON ptm.project_id = p.project_id" in body

    def test_budget_ratio_is_null_not_zero_without_a_budget(self, sql):
        """An un-budgeted project must not read as having spent nothing."""
        body = sql[self.NAME]
        assert "AS pct_of_budget_used" in body
        assert "SAFE_DIVIDE(p.budget_used, p.budget_capacity)" in body

    def test_budget_columns_are_not_divided_again_in_sql(self, sql):
        """transform.cents_to_dollars already converted them on ingest."""
        body = sql[self.NAME]
        assert "budget_capacity / 100" not in body
        assert "budget_used / 100" not in body

    def test_zero_minute_entries_are_excluded_from_the_time_rollup(self, sql):
        assert "tl.minutes > 0" in sql[self.NAME]

    def test_uses_the_reporting_timezone_for_every_date_question(self, sql):
        body = sql[self.NAME]
        assert f"CURRENT_DATE('{views.REPORTING_TIMEZONE}')" in body
        assert f"DATE(p.updated_at, '{views.REPORTING_TIMEZONE}')" in body

    def test_a_completed_project_is_never_past_its_end_date(self, sql):
        """Past-end-date means actionable; a finished project is not."""
        body = sql[self.NAME]
        marker = "AS is_past_end_date"
        window = body[body.index(marker) - 300 : body.index(marker)]
        assert "p.completed_at IS NULL" in window


class TestTaskReviewParentRollup:
    """parent_task_name + a rollup key, so a report can group sub-tasks under
    their parent. parent_task_id alone cannot: it is NULL on top-level tasks
    and an integer is not a readable report dimension.
    """

    NAME = "v_task_review"

    def test_parent_name_is_resolved(self, sql):
        assert "parent.name AS parent_task_name" in sql[self.NAME]

    def test_rollup_columns_exist(self, sql):
        body = sql[self.NAME]
        assert "AS rollup_task_id" in body
        assert "AS rollup_task_name" in body

    def test_rollup_id_and_name_share_one_condition(self, sql):
        """They must always describe the SAME task.

        Coalescing them independently -- COALESCE(t.parent_task_id, t.task_id)
        with COALESCE(parent.name, t.name) -- pairs a parent's id with a
        child's name whenever the parent is missing from the tasks table
        (deleted, or outside the pull). Both must branch on whether the parent
        actually resolved.
        """
        body = sql[self.NAME]
        assert "IF(parent.task_id IS NOT NULL, t.parent_task_id, t.task_id) AS rollup_task_id" in body
        assert "IF(parent.task_id IS NOT NULL, parent.name, t.name) AS rollup_task_name" in body
        assert "COALESCE(t.parent_task_id, t.task_id)" not in body
        assert "COALESCE(parent.name, t.name)" not in body

    def _parent_join_line(self, sql):
        """The one line joining the parent task, isolated.

        Located rather than string-split: splitting on an anchor that is
        absent yields the whole body, whose last line is a DIFFERENT LEFT
        JOIN, so the naive assertion passed against broken SQL.
        """
        lines = [l for l in sql[self.NAME].splitlines()
                 if "parent ON parent.task_id" in l]
        assert len(lines) == 1, lines
        return lines[0]

    def test_parent_join_is_left_and_keyed_on_the_parent_id(self, sql):
        """LEFT, because an inner join drops every top-level task. Keyed on
        parent_task_id, because keying it on task_id joins each task to
        itself and makes every task its own parent."""
        line = self._parent_join_line(sql)
        assert line.startswith("LEFT JOIN"), line
        assert line.endswith("= t.parent_task_id"), line

    def test_rollup_columns_are_confined_to_an_explicit_allowlist(self, sql):
        """v_timelog_detail carries the same pair deliberately, at timelog
        grain, so a monthly pivot can roll sub-tasks up. This stays an
        allowlist rather than being dropped: a third view picking the columns
        up by copy-paste should still fail here and be justified.
        """
        allowed = {self.NAME, "v_timelog_detail"}
        others = [n for n, b in sql.items()
                  if n not in allowed and "rollup_task_id" in b]
        assert others == [], others

    def test_every_view_with_the_rollup_derives_it_identically(self, sql):
        """Two views answering "which task does this roll up to" must agree."""
        carriers = [n for n, b in sql.items() if "AS rollup_task_id" in b]
        assert len(carriers) == 2, carriers
        for name in carriers:
            body = sql[name]
            assert "IF(parent.task_id IS NOT NULL," in body, name
            assert "COALESCE(parent.name," not in body, name


class TestTimelogDetailParentRollup:
    """The same rollup pair at timelog grain.

    v_task_review is one row per task with lifetime totals, so it has no date
    to pivot on. A month-by-month report has to read from v_timelog_detail,
    which is why these columns exist in both places.
    """

    NAME = "v_timelog_detail"

    def test_parent_name_and_rollup_columns_exist(self, sql):
        body = sql[self.NAME]
        assert "parent.name AS parent_task_name" in body
        assert "AS rollup_task_id" in body
        assert "AS rollup_task_name" in body

    def test_rollup_id_and_name_share_one_condition(self, sql):
        body = sql[self.NAME]
        assert "IF(parent.task_id IS NOT NULL, tk.parent_task_id, tk.task_id) AS rollup_task_id" in body
        assert "IF(parent.task_id IS NOT NULL, parent.name, tk.name) AS rollup_task_name" in body

    def test_project_level_time_gets_no_invented_group(self, sql):
        """Time with no task must leave the rollup NULL.

        Both branches read from tk, which is NULL when there is no task, so
        the columns fall out NULL rather than grouping untasked time under
        some made-up label. task_join_status already names that population.
        """
        body = sql[self.NAME]
        assert "tk.task_id) AS rollup_task_id" in body
        assert "tk.name) AS rollup_task_name" in body
        assert "'No task (project-level time)'" in body

    def test_parent_join_is_left_and_keyed_on_the_task_alias(self, sql):
        """Must hang off tk (the timelog's task), not tl, and keep every row.

        Keyed on tl.task_id instead, every timelog would join to its OWN task
        as though that task were its parent, and rollup_task_name would report
        the task itself for everything.
        """
        lines = [l for l in sql[self.NAME].splitlines()
                 if "parent ON parent.task_id" in l]
        assert len(lines) == 1, lines
        assert lines[0].startswith("LEFT JOIN"), lines[0]
        assert lines[0].endswith("= tk.parent_task_id"), lines[0]

    def test_the_date_column_a_monthly_pivot_needs_is_present(self, sql):
        """The reason this view is the right source for a monthly report."""
        body = sql[self.NAME]
        assert "tl.log_date" in body


class TestClientMonthView:
    """v_client_month: % of budget over a DYNAMIC date range.

    A Looker blend joins a client-level budget to month-level revenue and
    contributes the budget once regardless of the selected range, so revenue
    scales with the date filter and the denominator does not. Putting the
    monthly budget on each month's row is what makes
    SUM(revenue) / SUM(budget) correct for any period.
    """

    NAME = "v_client_month"

    def test_view_is_registered(self):
        assert self.NAME in views.VIEW_NAMES

    def test_created_after_the_view_it_reads_from(self, sql):
        """BigQuery requires a referenced view to already exist."""
        names = list(sql)
        assert names.index(self.NAME) > names.index("v_timelog_detail")

    def test_reads_the_base_view_not_raw_timelogs(self, sql):
        """Join logic and billable_amount stay defined in one place."""
        body = sql[self.NAME]
        assert "v_timelog_detail` d" in body
        assert "teamwork_data.timelogs` " not in body

    def test_budget_is_carried_per_month_not_per_client(self, sql):
        """The whole point: one row per client per month, each with the budget.

        Grouping the budget only by client would reproduce the blend's bug.
        """
        body = sql[self.NAME]
        assert "GROUP BY client_name, month_start" in body
        assert "GENERATE_DATE_ARRAY(bp.first_month, bp.last_month, INTERVAL 1 MONTH)" in body

    def test_month_spine_is_bounded_by_project_dates(self, sql):
        """Per instruction: budget accrues only while the engagement is live."""
        body = sql[self.NAME]
        assert "COALESCE(p.start_date, b.floor_month)" in body
        assert "COALESCE(p.end_date, b.current_month)" in body

    def test_spine_is_clamped_to_the_history_floor_and_current_month(self, sql):
        """A month before loaded history carries budget against zero revenue."""
        body = sql[self.NAME]
        assert "GREATEST(" in body and "b.floor_month" in body
        assert "LEAST(" in body and "b.current_month" in body
        assert views.CLIENT_MONTH_HISTORY_FLOOR in body

    def test_history_floor_is_a_real_date(self):
        from datetime import date
        y, m, d = (int(p) for p in views.CLIENT_MONTH_HISTORY_FLOOR.split("-"))
        assert date(y, m, d)

    def test_only_budgeted_non_archived_projects_form_the_denominator(self, sql):
        body = sql[self.NAME]
        assert "WHERE p.archived_at IS NULL" in body
        assert "COALESCE(p.budget_capacity, 0) > 0" in body

    def test_budget_is_not_divided_again_in_sql(self, sql):
        """transform.cents_to_dollars already converted it on ingest."""
        body = sql[self.NAME]
        assert "budget_capacity / 100" not in body

    def test_full_outer_join_keeps_both_sides(self, sql):
        """A budgeted month with no time still consumes budget; revenue on an
        unbudgeted project must not vanish."""
        body = sql[self.NAME]
        assert "FULL OUTER JOIN client_month_actuals a" in body
        assert "COALESCE(b.client_name, a.client_name) AS client_name" in body
        assert "COALESCE(b.month_start, a.month_start) AS month_start" in body

    def test_additive_columns_are_zero_filled_so_they_can_be_summed(self, sql):
        body = sql[self.NAME]
        for col in ("monthly_budget", "billable_revenue", "billable_hours"):
            assert f"AS {col}," in body
        assert "COALESCE(a.billable_revenue, 0) AS billable_revenue" in body
        assert "COALESCE(b.monthly_budget, 0) AS monthly_budget" in body

    def test_no_row_level_percentage_column(self, sql):
        """The view emits additive columns only.

        A row-level percentage cannot be summed or averaged across months, so
        it is wrong for every range but one -- and a dynamic range is the
        reason this view exists. It also double-scales: the repo's pct_*
        columns are already x100, so Looker's Percent type renders a real 128%
        as 12,840%, which happened in a live report. Removed 2026-09-21; this
        guards against it being reintroduced out of convenience.
        """
        body = sql[self.NAME]
        assert "AS pct_of_budget" not in body
        assert "AS is_over_budget" not in body

    def test_every_emitted_measure_is_additive(self, sql):
        """Anything summable over a date range is safe; nothing else ships.

        Percentages, ratios and averages are all non-additive. If one is ever
        added here, it must be as an aggregate in the report, not a column.
        """
        body = sql[self.NAME]
        select = body[body.rindex("SELECT"):body.rindex("FROM client_month_budget")]
        # Strip SQL comments: the assertion is about emitted columns, not
        # prose. Explaining why a percentage is absent should not trip a test
        # looking for percentages.
        code = "\n".join(
            l for l in select.splitlines() if not l.strip().startswith("--")
        )
        for banned in ("pct_", "_pct", "AVG(", "ratio"):
            assert banned not in code, banned

    def test_the_correct_aggregate_is_documented_in_the_sql(self, sql):
        """Removing the column only helps if the replacement is findable.

        Someone reaching for a percentage should hit the right expression in
        the view itself, not have to rediscover it.
        """
        body = sql[self.NAME]
        assert "DELIBERATELY NO pct_of_budget" in body
        assert "SUM(billable_revenue) / SUM(monthly_budget)" in body

    def test_only_one_revenue_column(self, sql):
        """Scoped to one column per instruction; budgets land imminently."""
        body = sql[self.NAME]
        assert "billable_revenue_budgeted_projects" not in body
        assert "pct_of_budget_budgeted_projects_only" not in body

    def test_rollout_progress_is_still_visible(self, sql):
        """With the split revenue column gone, these two counts are what shows
        that a client-month's revenue includes unbudgeted work."""
        body = sql[self.NAME]
        assert "AS budgeted_project_count" in body
        assert "AS project_count" in body

    def test_uses_the_reporting_timezone(self, sql):
        assert f"CURRENT_DATE('{views.REPORTING_TIMEZONE}')" in sql[self.NAME]


class TestDataFreshnessView:
    """v_data_freshness: the "last updated at" source for Looker Studio.

    synced_at is a BigQuery TIMESTAMP, which Looker renders in UTC — a 09:53
    UTC sync reads as a mid-morning refresh when it was 05:53 ET. The whole
    point of this view is that the conversion happens in SQL, where DST comes
    off the tz database, rather than as a fixed-offset shift in Looker.
    """

    NAME = "v_data_freshness"

    def test_view_is_registered(self):
        assert self.NAME in views.VIEW_NAMES

    def test_reads_only_base_tables(self, sql):
        """This is what leaves its position in the creation order free.

        If it ever starts reading another view, it needs an ordering test like
        v_client_month's — so fail here rather than at --create-views time.
        """
        body = sql[self.NAME]
        referenced = set(re.findall(r"teamwork_data\.(\w+)`", body))
        assert referenced == {"projects", "tasks", "users", "timelogs", self.NAME}

    def test_all_four_tables_are_covered(self, sql):
        body = sql[self.NAME]
        for table in ("projects", "tasks", "users", "timelogs"):
            assert f"SELECT '{table}' AS table_name, MAX(synced_at)" in body

    def test_per_table_stamp_is_a_max_never_a_min(self, sql):
        """timelogs is a windowed replace: only the rolling window's rows get
        a fresh synced_at, so an untouched January row keeps a months-old
        stamp. MIN within a table would report that as the sync time."""
        body = sql[self.NAME]
        per_table = body.split("rolled AS")[0]
        assert "MIN(" not in per_table
        assert per_table.count("MAX(synced_at)") == 4

    def test_headline_is_the_oldest_table_not_the_newest(self, sql):
        """The pipeline fails a stage rather than writing bad data, so a
        partial failure leaves one table stale and the rest current. MAX
        across the four would claim a freshness the dashboard lacks."""
        body = sql[self.NAME]
        assert "MIN(synced_at) AS oldest_synced_at" in body
        assert (
            f"DATETIME(r.oldest_synced_at, '{views.REPORTING_TIMEZONE}') "
            "AS last_synced_at_et"
        ) in body
        assert "AS last_synced_at_et" in body and "newest_synced_at) AS last_synced_at_et" not in body

    def test_age_is_measured_from_the_oldest_stamp_too(self, sql):
        """A headline of MIN and an age off MAX would contradict each other."""
        body = sql[self.NAME]
        for unit in ("MINUTE", "SECOND", "HOUR"):
            assert f"TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.oldest_synced_at, {unit})" in body
        assert "TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.newest_synced_at" not in body

    def test_every_rendered_timestamp_is_timezone_converted(self, sql):
        """A single bare DATETIME(ts) would silently reintroduce UTC."""
        body = sql[self.NAME]
        total = len(re.findall(r"DATETIME\(", body))
        qualified = len(re.findall(r"DATETIME\([^)]+, '" + views.REPORTING_TIMEZONE + r"'\)", body))
        assert total > 0 and total == qualified

    def test_the_preformatted_label_is_timezone_qualified(self, sql):
        """FORMAT_TIMESTAMP defaults to UTC when the zone is omitted."""
        body = sql[self.NAME]
        assert re.search(
            r"FORMAT_TIMESTAMP\(\s*'[^']+',\s*r\.oldest_synced_at,\s*'"
            + views.REPORTING_TIMEZONE
            + r"'\s*\)",
            body,
        )

    def test_age_uses_current_timestamp_not_current_date(self, sql):
        """CURRENT_TIMESTAMP() needs no timezone — a TIMESTAMP is an absolute
        instant and TIMESTAMP_DIFF between two is timezone-independent. This
        pins that so nobody "fixes" it into a CURRENT_DATE comparison."""
        body = sql[self.NAME]
        assert "CURRENT_TIMESTAMP()" in body
        assert "CURRENT_DATE(" not in body

    def test_stale_threshold_comes_from_the_constant(self, sql):
        body = sql[self.NAME]
        assert f">= {views.DATA_FRESHNESS_STALE_AFTER_HOURS}" in body
        assert ") AS is_stale" in body

    def test_stale_threshold_clears_the_measured_scheduler_gap(self):
        """The cron implies 12h; GitHub actually delivers the two firings
        9.5-15.1h apart (33 firings measured, see README "Known gaps"). A
        threshold at or below that flags stale during normal operation and
        trains everyone to ignore the indicator."""
        assert views.DATA_FRESHNESS_STALE_AFTER_HOURS > 15.1

    def test_empty_table_guard_is_present(self, sql):
        """MIN/MAX skip NULLs, so an empty table would vanish rather than
        drag the headline down. tables_reporting should always be 4."""
        body = sql[self.NAME]
        assert "COUNTIF(synced_at IS NOT NULL) AS tables_reporting" in body

    def test_stage_skew_is_exposed_for_diagnosis(self, sql):
        """Hours of skew means a stage failed and its table was never
        rewritten — the case the MIN headline is hiding from the reader."""
        body = sql[self.NAME]
        assert (
            "TIMESTAMP_DIFF(r.newest_synced_at, r.oldest_synced_at, MINUTE)" in body
        )
        assert "AS stage_skew_minutes" in body

    def test_it_is_one_row(self, sql):
        """Aggregates with no GROUP BY. A grouped version would break every
        scorecard pointed at it."""
        body = sql[self.NAME]
        assert "GROUP BY" not in body


class TestBaseViewBillableRevenue:
    """billable_revenue on v_user_daily_billable_hours_base.

    Added as a purely additive column: v_user_weekly_billable_hours reads the
    base with an explicit column list, so a new column cannot disturb it.
    That property is load-bearing and is asserted below, because the dependent
    is a UNION ALL -- a SELECT * there would make any future column addition
    change one branch's shape and break the union outright.
    """

    BASE = "v_user_daily_billable_hours_base"
    DEPENDENT = "v_user_weekly_billable_hours"

    def test_column_exists(self, sql):
        assert "AS billable_revenue" in sql[self.BASE]

    def test_revenue_is_derived_from_minutes_not_the_rounded_hours_column(self, sql):
        """One definition of revenue across the warehouse.

        timelogs.hours is stored pre-rounded, so SUM(hours * rate) would be a
        second, slightly different revenue that would not tie back to
        v_timelog_detail.billable_amount or v_client_month.billable_revenue.
        """
        body = sql[self.BASE]
        assert "SUM((tl.minutes / 60) * tl.billable_rate) AS billable_revenue" in body
        assert "SUM(tl.hours * tl.billable_rate)" not in body

    def test_matches_the_billable_amount_expression_in_v_timelog_detail(self, sql):
        """The two must not drift into different definitions of revenue."""
        assert "(tl.minutes / 60) * tl.billable_rate" in sql[self.BASE]
        assert "(tl.minutes / 60) * tl.billable_rate" in sql["v_timelog_detail"]

    def test_revenue_is_billable_only(self, sql):
        """The WHERE is what makes the IF unnecessary; losing it would let
        non-billable work contribute revenue."""
        assert "WHERE tl.is_billable = TRUE" in sql[self.BASE]

    def test_the_existing_hours_column_is_untouched(self, sql):
        """Rebasing hours onto minutes would silently shift every number
        v_user_weekly_billable_hours has ever reported."""
        assert "SUM(tl.hours) AS hours" in sql[self.BASE]

    def test_dependent_reads_an_explicit_column_list(self, sql):
        """This is why adding a column here is safe.

        The dependent is a UNION ALL; a SELECT * would change one branch's
        column count the moment anyone adds a field to the base.
        """
        body = sql[self.DEPENDENT]
        assert "SELECT base.user_id, base.day_bucket, base.week_start, base.hours" in body
        assert "SELECT *" not in body

    def test_dependent_did_not_pick_up_the_new_column(self, sql):
        """Additive means additive: the weekly view's output is unchanged."""
        assert "base.billable_revenue" not in sql[self.DEPENDENT]
