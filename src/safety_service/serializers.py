"""按角色裁剪的序列化层。

敏感信息分层：
- 场馆负责人 / 安全员 / 学校负责人：完整身份 + 完整健康字段；
- 文博中心运营员：完整身份，健康字段裁剪；
- 志愿者：身份脱敏（仅姓氏）、无联系方式、无任何健康字段，来源正文不可见；
- 监护人：仅本人监护对象可见完整身份与健康字段，其余人员脱敏。
"""
from __future__ import annotations

from .models import (
    FULL_HEALTH_ROLES,
    FULL_IDENTITY_ROLES,
    STAFF_ROLES,
    Incident,
    Measure,
    Person,
    Task,
    User,
    VerificationRequest,
)


def _dt(value) -> str | None:
    return value.isoformat() if value else None


def _mask_name(name: str) -> str:
    if not name:
        return ""
    first = name[0]
    return first + "**"


def person_to_dict(person: Person, viewer: User) -> dict:
    full_identity = viewer.role in FULL_IDENTITY_ROLES
    full_health = viewer.role in FULL_HEALTH_ROLES
    is_own_ward = viewer.role == "guardian" and person.guardian_user_id == viewer.user_id
    if is_own_ward:
        full_identity = True
        full_health = True

    data: dict = {
        "person_id": person.person_id,
        "kind": person.kind,
    }
    if full_identity:
        data.update({"name": person.name, "contact": person.contact})
    else:
        data.update({"name": _mask_name(person.name), "identity_redacted": True})

    if full_health:
        data.update({
            "is_injured": person.is_injured,
            "injury_description": person.injury_description,
            "medical_notes": person.medical_notes,
            "guardian_user_id": person.guardian_user_id,
        })
    else:
        data["health_redacted"] = True
    return data


def measure_to_dict(measure: Measure) -> dict:
    return {
        "measure_id": measure.measure_id,
        "title": measure.title,
        "description": measure.description,
        "status": measure.status,
        "is_completed": measure.is_completed,
        "decided_by": measure.decided_by,
        "decided_at": _dt(measure.decided_at),
        "basis_note": measure.basis_note,
        "basis_evidence_ids": measure.basis_evidence_ids,
        "completed_by": measure.completed_by,
        "completed_at": _dt(measure.completed_at),
        "completion_note": measure.completion_note,
    }


def task_to_dict(task: Task) -> dict:
    return {
        "task_id": task.task_id,
        "incident_id": task.incident_id,
        "title": task.title,
        "assignee": task.assignee,
        "description": task.description,
        "due_at": _dt(task.due_at),
        "status": task.status,
        "created_by": task.created_by,
        "created_at": _dt(task.created_at),
        "completed_at": _dt(task.completed_at),
    }


def verification_to_dict(req: VerificationRequest, viewer: User) -> dict:
    data = {
        "request_id": req.request_id,
        "incident_id": req.incident_id,
        "action": req.action,
        "status": req.status,
        "requested_by": req.requested_by_name,
        "requested_at": _dt(req.requested_at),
    }
    if viewer.role in STAFF_ROLES:
        data.update({
            "reason": req.reason,
            "params": req.params,
            "verifier_user_id": req.verifier_user_id,
            "verifier_name": req.verifier_name,
            "verifier_note": req.verifier_note,
            "decided_at": _dt(req.decided_at),
        })
    else:
        data["detail_redacted"] = True
    return data


def incident_to_dict(incident: Incident, viewer: User) -> dict:
    is_staff = viewer.role in STAFF_ROLES

    sources = [
        {
            "report_id": s.report_id,
            "org": s.org,
            "reporter_name": s.reporter_name,
            "created_at": _dt(s.created_at),
            # 志愿者/监护人不可读来源正文，避免其中夹带的健康信息外泄
            **({"content": s.content} if is_staff else {"content_redacted": True}),
        }
        for s in incident.sources
    ]

    if viewer.role == "volunteer":
        # 普通志愿者不读事件自由描述（可能含健康信息）
        description = ""
        description_redacted = True
    else:
        description = incident.description
        description_redacted = False

    if is_staff:
        evidence = [
            {
                "evidence_id": e.evidence_id,
                "kind": e.kind,
                "summary": e.summary,
                "ref": e.ref,
                "created_by": e.created_by,
                "created_at": _dt(e.created_at),
            }
            for e in incident.evidence
        ]
    else:
        # 非工作人员：可见证据类型与摘要，定位信息（ref）裁剪
        evidence = [
            {"evidence_id": e.evidence_id, "kind": e.kind, "summary": e.summary, "ref_redacted": True}
            for e in incident.evidence
        ]

    return {
        "incident_id": incident.incident_id,
        "title": incident.title,
        "description": description,
        "description_redacted": description_redacted,
        "location": incident.location,
        "severity": incident.severity,
        "state": incident.state,
        "owner_org": incident.owner_org,
        "occurred_at": _dt(incident.occurred_at),
        "created_at": _dt(incident.created_at),
        "updated_at": _dt(incident.updated_at),
        "closed_at": _dt(incident.closed_at),
        "sources": sources,
        "source_count": len(incident.sources),
        "merged_from": incident.merged_from,
        "people": [person_to_dict(p, viewer) for p in incident.people],
        "evidence": evidence,
        "timeline": [
            {
                "event_id": t.event_id,
                "at": _dt(t.at),
                "kind": t.kind,
                "actor": t.actor_name,
                "summary": t.summary,
                "ref": t.ref,
            }
            for t in incident.timeline
        ],
        "measures": [measure_to_dict(m) for m in incident.measures],
        "tasks": [task_to_dict(t) for t in incident.tasks],
        "verifications": [verification_to_dict(v, viewer) for v in incident.verifications],
        "viewer": {"user_id": viewer.user_id, "name": viewer.name, "role": viewer.role},
    }
