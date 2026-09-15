"""Fill-rate reporting for columns that should never be empty.

The incident: tasks.web_link and projects.web_link were NULL on 100% of rows
for months. They read a payload key Teamwork v3 does not return, a missing
dict key yields None, and every run reported success. Nothing in the pipeline
looked at whether a column it had just written contained anything.

See README "Known gaps".
"""

import pytest

import schemas
import sync
import transform


class TestIsPopulated:
    """Zero and False are values, not absences."""

    @pytest.mark.parametrize("value", [1, 0, 0.0, False, True, "x", " x ", [1], {"a": 1}])
    def test_populated(self, value):
        assert transform._is_populated(value) is True

    @pytest.mark.parametrize("value", [None, "", "   ", [], {}, set()])
    def test_empty(self, value):
        assert transform._is_populated(value) is False

    def test_zero_minutes_is_not_a_missing_value(self):
        """Counting 0 as empty would flag every zero-minute timelog."""
        assert transform._is_populated(0) is True

    def test_false_is_not_a_missing_value(self):
        """Counting False as empty would flag every non-billable entry."""
        assert transform._is_populated(False) is True


class TestFillRates:
    def test_rate_is_the_populated_fraction(self):
        rows = [{"a": 1}, {"a": None}, {"a": 2}, {"a": 4}]
        assert transform.fill_rates(rows, ("a",)) == {"a": 0.75}

    def test_column_absent_from_every_row_reads_zero(self):
        """The shape of the actual bug: the key is simply never set."""
        rows = [{"a": 1}, {"a": 2}]
        assert transform.fill_rates(rows, ("web_link",)) == {"web_link": 0.0}

    def test_no_rows_returns_empty_not_zeroes(self):
        """A rate over zero rows is undefined.

        Reporting 0.0 would be indistinguishable from a column that failed to
        populate, and would fire the warning on every empty pull. Empty pulls
        are already caught by the write guards in bigquery_sync.
        """
        assert transform.fill_rates([], ("a", "b")) == {}


class TestUnderfilledColumns:
    def test_flags_only_below_the_minimum(self):
        rates = {"a": 1.0, "b": 0.999, "c": 0.0}
        assert transform.underfilled_columns(rates, 1.0) == ["b", "c"]

    def test_full_columns_are_never_flagged(self):
        assert transform.underfilled_columns({"a": 1.0, "b": 1.0}, 1.0) == []

    def test_output_is_sorted_for_stable_summaries(self):
        rates = {"z": 0.0, "a": 0.0, "m": 0.0}
        assert transform.underfilled_columns(rates, 1.0) == ["a", "m", "z"]


class TestDeclaredColumnsAreReal:
    """The check can only work if the declared names match the schema.

    A typo'd column name reads 0.0 forever and warns on every run, which is
    exactly the cry-wolf failure that would get the whole warning ignored.
    """

    SCHEMAS = {
        schemas.PROJECTS_TABLE: schemas.PROJECTS_SCHEMA,
        schemas.TASKS_TABLE: schemas.TASKS_SCHEMA,
        schemas.USERS_TABLE: schemas.USERS_SCHEMA,
        schemas.TIMELOGS_TABLE: schemas.TIMELOGS_SCHEMA,
    }

    def test_every_declared_table_is_a_real_table(self):
        assert set(schemas.ALWAYS_POPULATED_COLUMNS) <= set(self.SCHEMAS)

    def test_every_declared_column_exists_in_its_schema(self):
        for table, columns in schemas.ALWAYS_POPULATED_COLUMNS.items():
            real = {field.name for field in self.SCHEMAS[table]}
            unknown = sorted(set(columns) - real)
            assert unknown == [], f"{table}: {unknown}"

    def test_known_sparse_columns_are_not_declared(self):
        """Listing a legitimately-sparse column warns on every run.

        category_name sits at 1,885/1,890 and activity, estimate_minutes,
        due_date and tasklist_name are all optional by design. A warning that
        always fires is a warning nobody reads.
        """
        sparse = {
            schemas.PROJECTS_TABLE: ("category_name", "health", "budget_capacity"),
            schemas.TASKS_TABLE: (
                "activity", "estimate_minutes", "due_date", "tasklist_name",
                "parent_task_id", "sequence_id", "description",
            ),
            schemas.TIMELOGS_TABLE: ("task_id", "billable_rate", "description"),
        }
        for table, columns in sparse.items():
            declared = set(schemas.ALWAYS_POPULATED_COLUMNS.get(table, ()))
            overlap = sorted(declared & set(columns))
            assert overlap == [], f"{table}: {overlap}"

    def test_web_link_is_declared_for_both_tables_that_carry_it(self):
        """The regression this whole feature exists for."""
        assert "web_link" in schemas.ALWAYS_POPULATED_COLUMNS[schemas.PROJECTS_TABLE]
        assert "web_link" in schemas.ALWAYS_POPULATED_COLUMNS[schemas.TASKS_TABLE]


class TestFillRateReport:
    GOOD = [
        {
            "task_id": 1, "project_id": 7, "name": "Reconcile",
            "status": "new", "web_link": "https://x/app/tasks/1",
            "created_at": "2026-09-01T00:00:00Z",
        }
    ]

    def test_healthy_rows_report_no_underfill(self):
        report = sync.fill_rate_report(schemas.TASKS_TABLE, self.GOOD)
        assert report["underfilled_columns"] == []
        assert report["fill_rates"]["web_link"] == 1.0

    def test_the_web_link_regression_is_caught(self, caplog):
        """A column NULL on every row must be flagged and logged."""
        rows = [dict(self.GOOD[0], web_link=None)]
        with caplog.at_level("WARNING"):
            report = sync.fill_rate_report(schemas.TASKS_TABLE, rows)
        assert report["underfilled_columns"] == ["web_link"]
        assert report["fill_rates"]["web_link"] == 0.0
        assert "web_link" in caplog.text

    def test_report_never_raises_on_an_unknown_table(self):
        """A stage reporting a table with no declared columns is a no-op."""
        assert sync.fill_rate_report("not_a_table", self.GOOD) == {
            "fill_rates": {},
            "underfilled_columns": [],
        }

    def test_report_is_not_fatal(self, caplog):
        """Like the orphaned-views check: a signal for a human, never a failure.

        A column that stops populating is not a reason to abandon a sync that
        is otherwise writing good data.
        """
        rows = [dict(self.GOOD[0], web_link=None, name=None)]
        with caplog.at_level("WARNING"):
            report = sync.fill_rate_report(schemas.TASKS_TABLE, rows)
        assert report["underfilled_columns"] == ["name", "web_link"]

    def test_empty_stage_reports_nothing_rather_than_warning(self, caplog):
        with caplog.at_level("WARNING"):
            report = sync.fill_rate_report(schemas.TASKS_TABLE, [])
        assert report == {"fill_rates": {}, "underfilled_columns": []}
        assert "fill rate" not in caplog.text
