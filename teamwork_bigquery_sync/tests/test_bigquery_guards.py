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
