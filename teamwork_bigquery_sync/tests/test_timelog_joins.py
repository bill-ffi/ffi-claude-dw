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
    """Answers the totals query, then the by-project query."""

    def __init__(self, totals=(0, 0.0, 0, 0.0, 0, 0.0), by_project=(), raises=None):
        self.answers = [[totals], list(by_project)]
        self.raises, self.sql = raises, []

    def query(self, sql):
        self.sql.append(sql)
        return FakeJob(self.answers[len(self.sql) - 1], self.raises)


class Cfg:
    gcp_project_id, bq_dataset = "proj", "ds"


class TestCheckTimelogJoins:
    def test_reports_each_gap(self):
        c = FakeClient(totals=(2, 1.5, 7, 4.4, 30, 12.0),
                       by_project=[(1440795, "GRPN Payroll (2026)", 7)])
        assert bigquery_sync.check_timelog_joins(c, "proj", "ds") == {
            "missing_project": {"entries": 2, "hours": 1.5},
            "missing_task": {"entries": 7, "hours": 4.4, "by_project": [
                {"project_id": 1440795, "project_name": "GRPN Payroll (2026)", "entries": 7}]},
            "no_task": {"entries": 30, "hours": 12.0},
        }

    def test_clean_skips_the_breakdown(self):
        c = FakeClient()
        r = bigquery_sync.check_timelog_joins(c, "proj", "ds")
        assert r["missing_task"] == {"entries": 0, "hours": 0.0, "by_project": []}
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


class TestJoinReport:
    def run(self, monkeypatch, caplog, result):
        monkeypatch.setattr(bigquery_sync, "check_timelog_joins", lambda *a: result)
        with caplog.at_level(logging.WARNING):
            return sync.timelog_join_report(None, Cfg())

    def gaps(self, project=0, task=0, no_task=0):
        return {"missing_project": {"entries": project, "hours": 1.0},
                "missing_task": {"entries": task, "hours": 1.0, "by_project": []},
                "no_task": {"entries": no_task, "hours": 1.0}}

    def test_missing_project_warns(self, monkeypatch, caplog):
        self.run(monkeypatch, caplog, self.gaps(project=3))
        assert "have no row in projects" in caplog.text

    def test_missing_task_warns(self, monkeypatch, caplog):
        self.run(monkeypatch, caplog, self.gaps(task=3))
        assert "name a task that is not in tasks" in caplog.text

    def test_untasked_time_is_reported_not_warned(self, monkeypatch, caplog):
        # Policy, not data loss: warning on it would fire every run.
        assert self.run(monkeypatch, caplog, self.gaps(no_task=50))["no_task"]["entries"] == 50
        assert caplog.records == []

    def test_unrunnable_check_is_quiet(self, monkeypatch, caplog):
        assert self.run(monkeypatch, caplog, None) is None

    def test_both_runs_that_write_timelogs_report_it(self):
        for fn in (sync.run_full_sync, sync.run_backfill):
            source = inspect.getsource(fn)
            assert "joins = timelog_join_report(bq_client, cfg)" in source, fn.__name__
            assert '"timelog_joins": joins,' in source, fn.__name__
