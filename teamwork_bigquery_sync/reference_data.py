"""Small, hand-maintained lookup tables for grouping and sorting in reports.

Each one is a CSV checked into reference/, validated here, and loaded into a
native BigQuery table on every --create-views run (before the views, since
views join them). The CSV in the repo is the source of truth: edit it, commit,
and run --create-views; git history is the change log.

Deliberately NOT a Google Sheet. gs_minimum_user_info shows the cost: anything
that joins a Drive-backed table can only be queried by people with access to
that sheet. These tables are joined into v_timelog_detail, which every time
report reads, so a sheet here would lock the whole reporting layer behind
Drive permissions.

To add a table: drop the CSV in reference/ and add one entry to
REFERENCE_TABLES. Validation is generic -- column set, types, a unique key,
and any "group -> sort" pairs that must agree -- so a bad edit fails the load
loudly instead of quietly fanning out a join or scrambling a sort order.
"""

import csv
import logging
import os

from google.cloud import bigquery

logger = logging.getLogger(__name__)

REFERENCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference")

ACTIVITY_GROUPS_TABLE = "ref_activity_groups"

REFERENCE_TABLES = {
    ACTIVITY_GROUPS_TABLE: {
        "file": "activity_groups.csv",
        "schema": [
            bigquery.SchemaField("activity_group", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("ag_sort", "INT64", mode="REQUIRED"),
            bigquery.SchemaField("activity", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("activity_sort", "INT64", mode="REQUIRED"),
        ],
        # One row per Activity. Views LEFT JOIN on this, so a duplicate key
        # would fan out and double every hour on that Activity's time.
        "key": "activity",
        # Values that must be unique across rows: two Activities sharing a
        # sort number would order unpredictably.
        "unique": ["activity_sort"],
        # (label, sort) pairs where every row with the same label must carry
        # the same sort, or a group would sort in two places at once.
        "consistent": [("activity_group", "ag_sort")],
    },
}


class ReferenceDataError(ValueError):
    pass


def load_reference_rows(table_name, directory=REFERENCE_DIR):
    """Reads and validates one reference CSV. Returns a list of dicts typed to
    the table's schema; raises ReferenceDataError naming the problem."""
    spec = REFERENCE_TABLES[table_name]
    path = os.path.join(directory, spec["file"])
    columns = [f.name for f in spec["schema"]]
    types = {f.name: f.field_type for f in spec["schema"]}

    # utf-8-sig: a CSV saved from Excel starts with a byte-order mark that
    # would otherwise glue itself onto the first column's name.
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip() for h in (reader.fieldnames or [])]
        if header != columns:
            raise ReferenceDataError(
                f"{spec['file']}: columns must be exactly {columns}, got {header}"
            )
        rows = []
        for line_no, raw in enumerate(reader, start=2):
            values = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
            if not any(values.values()):
                continue  # a trailing blank line
            row = {}
            for col in columns:
                value = values.get(col, "")
                if value == "":
                    raise ReferenceDataError(f"{spec['file']} line {line_no}: '{col}' is blank")
                if types[col] == "INT64":
                    try:
                        value = int(value)
                    except ValueError:
                        raise ReferenceDataError(
                            f"{spec['file']} line {line_no}: '{col}' must be a whole number, got {value!r}"
                        ) from None
                row[col] = value
            rows.append(row)

    if not rows:
        raise ReferenceDataError(f"{spec['file']}: no rows")

    for col in [spec["key"]] + spec.get("unique", []):
        seen = {}
        for row in rows:
            if row[col] in seen:
                raise ReferenceDataError(f"{spec['file']}: '{col}' value {row[col]!r} appears twice")
            seen[row[col]] = True

    for label, sort in spec.get("consistent", []):
        sort_by_label = {}
        for row in rows:
            previous = sort_by_label.setdefault(row[label], row[sort])
            if previous != row[sort]:
                raise ReferenceDataError(
                    f"{spec['file']}: {label} {row[label]!r} has two {sort} values "
                    f"({previous} and {row[sort]})"
                )
        labels_by_sort = {}
        for lab, srt in sort_by_label.items():
            if srt in labels_by_sort:
                raise ReferenceDataError(
                    f"{spec['file']}: {sort} {srt} is shared by {labels_by_sort[srt]!r} and {lab!r}"
                )
            labels_by_sort[srt] = lab

    return rows


def load_reference_tables(client, dataset_ref, directory=REFERENCE_DIR):
    """Validates every reference CSV, then replaces each BigQuery table.
    Validation runs for ALL tables before ANY load, so one bad file cannot
    leave the set half-updated. Returns {table_name: rows_loaded}."""
    validated = {name: load_reference_rows(name, directory) for name in REFERENCE_TABLES}
    loaded = {}
    for name, rows in validated.items():
        job_config = bigquery.LoadJobConfig(
            schema=REFERENCE_TABLES[name]["schema"],
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        client.load_table_from_json(rows, dataset_ref.table(name), job_config=job_config).result()
        logger.info("Reference table %s loaded: %d rows", name, len(rows))
        loaded[name] = len(rows)
    return loaded


def unmapped_activities(activities, directory=REFERENCE_DIR):
    """Activity labels with no row in the activity-groups CSV. Their time still
    shows, but with a blank activity_group -- this is what makes a new or
    renamed Activity in Teamwork visible instead of silently ungrouped."""
    mapped = {r["activity"] for r in load_reference_rows(ACTIVITY_GROUPS_TABLE, directory)}
    return sorted({a for a in activities if a is not None} - mapped)
