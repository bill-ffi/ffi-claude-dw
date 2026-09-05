"""sync.py — task scope selection and the timelogs window.

These two decide how many rows land in BigQuery, and both were wrong at some
point today. The numbers asserted below were measured against the live
account on 2026-09-04 (see README "Known gaps").
"""

from datetime import date, datetime

import pytest

import sync


class TestShiftMonth:
    @pytest.mark.parametrize("year,month,delta,expected", [
        (2026, 1, -1, (2025, 12)),      # back across a year boundary
        (2026, 1, -2, (2025, 11)),
        (2026, 12, 1, (2027, 1)),       # forward across one
        (2026, 3, -3, (2025, 12)),
        (2026, 6, 0, (2026, 6)),
        (2026, 1, -12, (2025, 1)),
    ])
    def test_shifts_across_year_boundaries(self, year, month, delta, expected):
        assert sync._shift_month(year, month, delta) == expected


class TestMonthBounds:
    def test_ordinary_month(self):
        assert sync.month_bounds(2026, 8) == (date(2026, 8, 1), date(2026, 9, 1))

    def test_december_rolls_into_the_next_year(self):
        assert sync.month_bounds(2026, 12) == (date(2026, 12, 1), date(2027, 1, 1))


class TestTimelogWindow:
    """The window was current-month-only, so a closed month froze the moment
    the calendar rolled over and retroactive entries never arrived."""

    @pytest.fixture
    def frozen(self, monkeypatch):
        def _freeze(when):
            class FrozenDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return when.replace(tzinfo=tz)
            monkeypatch.setattr(sync, "datetime", FrozenDatetime)
        return _freeze

    @pytest.mark.parametrize("today,expected", [
        (datetime(2026, 9, 4),  (date(2026, 8, 1), date(2026, 10, 1))),
        (datetime(2026, 1, 5),  (date(2025, 12, 1), date(2026, 2, 1))),   # into last year
        (datetime(2026, 1, 31), (date(2025, 12, 1), date(2026, 2, 1))),
        (datetime(2026, 12, 15), (date(2026, 11, 1), date(2027, 1, 1))),  # into next year
        (datetime(2027, 1, 1),  (date(2026, 12, 1), date(2027, 2, 1))),
    ])
    def test_spans_the_current_month_plus_one(self, frozen, today, expected):
        frozen(today)
        assert sync.timelog_window("America/New_York") == expected

    def test_window_always_starts_and_ends_on_a_first(self, frozen):
        frozen(datetime(2026, 9, 4))
        start, end = sync.timelog_window("America/New_York")
        assert start.day == 1 and end.day == 1

    def test_months_back_zero_reproduces_the_old_behaviour(self, frozen):
        frozen(datetime(2026, 9, 4))
        assert sync.timelog_window("America/New_York", months_back=0) == (
            date(2026, 9, 1), date(2026, 10, 1)
        )

    def test_months_back_two_spans_three_months(self, frozen):
        frozen(datetime(2026, 9, 4))
        assert sync.timelog_window("America/New_York", months_back=2) == (
            date(2026, 7, 1), date(2026, 10, 1)
        )

    def test_the_month_tail_gap_is_closed(self, frozen):
        # The day after the month rolls over, the whole previous month must
        # still be inside the window — that is the entire point of the change.
        frozen(datetime(2026, 10, 1))
        start, end = sync.timelog_window("America/New_York")
        assert start <= date(2026, 9, 30) < end

    def test_but_only_for_months_back_months(self, frozen):
        # Documented residual gap: a very late retroactive edit still misses.
        frozen(datetime(2026, 11, 1))
        start, end = sync.timelog_window("America/New_York")
        assert not (start <= date(2026, 9, 30) < end)


class TestMonthsInWindow:
    def test_labels_every_month_the_window_spans(self):
        assert sync._months_in_window(date(2026, 8, 1), date(2026, 10, 1)) == ["2026-08", "2026-09"]

    def test_single_month_window(self):
        assert sync._months_in_window(date(2026, 8, 1), date(2026, 9, 1)) == ["2026-08"]

    def test_spanning_a_year_boundary(self):
        assert sync._months_in_window(date(2026, 12, 1), date(2027, 2, 1)) == ["2026-12", "2027-01"]


class TestParseBackfillMonths:
    def test_sorts_and_deduplicates(self):
        assert sync.parse_backfill_months("2026-03,2026-01,2026-01") == [(2026, 1), (2026, 3)]

    def test_tolerates_whitespace_and_empty_entries(self):
        assert sync.parse_backfill_months(" 2026-01 , ,2026-02 ") == [(2026, 1), (2026, 2)]

    @pytest.mark.parametrize("bad", ["2026-13", "2026-00", "26-01", "2026/01", "January", ""])
    def test_rejects_malformed_input_by_name(self, bad):
        with pytest.raises(ValueError):
            sync.parse_backfill_months(bad)


class TestSelectTaskPullProjects:
    """Scope = active projects, plus anything archived on/after the cutoff."""

    def _rows(self):
        return [
            {"project_id": 1, "status": "active",   "archived_at": None},
            {"project_id": 2, "status": "inactive", "archived_at": None},
            {"project_id": 3, "status": "active",   "archived_at": "2023-05-01T00:00:00Z"},
            {"project_id": 4, "status": "inactive", "archived_at": "2026-03-04T00:00:00Z"},
            {"project_id": 5, "status": "active",   "archived_at": "2026-01-01T00:00:00Z"},
            {"project_id": 6, "status": None,       "archived_at": None},
            {"project_id": 7, "status": "ACTIVE",   "archived_at": "0001-01-01T00:00:00Z"},
        ]

    def test_selects_active_and_recently_archived(self):
        ids, _ = sync.select_task_pull_projects(self._rows())
        assert ids == {1, 4, 5, 7}

    def test_archived_exactly_on_the_cutoff_is_included(self):
        ids, _ = sync.select_task_pull_projects(self._rows())
        assert 5 in ids

    def test_archived_before_the_cutoff_is_excluded(self):
        ids, _ = sync.select_task_pull_projects(self._rows())
        assert 3 not in ids

    def test_status_matching_is_case_insensitive(self):
        ids, _ = sync.select_task_pull_projects(self._rows())
        assert 7 in ids

    def test_go_sentinel_project_counts_as_never_archived(self):
        # Project 7 carries the zero-time sentinel; a string compare would
        # have dropped it as "archived in year 1".
        _, breakdown = sync.select_task_pull_projects(self._rows())
        assert breakdown["in_scope_active"] == 2

    def test_breakdown_accounts_for_every_project_exactly_once(self):
        rows = self._rows()
        _, b = sync.select_task_pull_projects(rows)
        counted = (b["in_scope_active"] + b["in_scope_archived_on_or_after_cutoff"]
                   + b["excluded_archived_before_cutoff"] + b["excluded_not_active"])
        assert counted == len(rows) == b["projects_considered"]

    def test_breakdown_names_the_excluded_statuses(self):
        _, b = sync.select_task_pull_projects(self._rows())
        assert b["excluded_statuses"] == {"inactive": 1, "(null)": 1}

    def test_widening_the_status_set_restores_the_old_behaviour(self):
        ids, _ = sync.select_task_pull_projects(
            self._rows(), active_statuses={"active", "inactive", ""}
        )
        assert ids == {1, 2, 4, 5, 6, 7}

    def test_status_filter_is_a_noop_when_status_tracks_archival(self):
        # Measured on this account: active <=> never archived. The status
        # gate must therefore change nothing for real-shaped data.
        real_shaped = [
            {"project_id": i, "status": "active", "archived_at": None} for i in range(3)
        ] + [
            {"project_id": 10 + i, "status": "inactive", "archived_at": "2026-03-01T00:00:00Z"}
            for i in range(2)
        ]
        strict, _ = sync.select_task_pull_projects(real_shaped)
        loose, _ = sync.select_task_pull_projects(
            real_shaped, active_statuses={"active", "inactive"}
        )
        assert strict == loose

    def test_empty_input_yields_empty_scope(self):
        ids, b = sync.select_task_pull_projects([])
        assert ids == set() and b["projects_in_scope"] == 0
