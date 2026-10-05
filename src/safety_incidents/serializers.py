"""只读视图序列化：按观看者角色裁剪身份与健康字段。

API 永远不直接返回领域对象，而是经过本模块投影，避免调用方绕过分层。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import permissions
from .models import (
    Incident,
    Task,
    User,
    VerificationAction,
    VerificationStatus,
)


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def serialize_user(user: User) -> dict[str, Any]:
    return {
        "user_id": user.user_id,
        "name": user.name,
        "role": user.role.value,
        "organization": user.organization,
    }


def serialize_person(person, visibility: permissions.FieldVisibility) -> dict[str, Any]:
    data: dict[str, Any] = {
        "person_id": person.person_id,
        "label": person.label,
        "guardian_user_id": person.guardian_user_id,
    }
    if visibility.show_identity:
        data["name"] = person.name
        data["contact"] = person.contact
    else:
        data["name"] = "***"
        data["contact"] = "***"
    if visibility.show_health:
        data["health_summary"] = person.health_summary
        data["medical_action"] = person.medical_action
    else:
        data["health_summary"] = "***"
        data["medical_action"] = "***"
    data["fields_redacted"] = {
        "identity": not visibility.show_identity,
        "health": not visibility.show_health,
    }
    return data


def serialize_evidence(evidence, viewer: User) -> dict[str, Any]:
    data = {
        "evidence_id": evidence.evidence_id,
        "kind": evidence.kind,
        "captured_at": _dt(evidence.captured_at),
        "recorded_by": evidence.recorded_by,
        "source_ref": evidence.source_ref,
        "summary": evidence.summary,
    }
    # 监护人端只展示证据条目存在性，不展示可能含伤情的摘要原文
    if viewer.role is permissions.UserRole.GUARDIAN:
        data["summary"] = "***"
        data["recorded_by"] = "***"
        data["source_ref"] = "***"
    return data


def serialize_measure(measure) -> dict[str, Any]:
    """措施视图明确展示「是否完成」以及「完成依据」。"""
    return {
        "measure_id": measure.measure_id,
        "title": measure.title,
        "description": measure.description,
        "basis": measure.basis,
        "ordered_at": _dt(measure.ordered_at),
        "ordered_by": measure.ordered_by,
        "related_evidence_ids": list(measure.related_evidence_ids),
        "completed": measure.completed_at is not None,
        "completed_at": _dt(measure.completed_at),
        "completed_by": measure.completed_by,
        "completion_basis": measure.completion_basis or None,
    }


def serialize_task(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "description": task.description,
        "assignee_id": task.assignee_id,
        "assignee_name": task.assignee_name,
        "basis": task.basis,
        "created_at": _dt(task.created_at),
        "due_at": _dt(task.due_at),
        "status": task.status.value,
        "started_at": _dt(task.started_at),
        "completed_at": _dt(task.completed_at),
        "completed_by": task.completed_by,
        "completion_basis": task.completion_basis or None,
        "overdue": task.overdue,
        "reminders": [
            {
                "reminder_id": r.reminder_id,
                "created_at": _dt(r.created_at),
                "due_at": _dt(r.due_at),
                "level": r.level,
                "note": r.note,
            }
            for r in task.reminders
        ],
    }


def serialize_source(source, viewer: User) -> dict[str, Any]:
    data = {
        "source_id": source.source_id,
        "reporter_org": source.reporter_org.value,
        "created_at": _dt(source.created_at),
        "occurred_at": _dt(source.occurred_at),
        "title": source.title,
        "summary": source.summary,
        "external_ref": source.external_ref,
    }
    if viewer.role is permissions.UserRole.VOLUNTEER:
        # 志愿者可见建档渠道，但不暴露建档人姓名/外部单号
        data["reporter"] = {"role": source.reporter.role.value}
        data["external_ref"] = "***"
    else:
        data["reporter"] = serialize_user(source.reporter)
    return data


def serialize_verification(verification) -> dict[str, Any]:
    return {
        "verification_id": verification.verification_id,
        "action": verification.action.value,
        "status": verification.status.value,
        "requested_by": verification.requested_by,
        "requested_by_name": verification.requested_by_name,
        "requested_at": _dt(verification.requested_at),
        "reason": verification.reason,
        "verifier_id": verification.verifier_id,
        "verifier_name": verification.verifier_name,
        "decided_at": _dt(verification.decided_at),
        "decision_note": verification.decision_note or None,
        "payload": verification.payload,
        "independent": verification.verifier_id != verification.requested_by
        if verification.verifier_id
        else None,
    }


def serialize_incident(incident: Incident, viewer: User) -> dict[str, Any]:
    """按观看者角色投影整个事件。"""
    people_view = []
    for person in incident.people:
        visibility = permissions.person_visibility(viewer, person, incident)
        if not visibility.show_identity and not visibility.show_health:
            if viewer.role is permissions.UserRole.GUARDIAN:
                # 监护人看不到其他孩子的任何信息
                continue
        people_view.append(serialize_person(person, visibility))

    data = {
        "incident_id": incident.incident_id,
        "title": incident.title,
        "description": incident.description,
        "status": incident.status.value,
        "severity": incident.severity,
        "owner_org": incident.owner_org,
        "created_at": _dt(incident.created_at),
        "closed_at": _dt(incident.closed_at),
        "sources": [serialize_source(s, viewer) for s in incident.sources],
        "merged_source_incident_ids": list(incident.merged_source_incident_ids),
        "merged_into": incident.merged_into,
        "people": people_view,
        "evidence": [serialize_evidence(e, viewer) for e in incident.evidence],
        "measures": [serialize_measure(m) for m in incident.measures],
        "tasks": [serialize_task(t) for t in incident.tasks],
        "timeline": [
            {
                "entry_id": e.entry_id,
                "at": _dt(e.at),
                "kind": e.kind,
                "actor": e.actor,
                "summary": e.summary,
                "refs": e.refs,
            }
            for e in incident.timeline
        ],
        "verifications": [
            serialize_verification(v) for v in incident.verifications
        ],
        "viewer": {
            "user_id": viewer.user_id,
            "role": viewer.role.value,
        },
    }
    return data
