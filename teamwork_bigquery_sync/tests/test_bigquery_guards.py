"""bigquery_sync.py — the guards on destructive writes.

projects/tasks/users are WRITE_TRUNCATE, so a bad pull does not corrupt
them, it erases them, and there is no prior version to fall back to.
"""

import pytest
from google.api_core.exceptions import NotFound

import bigquery_sync


class FakeTable:
    def __init__(self, num_rows):
        self.num_rows = num_rows


class FakeLoadJob:
    errors = None

    def result(self):
        return None


class FakeBQClient:
    def __init__(self, existing_rows):
        self._existing = existing_rows
        self.loaded = None

    def get_table(self, ref):
        if self._existing is None:
            raise NotFound("no such table")
        return FakeTable(self._existing)

    def load_table_from_json(self, rows, ref, job_config=None):
        self.loaded = list(rows)
        return FakeLoadJob()


class FakeDatasetRef:
    def table(self, name):
        return f"ref:{name}"


def attempt(existing, new_row_count, allow_shrink=False):
    client = FakeBQClient(existing)
    rows = [{"i": i} for i in range(new_row_count)]
    written = bigquery_sync.truncate_and_load(
        client, FakeDatasetRef(), "tasks", [], rows, allow_shrink=allow_shrink
    )
    return written, client


class TestEmptyLoadRefused:
    def test_zero_rows_over_a_populated_table_is_refused(self):
        with pytest.raises(bigquery_sync.EmptyLoadRefused):
            attempt(37226, 0)

    def test_allow_shrink_does_not_override_it(self):
        # An empty pull is never a legitimate result for these tables.
        with pytest.raises(bigquery_sync.EmptyLoadRefused):
            attempt(37226, 0, allow_shrink=True)

    def test_refused_even_for_a_brand_new_table(self):
        with pytest.raises(bigquery_sync.EmptyLoadRefused):
            attempt(None, 0)

    def test_nothing_is_written_when_refused(self):
        client = FakeBQClient(100)
        with pytest.raises(bigquery_sync.EmptyLoadRefused):
            bigquery_sync.truncate_and_load(client, FakeDatasetRef(), "tasks", [], [])
        assert client.loaded is None, "the table must keep its previous contents"


class TestSuspiciousShrinkRefused:
    def test_a_partial_pull_is_refused(self):
        with pytest.raises(bigquery_sync.SuspiciousShrinkRefused):
            attempt(37226, 12000)

    def test_exactly_at_the_ratio_is_allowed(self):
        # The guard is `ratio < MIN_REPLACE_ROWS_RATIO`, so retaining exactly
        # half the rows passes and one row fewer does not. Asserted explicitly
        # because the boundary is otherwise easy to misremember in either
        # direction.
        boundary = int(37226 * bigquery_sync.MIN_REPLACE_ROWS_RATIO)
        assert boundary == 18613
        written, _ = attempt(37226, boundary)
        assert written == boundary

    def test_one_row_below_the_ratio_is_refused(self):
        boundary = int(37226 * bigquery_sync.MIN_REPLACE_ROWS_RATIO)
        with pytest.raises(bigquery_sync.SuspiciousShrinkRefused):
            attempt(37226, boundary - 1)

    def test_ordinary_fluctuation_passes(self):
        written, _ = attempt(37226, 30000)
        assert written == 30000

    def test_a_steady_state_run_passes(self):
        written, _ = attempt(37226, 37226)
        assert written == 37226

    def test_growth_passes(self):
        written, _ = attempt(1000, 40000)
        assert written == 40000

    def test_allow_shrink_permits_a_deliberate_scope_reduction(self):
        written, _ = attempt(37226, 12000, allow_shrink=True)
        assert written == 12000

    def test_the_message_names_the_override(self):
        with pytest.raises(bigquery_sync.SuspiciousShrinkRefused) as exc:
            attempt(37226, 12000)
        assert "--allow-shrink" in str(exc.value)


class TestNewOrEmptyTables:
    def test_a_missing_table_has_no_prior_count_to_compare(self):
        written, _ = attempt(None, 1889)
        assert written == 1889

    def test_an_existing_empty_table_accepts_a_first_load(self):
        written, _ = attempt(0, 1889)
        assert written == 1889


class TestExistingRowCount:
    def test_reads_metadata_rather_than_querying(self):
        assert bigquery_sync._existing_row_count(FakeBQClient(42), "ref") == 42

    def test_returns_none_for_a_missing_table(self):
        assert bigquery_sync._existing_row_count(FakeBQClient(None), "ref") is None


class TestUnresolvedTimelogUsers:
    """Time whose user id has no row in `users` reads with a blank name in
    every view. Two former staff's 857 timelogs sat that way for months while
    every run reported success; this check surfaces it in RUN_SUMMARY."""

    class FakeJob:
        def __init__(self, rows, raises):
            self.rows, self.raises = rows, raises

        def result(self):
            if self.raises:
                raise self.raises
            return self.rows

    class FakeClient:
        def __init__(self, rows=(), raises=None):
            self.rows, self.raises, self.sql = list(rows), raises, None

        def query(self, sql):
            self.sql = sql
            return TestUnresolvedTimelogUsers.FakeJob(self.rows, self.raises)

    def test_reports_each_missing_user(self):
        c = self.FakeClient(rows=[(700802, 801, 392.3, "2026-07-09")])
        assert bigquery_sync.list_unresolved_timelog_users(c, "p", "d") == [
            {"user_id": 700802, "entries": 801, "hours": 392.3, "last_entry": "2026-07-09"}
        ]

    def test_everyone_resolving_is_an_empty_list(self):
        assert bigquery_sync.list_unresolved_timelog_users(self.FakeClient(), "p", "d") == []

    def test_a_failed_check_is_none_not_a_false_clean(self):
        c = self.FakeClient(raises=RuntimeError("permission denied"))
        assert bigquery_sync.list_unresolved_timelog_users(c, "p", "d") is None

    def test_is_an_anti_join_over_all_history(self):
        # A departed person's last entry is in the past by definition, so the
        # check must not be restricted to this run's timelog window.
        c = self.FakeClient()
        bigquery_sync.list_unresolved_timelog_users(c, "proj", "ds")
        assert "LEFT JOIN `proj.ds.users` u ON u.user_id = tl.user_id" in c.sql
        assert "WHERE u.user_id IS NULL" in c.sql
        assert "log_date >=" not in c.sql and "@window" not in c.sql
