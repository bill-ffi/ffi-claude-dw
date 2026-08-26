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
