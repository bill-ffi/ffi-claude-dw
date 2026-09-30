"""is_oosoob / tag_ids on timelogs ("out of scope, out of budget").

Added 2026-09-30. The flag is derived from the tags on each time entry; the
things that can silently break it are all pinned here: the tag id, reading
both tag shapes time.json sends, NULL-vs-FALSE for history not yet re-pulled,
adding the columns to the existing table, and the independent cross-check.
"""

import inspect
from datetime import date

import pytest

import bigquery_sync
import schemas
import sync
import teamwork_client as tc
import transform
import views
from conftest import FakeResponse

OOSOOB = 395371


def entry(**extra):
    return {"id": 1, "timeLogged": "2026-09-01T14:00:00Z", "minutes": 60, **extra}


class TestTagIdAndDerivation:
    def test_the_confirmed_tag_id(self):
        # From the 2026-09-30 discovery dry run. Matched by id, not name.
        assert transform.OOSOOB_TAG_ID == OOSOOB

    def test_tagged_entry_is_flagged(self):
        row = transform.normalize_timelog(entry(tagIds=[OOSOOB]))
        assert row["is_oosoob"] is True and row["tag_ids"] == [OOSOOB]

    def test_other_tags_are_kept_but_not_flagged(self):
        row = transform.normalize_timelog(entry(tagIds=[264875]))
        assert row["is_oosoob"] is False and row["tag_ids"] == [264875]

    def test_untagged_entry_has_no_tag_field_at_all(self):
        # time.json omits tagIds on an untagged entry -- the reason the first
        # dry-run sample showed no tag field. That is FALSE, not unknown.
        row = transform.normalize_timelog(entry())
        assert row["is_oosoob"] is False and row["tag_ids"] == []

    def test_reads_the_tags_ref_list_when_tagIds_is_absent(self):
        row = transform.normalize_timelog(entry(tags=[{"id": OOSOOB, "type": "tags"}]))
        assert row["is_oosoob"] is True

    def test_ids_are_deduplicated_sorted_integers(self):
        assert transform.timelog_tag_ids({"tagIds": ["395371", 264875, 395371]}) == [264875, OOSOOB]


class TestSchema:
    def fields(self):
        return {f.name: f for f in schemas.TIMELOGS_SCHEMA}

    def test_columns_exist_and_can_be_added_to_an_existing_table(self):
        f = self.fields()
        assert f["is_oosoob"].field_type == "BOOL" and f["is_oosoob"].mode == "NULLABLE"
        assert f["tag_ids"].field_type == "INT64" and f["tag_ids"].mode == "REPEATED"

    def test_appended_last_so_added_columns_keep_schema_order(self):
        assert [f.name for f in schemas.TIMELOGS_SCHEMA][-2:] == ["tag_ids", "is_oosoob"]

    def test_not_in_the_always_populated_list(self):
        # Legitimately empty on history not yet re-pulled; listing them would
        # make the fill-rate warning fire and train everyone to ignore it.
        cols = schemas.ALWAYS_POPULATED_COLUMNS.get(schemas.TIMELOGS_TABLE, [])
        assert "is_oosoob" not in cols and "tag_ids" not in cols


class FakeField:
    def __init__(self, name, mode="NULLABLE"):
        self.name, self.mode = name, mode


class FakeTable:
    def __init__(self, names):
        self.schema = [FakeField(n) for n in names]


class FakeClient:
    def __init__(self, names):
        self.table = FakeTable(names)
        self.updates = []

    def get_table(self, ref):
        return self.table

    def update_table(self, table, fields):
        self.updates.append((fields, [f.name for f in table.schema]))
        return table


class FakeDatasetRef:
    def table(self, name):
        return name


class TestEnsureTableColumns:
    def test_adds_only_the_missing_columns_at_the_end(self):
        client = FakeClient(["a", "b"])
        schema = [FakeField("a"), FakeField("b"), FakeField("tag_ids", "REPEATED"), FakeField("is_oosoob")]
        added = bigquery_sync.ensure_table_columns(client, FakeDatasetRef(), "timelogs", schema)
        assert added == ["tag_ids", "is_oosoob"]
        assert client.updates == [(["schema"], ["a", "b", "tag_ids", "is_oosoob"])]

    def test_no_change_when_nothing_is_missing(self):
        client = FakeClient(["a", "b"])
        assert bigquery_sync.ensure_table_columns(
            client, FakeDatasetRef(), "t", [FakeField("a"), FakeField("b")]) == []
        assert client.updates == []

    def test_keeps_columns_the_schema_no_longer_lists(self):
        # Never drops anything: dropping a column is a decision, not a sync.
        client = FakeClient(["a", "old"])
        bigquery_sync.ensure_table_columns(client, FakeDatasetRef(), "t", [FakeField("a"), FakeField("new")])
        assert client.updates[0][1] == ["a", "old", "new"]

    def test_refuses_a_required_column(self):
        client = FakeClient(["a"])
        with pytest.raises(ValueError, match="REQUIRED"):
            bigquery_sync.ensure_table_columns(
                client, FakeDatasetRef(), "t", [FakeField("a"), FakeField("x", "REQUIRED")])
        assert client.updates == []

    def test_every_table_gets_the_column_check(self):
        # Timelogs AND its staging table: the replace INSERTs every schema
        # column by name from staging into timelogs.
        source = inspect.getsource(bigquery_sync.ensure_all_tables)
        assert "ensure_table_columns(client, dataset_ref, table_name, schema)" in source
        for name in ("TIMELOGS_TABLE, TIMELOGS_SCHEMA", "TIMELOGS_STAGING_TABLE, TIMELOGS_SCHEMA"):
            assert name in source

    def test_the_replace_inserts_the_new_columns(self):
        source = inspect.getsource(bigquery_sync.replace_timelogs_window)
        assert 'columns = ", ".join(field.name for field in TIMELOGS_SCHEMA)' in source


class TestTagCount:
    def test_filters_with_tagIds_not_the_ignored_bracket_form(self, client, no_sleep):
        c = client([FakeResponse(payload={"timelogs": [], "meta": {"page": {"count": 263}}})])
        assert c.count_timelogs_with_tag("2026-01-01", "2026-09-30", OOSOOB) == 263
        params = c.session.requests[0]["params"]
        assert params["tagIds"] == str(OOSOOB) and "tagIds[]" not in params
        assert params["pageSize"] == 1
        assert params["startDate"] == "2026-01-01" and params["endDate"] == "2026-09-30"

    def test_no_count_is_none(self, client, no_sleep):
        c = client([FakeResponse(payload={"timelogs": []})])
        assert c.count_timelogs_with_tag("2026-01-01", "2026-09-30", OOSOOB) is None


class FakeTw:
    def __init__(self, count=None, raises=None):
        self.count, self.raises, self.calls = count, raises, []

    def count_timelogs_with_tag(self, start, end, tag_id):
        self.calls.append((start, end, tag_id))
        if self.raises:
            raise self.raises
        return self.count


class TestCrossCheck:
    ROWS = [{"is_oosoob": True}, {"is_oosoob": False}, {"is_oosoob": True}]

    def check(self, tw):
        return sync.oosoob_cross_check(tw, self.ROWS, date(2026, 8, 1), date(2026, 9, 30))

    def test_agreement(self, caplog):
        with caplog.at_level("WARNING"):
            assert self.check(FakeTw(count=2)) == {"marked": 2, "teamwork_count": 2}
        assert caplog.records == []

    def test_mismatch_warns(self, caplog):
        with caplog.at_level("WARNING"):
            assert self.check(FakeTw(count=5)) == {"marked": 2, "teamwork_count": 5}
        assert "OOSOOB mismatch" in caplog.records[0].getMessage()

    def test_asks_about_the_same_window_and_tag(self):
        tw = FakeTw(count=2)
        self.check(tw)
        assert tw.calls == [("2026-08-01", "2026-09-30", OOSOOB)]

    def test_a_failed_count_is_none_and_never_fatal(self):
        assert self.check(FakeTw(raises=RuntimeError("503"))) == {"marked": 2, "teamwork_count": None}

    def test_runs_on_every_timelogs_window_and_reaches_the_summary(self):
        source = inspect.getsource(sync.sync_timelogs_for_window)
        assert "oosoob = oosoob_cross_check(tw_client, rows, window_start, last_day_inclusive)" in source
        assert '"oosoob": oosoob,' in source


class TestView:
    def test_timelog_detail_exposes_the_flag(self):
        body = views.build_view_sql("p", "d")["v_timelog_detail"]
        assert "\n  tl.is_oosoob,\n" in body
