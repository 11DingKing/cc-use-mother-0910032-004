"""领域服务测试：合单留源、独立核验、逾期时钟、角色裁剪、措施依据。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from safety_service.clock import FixedClock
from safety_service.errors import Conflict, PermissionDenied, ValidationError
from safety_service.models import (
    ARCHIVED,
    CONFIRMED,
    IN_PROGRESS,
    PENDING_VERIFY,
    User,
)
from safety_service.repository import Repository
from safety_service.serializers import incident_to_dict
from safety_service.service import SafetyService

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def make_service() -> tuple[SafetyService, FixedClock]:
    clock = FixedClock(T0)
    svc = SafetyService(Repository(), clock)
    return svc, clock


def users() -> dict[str, User]:
    return {
        "op": User("u-op-1", "王敏", "venue_operator", "文博中心"),
        "vol": User("u-vol-1", "李小爱", "volunteer", "志愿者团队"),
        "guard": User("u-guard-1", "张建国", "guardian", ""),
        "mgr": User("u-mgr-1", "赵馆长", "venue_manager", "文博中心"),
        "safe": User("u-safe-1", "陈安全", "safety_officer", "场馆安全办"),
        "school": User("u-school-1", "孙教务", "school_admin", "第三中学"),
    }


class IncidentLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.u = users()

    def _incident(self, actor="op"):
        return self.svc.create_incident(self.u[actor], {
            "title": "学生轻微擦伤",
            "description": "活动中学生摔倒，膝盖轻微擦伤",
            "location": "文博中心三层活动室",
            "severity": "low",
            "report_content": "场馆前台接到带队老师口头报告",
        })

    def test_multi_source_reports_preserved_after_merge(self) -> None:
        """场馆、安全员、学校分别建档 → 合单后来源逐条保留，后续操作可对应原事件。"""
        venue = self._incident("op")
        self.clock.advance(minutes=30)
        safety = self.svc.create_incident(self.u["safe"], {
            "title": "擦伤事件（安全办建档）",
            "description": "安全办巡查记录",
            "location": "文博中心三层活动室",
            "report_content": "安全员现场查看，学生膝盖擦伤，已消毒",
        })
        self.clock.advance(minutes=20)
        school = self.svc.create_incident(self.u["school"], {
            "title": "校外活动受伤（学校建档）",
            "description": "学校建档跟进",
            "location": "文博中心三层活动室",
            "report_content": "学校教务接带队老师报告，建立健康跟进档案",
        })

        # 创建响应会提示潜在重复
        candidates = self.svc.find_duplicate_candidates(self.u["op"], "文博中心三层活动室", T0)
        self.assertEqual({c.incident_id for c in candidates}, {venue.incident_id, safety.incident_id, school.incident_id})

        merged = self.svc.merge_incidents(self.u["mgr"], safety.incident_id, venue.incident_id, reason="同一场活动同一伤情")
        merged = self.svc.merge_incidents(self.u["mgr"], school.incident_id, merged.incident_id)

        self.assertEqual(len(merged.sources), 3)
        orgs = {s.org for s in merged.sources}
        self.assertEqual(orgs, {"文博中心", "场馆安全办", "第三中学"})
        self.assertEqual(set(merged.merged_from), {safety.incident_id, school.incident_id})

        # 被合并事件访问自动导向主事件，措施/任务不会游离
        self.assertEqual(self.svc.get_incident(self.u["op"], safety.incident_id).incident_id, venue.incident_id)

        # 志愿者不能合并
        with self.assertRaises(PermissionDenied):
            self.svc.merge_incidents(self.u["vol"], school.incident_id, venue.incident_id)

    def test_timeline_records_every_action(self) -> None:
        inc = self._incident()
        self.svc.add_evidence(self.u["safe"], inc.incident_id, {
            "kind": "photo", "summary": "膝盖擦伤照片", "ref": "s3://ev/photo-1.jpg"})
        self.svc.add_measure(self.u["safe"], inc.incident_id, {
            "title": "伤口消毒并冰敷", "basis_note": "现场急救箱处置"})
        kinds = [t.kind for t in inc.timeline]
        self.assertEqual(kinds, ["created", "evidence_added", "measure_added"])
        self.assertTrue(all(t.actor_name and t.summary for t in inc.timeline))

    def test_measure_completion_requires_basis_and_exposes_it(self) -> None:
        inc = self._incident()
        ev = self.svc.add_evidence(self.u["safe"], inc.incident_id, {
            "kind": "document", "summary": "校医室复诊记录", "ref": "s3://ev/note-1"})
        measure = self.svc.add_measure(self.u["safe"], inc.incident_id, {
            "title": "通知监护人并安排复诊", "basis_note": "伤情轻微但需观察"})

        with self.assertRaises(ValidationError):
            self.svc.resolve_measure(self.u["safe"], inc.incident_id, measure.measure_id, {"status": "completed"})
        with self.assertRaises(ValidationError):
            self.svc.resolve_measure(self.u["safe"], inc.incident_id, measure.measure_id,
                                     {"status": "completed", "completion_note": "已复诊", "basis_evidence_ids": ["EV-999"]})

        done = self.svc.resolve_measure(self.u["safe"], inc.incident_id, measure.measure_id, {
            "status": "completed", "completion_note": "监护人确认复诊无异常", "basis_evidence_ids": [ev.evidence_id]})
        self.assertTrue(done.is_completed)
        view = incident_to_dict(self.svc.get_incident(self.u["safe"], inc.incident_id), self.u["safe"])
        m = next(m for m in view["measures"] if m["measure_id"] == measure.measure_id)
        self.assertTrue(m["is_completed"])
        self.assertEqual(m["completion_note"], "监护人确认复诊无异常")
        self.assertEqual(m["basis_evidence_ids"], [ev.evidence_id])
        self.assertIsNotNone(m["completed_at"])

    def test_independent_verification_for_confirm_escalate_transfer_close_reopen(self) -> None:
        inc = self._incident()
        self.svc.add_person(self.u["op"], inc.incident_id, {
            "name": "张小明", "kind": "injured_student", "injury_description": "膝盖擦伤"})

        # 草拟 → 待核验：提报人不能核验自己
        self.svc.submit_for_verification(self.u["op"], inc.incident_id)
        self.assertEqual(inc.state, PENDING_VERIFY)
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_incident(self.u["op"], inc.incident_id)
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_incident(self.u["vol"], inc.incident_id)
        self.svc.confirm_incident(self.u["safe"], inc.incident_id, note="现场与监控核实一致")
        self.assertEqual(inc.state, CONFIRMED)
        self.svc.start_execution(self.u["op"], inc.incident_id)
        self.assertEqual(inc.state, IN_PROGRESS)

        # 升级：申请人不能自核
        req = self.svc.request_verification(self.u["op"], inc.incident_id,
                                            {"action": "escalate", "params": {"severity": "medium"}, "reason": "家长提出头晕"})
        with self.assertRaises(PermissionDenied):
            self.svc.decide_verification(self.u["op"], req.request_id, True)
        self.svc.decide_verification(self.u["mgr"], req.request_id, True, note="复核症状属实")
        self.assertEqual(inc.severity, "medium")
        # 升级必须真的升高
        with self.assertRaises(ValidationError):
            self.svc.request_verification(self.u["op"], inc.incident_id,
                                          {"action": "escalate", "params": {"severity": "low"}, "reason": "x"})

        # 转交：缺目标方被拒；通过后责任方变更
        with self.assertRaises(ValidationError):
            self.svc.request_verification(self.u["op"], inc.incident_id,
                                          {"action": "transfer", "params": {}, "reason": "x"})
        treq = self.svc.request_verification(self.u["op"], inc.incident_id,
                                             {"action": "transfer", "params": {"target_org": "第三中学"}, "reason": "后续复诊由学校跟进"})
        self.svc.decide_verification(self.u["school"], treq.request_id, True)
        self.assertEqual(inc.owner_org, "第三中学")

        # 结案：有未完成任务时拒绝
        task = self.svc.add_task(self.u["op"], inc.incident_id,
                                 {"title": "提交复诊结论", "assignee": "孙教务", "due_at": "2026-10-03T09:00:00+00:00"})
        creq = self.svc.request_verification(self.u["school"], inc.incident_id,
                                             {"action": "close", "reason": "全部措施完成"})
        with self.assertRaises(Conflict):
            self.svc.decide_verification(self.u["mgr"], creq.request_id, True)
        self.svc.complete_task(self.u["school"], task.task_id, "复诊结论已归档")
        # 同一申请在前置条件满足后仍可通过
        self.svc.decide_verification(self.u["mgr"], creq.request_id, True)
        self.assertEqual(inc.state, ARCHIVED)

        # 已处理的申请不能重复决定
        with self.assertRaises(Conflict):
            self.svc.decide_verification(self.u["safe"], creq.request_id, False)

        # 复开：同样需要非本人独立核验
        rreq = self.svc.request_verification(self.u["school"], inc.incident_id,
                                             {"action": "reopen", "reason": "家长反馈伤口感染"})
        with self.assertRaises(PermissionDenied):
            self.svc.decide_verification(self.u["school"], rreq.request_id, True)
        self.svc.decide_verification(self.u["mgr"], rreq.request_id, True)
        self.assertEqual(inc.state, IN_PROGRESS)

    def test_rejected_verification_leaves_state_unchanged(self) -> None:
        inc = self._incident()
        self.svc.submit_for_verification(self.u["op"], inc.incident_id)
        self.svc.confirm_incident(self.u["safe"], inc.incident_id)
        req = self.svc.request_verification(self.u["op"], inc.incident_id,
                                            {"action": "escalate", "params": {"severity": "high"}, "reason": "疑似骨折"})
        self.svc.decide_verification(self.u["mgr"], req.request_id, False, note="复诊排除骨折")
        self.assertEqual(inc.severity, "low")
        self.assertEqual(req.status, "rejected")

    def test_overdue_and_due_soon_reminders_use_injected_clock(self) -> None:
        inc = self._incident()
        due = self.svc.add_task(self.u["op"], inc.incident_id,
                                {"title": "提交伤情跟进", "assignee": "陈安全", "due_at": "2026-10-02T09:00:00+00:00"})
        nodue = self.svc.add_task(self.u["op"], inc.incident_id,
                                  {"title": "无截止任务", "assignee": "王敏"})

        # 志愿者不能扫描提醒
        with self.assertRaises(PermissionDenied):
            self.svc.scan_due_tasks(self.u["vol"])

        # T0：距截止 24h，默认窗口内
        first = self.svc.scan_due_tasks(self.u["op"])
        self.assertEqual([r.kind for r in first], ["due_soon"])
        self.assertEqual(first[0].task_id, due.task_id)
        # 重复扫描不产生重复提醒
        self.assertEqual(self.svc.scan_due_tasks(self.u["op"]), [])

        # 推进到逾期
        self.clock.set(datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc))
        overdue = self.svc.scan_due_tasks(self.u["op"])
        self.assertEqual([r.kind for r in overdue], ["overdue"])
        self.assertEqual(self.svc.scan_due_tasks(self.u["op"]), [])
        # 无截止时间的任务永不提醒
        self.assertNotIn(nodue.task_id, {r.task_id for r in self.svc.repo.reminders})

        # 完成任务后不再提醒（新任务验证）
        t2 = self.svc.add_task(self.u["op"], inc.incident_id,
                               {"title": "回访", "assignee": "陈安全", "due_at": "2026-10-03T12:00:00+00:00"})
        self.svc.complete_task(self.u["op"], t2.task_id)
        self.assertEqual(self.svc.scan_due_tasks(self.u["op"]), [])

    def test_role_based_redaction(self) -> None:
        inc = self._incident()
        self.svc.add_person(self.u["op"], inc.incident_id, {
            "name": "张小明", "kind": "injured_student", "contact": "13800000000",
            "injury_description": "膝盖擦伤 2cm", "medical_notes": "无过敏史，已消毒",
            "guardian_user_id": "u-guard-1"})
        self.svc.add_source_report(self.u["school"], inc.incident_id, {"content": "报告中包含健康细节：xx"})
        self.svc.add_evidence(self.u["safe"], inc.incident_id,
                              {"kind": "photo", "summary": "伤情照片", "ref": "s3://ev/photo-1"})

        vol_view = incident_to_dict(inc, self.u["vol"])
        person = vol_view["people"][0]
        self.assertTrue(person["health_redacted"])
        self.assertNotIn("injury_description", person)
        self.assertNotIn("medical_notes", person)
        self.assertNotIn("contact", person)
        self.assertTrue(person["identity_redacted"])
        self.assertEqual(person["name"], "张**")
        self.assertTrue(vol_view["description_redacted"])
        self.assertTrue(all(s["content_redacted"] for s in vol_view["sources"]))
        self.assertTrue(all("ref" not in e and e.get("ref_redacted") for e in vol_view["evidence"]))

        # 运营员：身份完整、健康裁剪
        op_view = incident_to_dict(inc, self.u["op"])
        self.assertEqual(op_view["people"][0]["contact"], "13800000000")
        self.assertTrue(op_view["people"][0]["health_redacted"])

        # 安全员：身份与健康均完整
        safe_view = incident_to_dict(inc, self.u["safe"])
        self.assertEqual(safe_view["people"][0]["injury_description"], "膝盖擦伤 2cm")
        self.assertEqual(safe_view["people"][0]["medical_notes"], "无过敏史，已消毒")

        # 监护人只能看到自己监护对象的健康字段
        guard_view = incident_to_dict(inc, self.u["guard"])
        self.assertEqual(guard_view["people"][0]["injury_description"], "膝盖擦伤 2cm")
        self.svc.add_person(self.u["op"], inc.incident_id, {
            "name": "李四", "kind": "injured_student", "injury_description": "他人伤情"})
        guard_view2 = incident_to_dict(inc, self.u["guard"])
        other = next(p for p in guard_view2["people"] if p["name"] == "李**")
        self.assertTrue(other["health_redacted"])

    def test_volunteer_cannot_create_incident_or_task(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.create_incident(self.u["vol"], {"title": "x", "location": "y"})
        inc = self._incident()
        with self.assertRaises(PermissionDenied):
            self.svc.add_task(self.u["vol"], inc.incident_id, {"title": "x", "assignee": "a"})


if __name__ == "__main__":
    unittest.main()
