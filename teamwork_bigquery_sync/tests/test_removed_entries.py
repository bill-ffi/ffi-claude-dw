"""Guard against a timelogs replace silently deleting entries the pull missed.

Each timelogs window is a delete-and-reinsert. Until 2026-10-02 time.json
omitted every entry on an archived project, so time loaded while a project was
open was deleted on the next re-pull after it was archived: 9,014 of 37,673
entries since January, while every run reported success. Every existing check
compared our rows to the same short pull, so none could see it. This one
compares the pull against what the table held before the replace.
"""

from datetime import date

import bigquery_sync
import sync

START, LAST = date(2026, 9, 1), date(2026, 9, 30)


def report(stored, pulled_ids):
    rows = [{"timelog_id": tid} for tid in pulled_ids]
    return sync.removed_entries_report(stored, rows, START, LAST)


class TestRemovedEntriesReport:
    def test_names_entries_the_pull_lacks_and_which_were_archived(self, caplog):
        stored = {1: (10, False), 2: (10, False), 3: (20, True)}
        with caplog.at_level("WARNING"):
            r = report(stored, [1, 4])
        assert r["previously_stored"] == 3
        assert r["removed"] == 2
        assert r["removed_on_archived_projects"] == 1
        assert "ARCHIVED" in caplog.records[0].getMessage()

    def test_additions_do_not_mask_a_loss(self):
        # In an open month the pull grows; a net count would read as healthy.
        stored = {1: (10, True), 2: (10, False)}
        r = report(stored, [2] + list(range(100, 200)))
        assert r["removed"] == 1 and r["removed_on_archived_projects"] == 1

    def test_nothing_removed_is_quiet(self, caplog):
        with caplog.at_level("WARNING"):
            r = report({1: (10, True), 2: (10, False)}, [1, 2, 3])
        assert r["removed"] == 0 and r["removed_on_archived_projects"] == 0
        assert r["removed_by_project"] == []
        assert caplog.records == []

    def test_an_ordinary_deletion_is_reported_but_not_warned(self, caplog):
        # Someone deleting one entry in Teamwork is normal; warning on it
        # would fire every run and train everyone to ignore the warning.
        stored = {tid: (10, False) for tid in range(1, 101)}
        with caplog.at_level("WARNING"):
            r = report(stored, range(2, 101))
        assert r["removed"] == 1
        assert caplog.records == []

    def test_a_large_unexplained_loss_warns(self, caplog):
        stored = {tid: (10, False) for tid in range(1, 101)}
        with caplog.at_level("WARNING"):
            r = report(stored, range(11, 101))
        assert r["removed"] == 10 and r["removed_on_archived_projects"] == 0
        assert "deletes 10 of 100" in caplog.records[0].getMessage()

    def test_groups_removals_by_project_largest_first(self):
        stored = {1: (10, True), 2: (20, True), 3: (20, True), 4: (30, False)}
        r = report(stored, [4])
        assert r["removed_by_project"] == [
            {"project_id": 20, "entries": 2},
            {"project_id": 10, "entries": 1},
        ]

    def test_unreadable_stored_rows_is_none_not_a_false_clean(self):
        assert report(None, [1, 2]) is None


class TestStoredTimelogsInWindow:
    class FakeJob:
        def __init__(self, rows, raises):
            self.rows, self.raises = rows, raises

        def result(self):
            if self.raises:
                raise self.raises
            return self.rows

    class FakeClient:
        def __init__(self, rows=(), raises=None):
            self.rows, self.raises, self.sql, self.params = list(rows), raises, None, None

        def query(self, sql, job_config=None):
            self.sql = sql
            self.params = {p.name: p.value for p in job_config.query_parameters}
            return TestStoredTimelogsInWindow.FakeJob(self.rows, self.raises)

    def test_reads_the_window_with_each_entrys_archived_state(self):
        c = self.FakeClient(rows=[(1, 10, True), (2, 11, None)])
        got = bigquery_sync.stored_timelogs_in_window(c, "p", "d", START, date(2026, 10, 1))
        assert got == {1: (10, True), 2: (11, False)}
        assert "LEFT JOIN `p.d.projects` p ON p.project_id = tl.project_id" in c.sql
        assert "(p.archived_at IS NOT NULL) AS archived" in c.sql
        assert c.params == {"window_start": START, "window_end": date(2026, 10, 1)}

    def test_a_failed_read_is_none(self):
        c = self.FakeClient(raises=RuntimeError("permission denied"))
        assert bigquery_sync.stored_timelogs_in_window(c, "p", "d", START, date(2026, 10, 1)) is None


class FakeTw:
    def list_timelogs(self, start, end):
        return [{"id": 1, "timeLogged": "2026-09-01T14:00:00Z", "minutes": 60}]

    def count_timelogs_with_tag(self, start, end, tag_id):
        return 0


class TestRunsBeforeEveryReplace:
    def test_stored_rows_are_read_before_the_replace_deletes_them(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            bigquery_sync, "stored_timelogs_in_window",
            lambda *a: calls.append("read") or {1: (10, False), 2: (10, True)},
        )
        monkeypatch.setattr(
            bigquery_sync, "replace_timelogs_window",
            lambda client, project, dataset, rows, *a: calls.append("replace") or len(rows),
        )
        result = sync.sync_timelogs_for_window(FakeTw(), None, "p", "d", START, date(2026, 10, 1))
        assert calls == ["read", "replace"]
        assert result["removed_entries"]["removed"] == 1
        assert result["removed_entries"]["removed_on_archived_projects"] == 1
