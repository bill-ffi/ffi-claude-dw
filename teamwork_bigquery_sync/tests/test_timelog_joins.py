"""Every time entry must reach its project and, when it has one, its task.

Agreed objective (2026-10-03): every project and task that 2026 time is
logged against is in BigQuery. The task scope is argued to cover that; this
check measures it, because an untested assumption about scope is how 9,014
entries on archived projects went missing until 2026-10-02.
"""

import inspect
import logging

import bigquery_sync
import sync


class FakeJob:
    def __init__(self, rows, raises):
        self.rows, self.raises = rows, raises

    def result(self):
        if self.raises:
            raise self.raises
        return self.rows


class FakeClient:
    """Answers the totals query, then the by-project and task-id queries."""

    def __init__(self, totals=(0, 0.0, 0, 0.0, 0, 0.0), by_project=(), task_ids=(), raises=None):
        self.answers = [[totals], list(by_project), list(task_ids)]
        self.raises, self.sql = raises, []

    def query(self, sql):
        self.sql.append(sql)
        return FakeJob(self.answers[len(self.sql) - 1], self.raises)


class Cfg:
    gcp_project_id, bq_dataset = "proj", "ds"


class TestCheckTimelogJoins:
    def test_reports_each_gap(self):
        c = FakeClient(totals=(2, 1.5, 7, 4.4, 30, 12.0),
                       by_project=[(1440795, "GRPN Payroll (2026)", 7)],
                       task_ids=[(49992650, 6, 3.0), (50097313, 1, 1.4)])
        assert bigquery_sync.check_timelog_joins(c, "proj", "ds") == {
            "missing_project": {"entries": 2, "hours": 1.5},
            "missing_task": {"entries": 7, "hours": 4.4, "by_project": [
                {"project_id": 1440795, "project_name": "GRPN Payroll (2026)", "entries": 7}],
                "task_ids": [{"task_id": 49992650, "entries": 6, "hours": 3.0},
                             {"task_id": 50097313, "entries": 1, "hours": 1.4}]},
            "no_task": {"entries": 30, "hours": 12.0},
        }
        # Every missing id, not a top-N, so each can be looked up.
        assert "GROUP BY tl.task_id" in c.sql[2]
        assert f"LIMIT {bigquery_sync.MISSING_TASK_ID_LIMIT}" in c.sql[2]

    def test_clean_skips_the_breakdown(self):
        c = FakeClient()
        r = bigquery_sync.check_timelog_joins(c, "proj", "ds")
        assert r["missing_task"] == {"entries": 0, "hours": 0.0, "by_project": [], "task_ids": []}
        assert len(c.sql) == 1

    def test_a_failed_check_is_none_not_a_false_clean(self):
        c = FakeClient(raises=RuntimeError("permission denied"))
        assert bigquery_sync.check_timelog_joins(c, "proj", "ds") is None

    def test_joins_are_anti_joins_over_all_history(self):
        c = FakeClient(totals=(0, 0.0, 1, 0.5, 0, 0.0), by_project=[(1, "x", 1)])
        bigquery_sync.check_timelog_joins(c, "proj", "ds")
        totals = c.sql[0]
        assert "LEFT JOIN `proj.ds.projects` p ON p.project_id = tl.project_id" in totals
        # DISTINCT so a duplicated task row can never inflate the counts.
        assert "LEFT JOIN (SELECT DISTINCT task_id FROM `proj.ds.tasks`) tk" in totals
        assert "COUNTIF(p.project_id IS NULL)" in totals
        # A missing task is one the entry names but tasks lacks; an entry with
        # no task at all is counted separately, never as missing.
        assert "COUNTIF(tl.task_id IS NOT NULL AND tk.task_id IS NULL)" in totals
        assert "COUNTIF(tl.task_id IS NULL)" in totals
        assert "log_date" not in totals and "@window" not in totals
        assert "WHERE tl.task_id IS NOT NULL AND tk.task_id IS NULL" in c.sql[1]


class FakeTw:
    """task_exists(): True for ids in `alive`, False for `gone`, raises otherwise."""

    def __init__(self, alive=(), gone=()):
        self.alive, self.gone, self.asked = set(alive), set(gone), []

    def task_exists(self, task_id):
        self.asked.append(task_id)
        if task_id in self.alive:
            return True
        if task_id in self.gone:
            return False
        raise RuntimeError("HTTP 503")


class TestJoinReport:
    def run(self, monkeypatch, caplog, result, tw=None):
        monkeypatch.setattr(bigquery_sync, "check_timelog_joins", lambda *a: result)
        with caplog.at_level(logging.WARNING):
            return sync.timelog_join_report(None, Cfg(), tw or FakeTw())

    def gaps(self, project=0, task_ids=(), no_task=0):
        ids = [{"task_id": t, "entries": n, "hours": 1.0} for t, n in task_ids]
        return {"missing_project": {"entries": project, "hours": 1.0},
                "missing_task": {"entries": sum(n for _, n in task_ids), "hours": 1.0,
                                 "by_project": [], "task_ids": ids},
                "no_task": {"entries": no_task, "hours": 1.0}}

    def test_missing_project_warns(self, monkeypatch, caplog):
        self.run(monkeypatch, caplog, self.gaps(project=3))
        assert "have no row in projects" in caplog.text

    def test_a_task_teamwork_still_has_warns(self, monkeypatch, caplog):
        # The pull missed it: a real scope gap.
        r = self.run(monkeypatch, caplog, self.gaps(task_ids=[(5, 3)]), FakeTw(alive={5}))
        assert r["missing_task"]["still_in_teamwork"] == {"entries": 3, "hours": 1.0, "task_ids": [5]}
        assert "the tasks pull missed them" in caplog.text

    def test_a_task_teamwork_no_longer_has_is_reported_not_warned(self, monkeypatch, caplog):
        # 404: nothing could recover it, so warning would fire every run.
        r = self.run(monkeypatch, caplog, self.gaps(task_ids=[(49992650, 7)]),
                     FakeTw(gone={49992650}))
        assert r["missing_task"]["gone_from_teamwork"] == {
            "entries": 7, "hours": 1.0, "task_ids": [49992650]}
        assert r["missing_task"]["still_in_teamwork"]["entries"] == 0
        assert caplog.records == []

    def test_a_lookup_that_fails_is_unchecked_and_warns(self, monkeypatch, caplog):
        r = self.run(monkeypatch, caplog, self.gaps(task_ids=[(9, 2)]), FakeTw())
        assert r["missing_task"]["unchecked"]["task_ids"] == [9]
        assert "could not be looked up" in caplog.text

    def test_each_missing_task_is_asked_about_once(self, monkeypatch, caplog):
        tw = FakeTw(alive={1}, gone={2, 3})
        r = self.run(monkeypatch, caplog, self.gaps(task_ids=[(1, 1), (2, 4), (3, 2)]), tw)
        assert tw.asked == [1, 2, 3]
        assert r["missing_task"]["gone_from_teamwork"]["entries"] == 6
        assert "task_ids" not in r["missing_task"]  # replaced by the buckets

    def test_untasked_time_is_reported_not_warned(self, monkeypatch, caplog):
        # Policy, not data loss: warning on it would fire every run.
        assert self.run(monkeypatch, caplog, self.gaps(no_task=50))["no_task"]["entries"] == 50
        assert caplog.records == []

    def test_unrunnable_check_is_quiet(self, monkeypatch, caplog):
        assert self.run(monkeypatch, caplog, None) is None

    def test_both_runs_that_write_timelogs_report_it(self):
        for fn in (sync.run_full_sync, sync.run_backfill):
            source = inspect.getsource(fn)
            assert "joins = timelog_join_report(bq_client, cfg, tw_client)" in source, fn.__name__
            assert '"timelog_joins": joins,' in source, fn.__name__
