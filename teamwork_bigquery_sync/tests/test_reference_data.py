"""reference_data.py -- hand-maintained grouping/sort tables.

A reference table is joined into views that every report reads, so a bad edit
does not just look wrong: a duplicate key fans out the join and doubles hours,
and a group carrying two sort numbers scrambles every sorted chart. These
tests pin the validation that stops such an edit at load time.
"""

import inspect
import os

import pytest

import reference_data as rd
import sync
import views

TABLE = rd.ACTIVITY_GROUPS_TABLE
HEADER = "activity_group,ag_sort,activity,activity_sort,tw_color\n"


def write(tmp_path, body, header=HEADER, name="activity_groups.csv", encoding="utf-8"):
    (tmp_path / name).write_text(header + body, encoding=encoding)
    return str(tmp_path)


class TestTheRealFile:
    def test_validates(self):
        rows = rd.load_reference_rows(TABLE)
        assert len(rows) == 13
        assert all(isinstance(r["ag_sort"], int) and isinstance(r["activity_sort"], int) for r in rows)

    def test_covers_every_activity_teamwork_offered_on_2026_09_29(self):
        # The option list from the Activity custom field (RUN_SUMMARY
        # activity.options). A snapshot: the sync's unmapped_activities check
        # is what catches options added after this date.
        offered = ["BOOKS / GL", "BANK RECS", "A/P & EXP", "A/R & INV", "REVnCOGS",
                   "CONTROLLING", "PAYROLL", "HR", "ADVISORY", "CLIENT MNGMT",
                   "FP&A", "PROJECTS", "COMPLIANCE"]
        assert rd.unmapped_activities(offered) == []

    def test_sorts_step_by_ten(self):
        # Confirmed 2026-09-29: gaps of 10, so a new group or Activity can be
        # slotted between two others without renumbering the rest.
        rows = rd.load_reference_rows(TABLE)
        assert sorted({r["ag_sort"] for r in rows}) == [10, 20, 30, 40, 50, 60]
        assert sorted(r["activity_sort"] for r in rows) == list(range(10, 140, 10))

    def test_every_sort_stays_below_the_missing_sort(self):
        # 'Missing' must sort after every real group and Activity.
        rows = rd.load_reference_rows(TABLE)
        top = max(max(r["ag_sort"], r["activity_sort"]) for r in rows)
        assert top < views.MISSING_ACTIVITY_SORT

    def test_colours_match_teamwork_on_2026_09_29(self):
        # The Activity field's option colours from that day's dry run. A
        # snapshot; activity_color_mismatches is what catches later changes.
        teamwork = {
            "BOOKS / GL": "#4461d7", "BANK RECS": "#4461d7", "A/P & EXP": "#4461d7",
            "A/R & INV": "#4461d7", "REVnCOGS": "#4461d7", "CONTROLLING": "#4461d7",
            "PAYROLL": "#4ecd97", "HR": "#4ecd97", "ADVISORY": "#bba1ff",
            "CLIENT MNGMT": "#bba1ff", "FP&A": "#bba1ff", "PROJECTS": "#bba1ff",
            "COMPLIANCE": "#ffc63c",
        }
        assert rd.activity_color_mismatches(teamwork) == []


class TestValidation:
    def test_a_good_file_loads_typed(self, tmp_path):
        d = write(tmp_path, "Books,2,BANK RECS,6,#4461d7\n")
        assert rd.load_reference_rows(TABLE, d) == [
            {"activity_group": "Books", "ag_sort": 2, "activity": "BANK RECS",
             "activity_sort": 6, "tw_color": "#4461d7"}
        ]

    def test_excel_byte_order_mark_and_crlf_are_tolerated(self, tmp_path):
        d = write(tmp_path, "Books,2,BANK RECS,6,#4461d7\r\n", header=HEADER.replace("\n", "\r\n"),
                  encoding="utf-8-sig")
        assert rd.load_reference_rows(TABLE, d)[0]["activity_group"] == "Books"

    def test_trailing_blank_lines_are_ignored(self, tmp_path):
        d = write(tmp_path, "Books,2,BANK RECS,6,#4461d7\n\n,,,\n")
        assert len(rd.load_reference_rows(TABLE, d)) == 1

    def test_surrounding_spaces_are_stripped(self, tmp_path):
        # "BANK RECS " would silently fail to match the Activity in the join.
        d = write(tmp_path, "Books , 2 , BANK RECS ,6,#4461d7\n")
        assert rd.load_reference_rows(TABLE, d)[0]["activity"] == "BANK RECS"

    @pytest.mark.parametrize("body, complaint", [
        # A duplicate key fans out the view join and doubles that Activity's hours.
        ("Books,2,BANK RECS,6,#4461d7\nBooks,2,BANK RECS,7,#4461d7\n", "'activity' value 'BANK RECS' appears twice"),
        ("Books,2,BANK RECS,6,#4461d7\nBooks,2,BOOKS / GL,6,#4461d7\n", "'activity_sort' value 6 appears twice"),
        # One group, two sort numbers: it would sort in two places at once.
        ("Books,2,BANK RECS,6,#4461d7\nBooks,3,BOOKS / GL,7,#4461d7\n", "has two ag_sort values"),
        # Two groups, one sort number: their order would be arbitrary.
        ("Books,2,BANK RECS,6,#4461d7\nPayroll,2,PAYROLL,7,#4461d7\n", "ag_sort 2 is shared"),
        ("Books,,BANK RECS,6,#4461d7\n", "'ag_sort' is blank"),
        ("Books,two,BANK RECS,6,#4461d7\n", "must be a whole number"),
        ("Books,2,BANK RECS,6,blue\n", "must look like #4461d7"),
        ("Books,2,BANK RECS,6,#4461d\n", "must look like #4461d7"),
        ("", "no rows"),
    ])
    def test_rejects(self, tmp_path, body, complaint):
        with pytest.raises(rd.ReferenceDataError, match=complaint.replace("(", r"\(")):
            rd.load_reference_rows(TABLE, write(tmp_path, body))

    def test_rejects_a_renamed_or_missing_column(self, tmp_path):
        d = write(tmp_path, "Books,2,BANK RECS,6,#4461d7\n",
                  header="activity_group,group_sort,activity,activity_sort,tw_color\n")
        with pytest.raises(rd.ReferenceDataError, match="columns must be exactly"):
            rd.load_reference_rows(TABLE, d)


class TestLoading:
    class FakeJob:
        def result(self):
            return None

    class FakeClient:
        def __init__(self):
            self.loads = []

        def load_table_from_json(self, rows, ref, job_config=None):
            self.loads.append((ref, list(rows), job_config))
            return TestLoading.FakeJob()

    class FakeDatasetRef:
        def table(self, name):
            return f"ref:{name}"

    def test_replaces_the_table_with_the_validated_rows(self):
        client = self.FakeClient()
        assert rd.load_reference_tables(client, self.FakeDatasetRef()) == {TABLE: 13}
        ref, rows, config = client.loads[0]
        assert ref == f"ref:{TABLE}" and len(rows) == 13
        assert config.write_disposition == "WRITE_TRUNCATE"

    def test_one_bad_file_loads_nothing(self, tmp_path, monkeypatch):
        # Validate everything before writing anything, so a bad edit cannot
        # leave the set half-updated.
        write(tmp_path, "Books,2,BANK RECS,6,#4461d7\n")
        write(tmp_path, "Books,2,BANK RECS,6,#4461d7\nBooks,2,BANK RECS,6,#4461d7\n", name="bad.csv")
        monkeypatch.setattr(rd, "REFERENCE_TABLES", {
            TABLE: rd.REFERENCE_TABLES[TABLE],
            "ref_bad": {**rd.REFERENCE_TABLES[TABLE], "file": "bad.csv"},
        })
        client = self.FakeClient()
        with pytest.raises(rd.ReferenceDataError):
            rd.load_reference_tables(client, self.FakeDatasetRef(), str(tmp_path))
        assert client.loads == []

    def test_create_views_loads_reference_tables_before_the_views(self):
        # BigQuery will not create a view over a table that does not exist.
        source = inspect.getsource(sync.run_create_views)
        assert source.index("reference_data.load_reference_tables(") < \
            source.index("views.create_or_replace_views(")


class TestUnmappedActivities:
    def test_names_activities_with_no_group(self):
        assert rd.unmapped_activities(["BANK RECS", "NEW THING"]) == ["NEW THING"]

    def test_ignores_tasks_with_no_activity(self):
        assert rd.unmapped_activities([None, "BANK RECS"]) == []

    def test_is_case_and_spelling_exact(self):
        # The view join is exact, so a near-miss must be reported, not forgiven.
        assert rd.unmapped_activities(["Bank Recs"]) == ["Bank Recs"]


@pytest.fixture(scope="module")
def sql():
    return views.build_view_sql("radiant-rig-284611", "teamwork_data")


class TestViewsJoinTheGroups:
    GROUPS = "`radiant-rig-284611.teamwork_data.ref_activity_groups`"

    @pytest.mark.parametrize("name, alias", [("v_timelog_detail", "tk"), ("v_task_review", "t")])
    def test_left_join_on_activity(self, sql, name, alias):
        # LEFT: an inner join would drop every entry with no Activity -- all
        # untasked time -- from the view.
        assert f"\nLEFT JOIN {self.GROUPS} ag ON ag.activity = {alias}.activity\n" in sql[name]

    @pytest.mark.parametrize("name", ["v_timelog_detail", "v_task_review", "v_user_daily_time_split"])
    def test_carries_group_and_sort_columns(self, sql, name):
        for col in ("activity_group", "ag_sort", "activity_sort"):
            assert col in sql[name], (name, col)

    def test_time_split_takes_them_from_v_timelog_detail(self, sql):
        body = sql["v_user_daily_time_split"]
        assert self.GROUPS not in body
        assert "    d.activity_group,\n    d.ag_sort,\n    d.activity_sort,\n" in body


class TestColourMismatches:
    def test_reports_a_recoloured_activity(self):
        assert rd.activity_color_mismatches({"PAYROLL": "#ff0000"}) == [
            {"activity": "PAYROLL", "teamwork": "#ff0000", "reference": "#4ecd97"}
        ]

    def test_case_is_not_a_difference(self):
        assert rd.activity_color_mismatches({"PAYROLL": "#4ECD97"}) == []

    def test_activities_teamwork_did_not_report_are_skipped(self):
        # Missing or renamed options are unmapped_activities' concern.
        assert rd.activity_color_mismatches({}) == []


class TestOptionColourMap:
    def test_reads_the_live_option_shape(self):
        import transform
        field = {"options": {"choices": [{"value": "PAYROLL", "color": "#4ecd97"},
                                         {"value": "HR"}]}}
        assert transform.build_option_color_map(field) == {"PAYROLL": "#4ecd97"}


class TestMissingActivity:
    """No Activity reads 'Missing' and sorts last -- confirmed 2026-09-29."""

    @pytest.mark.parametrize("name, alias", [("v_timelog_detail", "tk"), ("v_task_review", "t")])
    def test_missing_label_and_sorts(self, sql, name, alias):
        body = sql[name]
        assert (f"CASE WHEN {alias}.activity IS NULL THEN 'Missing' ELSE ag.activity_group END"
                " AS activity_group,") in body
        assert f"CASE WHEN {alias}.activity IS NULL THEN 999 ELSE ag.ag_sort END AS ag_sort," in body
        assert (f"CASE WHEN {alias}.activity IS NULL THEN 999 ELSE ag.activity_sort END"
                " AS activity_sort,") in body

    @pytest.mark.parametrize("name, alias", [("v_timelog_detail", "tk"), ("v_task_review", "t")])
    def test_the_activity_column_itself_is_left_null(self, sql, name, alias):
        # has_activity and the missing-Activity exception rules test
        # activity IS NULL; relabelling the column would silently break them.
        body = sql[name]
        assert f"\n  {alias}.activity,\n" in body
        assert f"COALESCE({alias}.activity" not in body

    def test_time_split_carries_the_colour(self, sql):
        body = sql["v_user_daily_time_split"]
        assert "    d.tw_color,\n" in body
        group_line = [l for l in body.splitlines() if l.startswith("GROUP BY ")][0]
        assert "tw_color" in group_line
