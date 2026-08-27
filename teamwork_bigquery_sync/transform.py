"""Maps raw Teamwork API objects to BigQuery row dicts."""

from datetime import datetime, timezone


def _date_part(value):
    if not value:
        return None
    return value[:10]


def _ref_id(ref):
    """Teamwork v3 relationships come back as {"id": 123, "type": "..."}."""
    if not ref:
        return None
    return ref.get("id")


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def pick_current_budget(budgets_for_project):
    """A project can have several budgets (e.g. recurring monthly time
    budgets). Prefer the ACTIVE one; among ties, the latest start date.
    """
    active = [b for b in budgets_for_project if b.get("status") == "ACTIVE"]
    candidates = active or budgets_for_project
    if not candidates:
        return None
    return max(candidates, key=lambda b: b.get("startDate") or "")


def normalize_project(raw, category_names_by_id, budgets_by_project_id):
    if raw.get("status") == "deleted" or raw.get("deletedAt"):
        return None

    project_id = raw["id"]
    category_id = _ref_id(raw.get("category"))
    budget = pick_current_budget(budgets_by_project_id.get(project_id, []))
    budget_capacity = budget.get("capacity") if budget else None
    budget_used = budget.get("capacityUsed") if budget else None
    budget_left = (
        budget_capacity - budget_used
        if budget_capacity is not None and budget_used is not None
        else None
    )

    return {
        "project_id": project_id,
        "name": raw.get("name"),
        "description": raw.get("description"),
        "status": raw.get("status"),
        "sub_status": raw.get("subStatus"),
        "category_id": category_id,
        "category_name": category_names_by_id.get(category_id),
        "company_id": raw.get("companyId") or _ref_id(raw.get("company")),
        "owner_id": raw.get("projectOwnerId") or raw.get("ownerId"),
        "is_billable": raw.get("isBillable"),
        "start_date": _date_part(raw.get("startAt") or raw.get("startDate")),
        "end_date": _date_part(raw.get("endAt") or raw.get("endDate")),
        # Best-effort — see schemas.py comment. Only populated if Teamwork
        # actually returns this key for your account.
        "health": raw.get("health"),
        "budget_capacity": budget_capacity,
        "budget_used": budget_used,
        "budget_left": budget_left,
        "tag_ids": raw.get("tagIds") or [],
        "web_link": (raw.get("meta") or {}).get("webLink"),
        "created_at": raw.get("createdAt"),
        "created_by": raw.get("createdBy"),
        "updated_at": raw.get("updatedAt"),
        "updated_by": raw.get("updatedBy"),
        "completed_at": raw.get("completedAt"),
        "completed_by": raw.get("completedBy"),
        "archived_at": raw.get("archivedAt"),
        "synced_at": utc_now_iso(),
    }


def normalize_task(raw, in_scope_project_ids):
    if raw.get("deletedAt"):
        return None

    tasklist = raw.get("tasklist") or {}
    tasklist_meta = tasklist.get("meta") or {}
    project_id = tasklist_meta.get("projectId")
    if in_scope_project_ids is not None and project_id not in in_scope_project_ids:
        return None

    return {
        "task_id": raw["id"],
        "project_id": project_id,
        "tasklist_id": raw.get("tasklistId") or _ref_id(tasklist),
        "tasklist_name": tasklist_meta.get("name"),
        "parent_task_id": raw.get("parentTaskId") or None,
        "name": raw.get("name"),
        "description": raw.get("description"),
        "status": raw.get("status"),
        "priority": raw.get("priority"),
        "progress_pct": raw.get("progress"),
        "estimate_minutes": raw.get("estimateMinutes"),
        "start_date": _date_part(raw.get("startDate")),
        "due_date": _date_part(raw.get("dueDate")),
        "assignee_user_ids": raw.get("assigneeUserIds") or [],
        "tag_ids": raw.get("tagIds") or [],
        "is_private": bool(raw.get("isPrivate")),
        "is_archived": raw.get("isArchived"),
        # Filled in separately by sync.py after this row is built — Teamwork
        # doesn't include custom field values on the task list payload, so
        # this needs a follow-up call per task. Defaults to None so the
        # column always exists even if that enrichment step fails/is skipped.
        "activity": None,
        "web_link": (raw.get("meta") or {}).get("webLink"),
        "created_at": raw.get("createdAt"),
        "created_by": raw.get("createdByUserId") or raw.get("createdBy"),
        "updated_at": raw.get("updatedAt") or raw.get("dateUpdated"),
        "updated_by": raw.get("updatedBy"),
        "synced_at": utc_now_iso(),
    }


def normalize_timelog(raw):
    if raw.get("deleted") or raw.get("deletedAt"):
        return None

    minutes = raw.get("minutes")
    logged_at = raw.get("timeLogged")

    return {
        "timelog_id": raw["id"],
        "task_id": raw.get("taskId") or _ref_id(raw.get("task")),
        "project_id": raw.get("projectId") or _ref_id(raw.get("project")),
        "user_id": raw.get("userId") or _ref_id(raw.get("user")),
        "logged_by_user_id": raw.get("loggedByUserId") or raw.get("loggedBy"),
        "log_date": _date_part(logged_at),
        "logged_at": logged_at,
        "minutes": minutes,
        "hours": round(minutes / 60.0, 4) if minutes is not None else None,
        "is_billable": raw.get("isBillable", raw.get("billable")),
        "billable_rate": raw.get("billableRate"),
        "cost_rate": raw.get("costRate"),
        "description": raw.get("description"),
        "is_locked": raw.get("isLocked"),
        "created_at": raw.get("createdAt") or raw.get("dateCreated"),
        "updated_at": raw.get("updatedAt") or raw.get("dateEdited"),
        "synced_at": utc_now_iso(),
    }


def normalize_user(raw):
    first_name = raw.get("firstName") or ""
    last_name = raw.get("lastName") or ""
    full_name = f"{first_name} {last_name}".strip() or None

    return {
        "user_id": raw["id"],
        "first_name": raw.get("firstName"),
        "last_name": raw.get("lastName"),
        "full_name": full_name,
        "email": raw.get("email"),
        "title": raw.get("title"),
        "user_type": raw.get("type"),
        "is_admin": raw.get("isAdmin"),
        "company_id": raw.get("companyId") or _ref_id(raw.get("company")),
        "is_deleted": raw.get("deleted"),
        "last_login": raw.get("lastLogin"),
        "timezone": raw.get("timezone"),
        # userCost/userRate come back from Teamwork in cents (confirmed by
        # cross-checking against userRates[...].amount, which is in dollars,
        # on a live sample) — divide down to actual currency units.
        "user_cost": raw.get("userCost") / 100.0 if raw.get("userCost") is not None else None,
        "user_rate": raw.get("userRate") / 100.0 if raw.get("userRate") is not None else None,
        "created_at": raw.get("createdAt"),
        "updated_at": raw.get("updatedAt"),
        "synced_at": utc_now_iso(),
    }


def find_custom_field_by_name(custom_fields, name):
    """Case-insensitive match on a custom field's display name. Returns the
    raw custom field dict, or None if nothing matches.
    """
    target = name.strip().lower()
    for cf in custom_fields:
        if (cf.get("name") or "").strip().lower() == target:
            return cf
    return None


def build_option_label_map(custom_field):
    """Maps a dropdown custom field's option key -> display label.

    Confirmed live shape: `options` is a dict `{"choices": [{"value": "...",
    "color": "..."}, ...]}` — there's no separate numeric option id, the
    "value" string IS the label (e.g. "REVnCOGS"). This map ends up as an
    effectively-identity mapping for this field, but is kept generic (and
    still accepts a bare list of option dicts) in case another dropdown
    field is ever wired up with a genuine id/label split.
    """
    options = custom_field.get("options")
    if isinstance(options, dict):
        choices = options.get("choices") or []
    elif isinstance(options, list):
        choices = options
    else:
        choices = []

    label_by_key = {}
    for opt in choices:
        if not isinstance(opt, dict):
            continue
        key = opt.get("id")
        if key is None:
            key = opt.get("value")
        label = opt.get("label") or opt.get("name") or opt.get("value")
        if key is not None:
            label_by_key[key] = label
    return label_by_key


def extract_activity_value(raw_task_custom_field_values, activity_field_id, option_labels):
    """Given the raw list returned by get_task_custom_field_values() for one
    task, finds the value set for `activity_field_id` and resolves it to a
    human-readable label.

    Confirmed live shape: each entry looks like {"customfield": {"id":
    98742, ...}, "customfieldId": 98742, "value": "REVnCOGS", ...} — value
    is already the raw label string directly (option_labels lookup is a
    no-op for this field, but kept for any field that does use an id/label
    split). A task with no value set for this field simply has no matching
    entry in the list.
    """
    if not raw_task_custom_field_values:
        return None
    for entry in raw_task_custom_field_values:
        field_ref = entry.get("customfield") or {}
        field_id = field_ref.get("id") if isinstance(field_ref, dict) else None
        if field_id is None:
            field_id = entry.get("customfieldId")
        if field_id != activity_field_id:
            continue
        value = entry.get("value")
        if isinstance(value, dict):
            return value.get("label") or value.get("name") or value.get("value")
        if value in option_labels:
            return option_labels[value]
        return value
    return None


def extract_activity_map_from_sideload(tasks_included, activity_field_id, option_labels):
    """Given the `included` block from a bulk tasks.json pull made with
    includeCustomFields=true, builds {task_id: activity_label} for every
    task that has the Activity field set.

    Returns None if included["customfieldTasks"] is missing entirely —
    that's the signal to the caller that the bulk sideload didn't work and
    it should fall back to per-task fetching. Returns {} (not None) if the
    key is present but nothing matched the activity field.

    Confirmed-live entry shape (from a single-task pull, presumed to hold
    for the bulk sideload too — see the "bulk_sideload_entry_shape" dry-run
    diagnostic to confirm): {"customfield": {"id": ...}, "customfieldId":
    ..., "taskId": ..., "value": "<label>"}. The examples in Teamwork's own
    public API-Request-Examples repo iterate this as an object keyed by the
    value's own id (`for key in customfieldTasks`), so this handles both a
    dict-of-values and a plain list defensively.
    """
    customfield_tasks = tasks_included.get("customfieldTasks")
    if customfield_tasks is None:
        return None

    entries = customfield_tasks.values() if isinstance(customfield_tasks, dict) else customfield_tasks

    activity_by_task_id = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        field_ref = entry.get("customfield") or {}
        field_id = field_ref.get("id") if isinstance(field_ref, dict) else None
        if field_id is None:
            field_id = entry.get("customfieldId")
        if field_id != activity_field_id:
            continue
        task_id = entry.get("taskId")
        if task_id is None:
            task_ref = entry.get("task") or {}
            task_id = task_ref.get("id") if isinstance(task_ref, dict) else None
        if task_id is None:
            continue
        value = entry.get("value")
        if isinstance(value, dict):
            value = value.get("label") or value.get("name") or value.get("value")
        elif value in option_labels:
            value = option_labels[value]
        activity_by_task_id[task_id] = value
    return activity_by_task_id


def build_category_name_map(projects_included):
    """`included.projectCategories` from the projects list response, keyed
    by category id -> name.
    """
    categories = (projects_included or {}).get("projectCategories", {})
    return {int(k): v.get("name") for k, v in categories.items()}


def build_budgets_by_project(budgets):
    by_project = {}
    for budget in budgets:
        project_id = budget.get("projectId") or _ref_id(budget.get("project"))
        if project_id is None:
            continue
        by_project.setdefault(project_id, []).append(budget)
    return by_project
