"""核心领域规则测试：时间线、合并保源、独立核验、注入时钟、字段分层。"""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from safety_incidents.clock import FixedClock
from safety_incidents.models import (
    IncidentStatus,
    TaskStatus,
    User,
    UserRole,
    VerificationAction,
    VerificationStatus,
)
from safety_incidents.repository import Repository
from safety_incidents.serializers import serialize_incident
from safety_incidents.service import DomainError, IncidentService


def make_world(clock=None):
    repo = Repository()
    op = User("op", "林运营", UserRole.VENUE_OPERATOR, "文博中心")
    volunteer = User("vol", "小陈", UserRole.VOLUNTEER, "文博中心")
    guardian = User("g1", "王家长", UserRole.GUARDIAN, "第三中学")
    manager_a = User("m1", "周馆长", UserRole.VENUE_MANAGER, "文博中心")
    manager_b = User("m2", "吴馆长", UserRole.VENUE_MANAGER, "文博中心")
    school = User("s1", "李老师", UserRole.GUARDIAN, "第三中学")
    for u in (op, volunteer, guardian, manager_a, manager_b, school):
        repo.add_user(u)
    return repo, IncidentService(repo, clock or FixedClock()), {
        "op": op,
        "volunteer": volunteer,
        "guardian": guardian,
        "manager_a": manager_a,
        "manager_b": manager_b,
        "school": school,
    }


class TimelineAndRecordsTest(unittest.TestCase):
    def test_full_flow_records_timeline_evidence_measures_tasks(self):
        clock = FixedClock()
        repo, svc, u = make_world(clock)
        incident = svc.report_incident(
            u["op"], "场馆", "研学活动轻微擦伤", "学生在台阶处滑倒擦伤膝盖",
            description="学生团队活动",
        )
        svc.submit_for_confirmation(u["op"], incident.incident_id)
        svc.confirm_incident(u["manager_a"], incident.incident_id, "监控佐证")
        svc.start_execution(u["op"], incident.incident_id)

        person = svc.add_person(
            u["op"], incident.incident_id, "受伤学生", "王小明",
            contact="138****0000", health_summary="右膝表皮擦伤",
            medical_action="碘伏消毒并贴敷敷料", guardian_user_id="g1",
        )
        ev = svc.add_evidence(
            u["op"], incident.incident_id, "现场照片与监控时间戳",
            kind="照片", source_ref="bucket/ev-1.jpg",
        )
        measure = svc.add_measure(
            u["op"], incident.incident_id, "现场冰敷与休息",
            "在医务室观察 30 分钟", basis=f"证据 {ev.evidence_id}：擦伤处置需要",
            related_evidence_ids=[ev.evidence_id],
        )
        due = clock.now() + timedelta(hours=2)
        task = svc.add_task(
            u["op"], incident.incident_id, "更换敷料并回访", "当晚回访家长",
            assignee_id="op", due_at=due, basis="医务室处置建议",
        )

        svc.complete_measure(
            u["op"], incident.incident_id, measure.measure_id,
            completion_basis="值班记录签字：观察 30 分钟无异常",
        )
        clock.advance(timedelta(hours=3))
        svc.complete_task(
            u["op"], incident.incident_id, task.task_id,
            completion_basis="电话回访录音编号 RC-22",
        )

        saved = repo.get_incident(incident.incident_id)
        kinds = [e.kind for e in saved.timeline]
        for expected in ("建报", "状态变更", "证据", "措施", "措施完成", "任务", "任务完成"):
            self.assertIn(expected, kinds)
        self.assertEqual(saved.people[0].person_id, person.person_id)
        self.assertTrue(saved.measures[0].completed_at is not None)
        self.assertEqual(saved.tasks[0].status, TaskStatus.DONE)
        # 措施必须有依据，完成必须有完成依据
        self.assertTrue(saved.measures[0].basis)
        self.assertTrue(saved.measures[0].completion_basis)

    def test_measure_requires_basis(self):
        repo, svc, u = make_world()
        incident = svc.report_incident(u["op"], "场馆", "事件", "摘要")
        with self.assertRaises(DomainError):
            svc.add_measure(u["op"], incident.incident_id, "措施", "", basis="   ")
        measure = svc.add_measure(
            u["op"], incident.incident_id, "措施", "", basis="伤情判断"
        )
        with self.assertRaises(DomainError):
            svc.complete_measure(u["op"], incident.incident_id, measure.measure_id, "  ")
        with self.assertRaises(KeyError):
            svc.complete_measure(u["op"], incident.incident_id, "MSR-9999", "依据")


class MergeTest(unittest.TestCase):
    def test_duplicate_reports_merge_but_sources_remain(self):
        repo, svc, u = make_world()
        venue_inc = svc.report_incident(u["op"], "场馆", "同一擦伤事件（场馆档）", "场馆记录")
        officer_inc = svc.report_incident(
            u["op"], "安全员", "同一擦伤事件（安全员档）", "安全员巡查记录",
            external_ref="SAFE-2026-77",
        )
        school_inc = svc.report_incident(
            u["school"], "学校", "同一擦伤事件（学校档）", "学校报平安记录",
        )
        svc.add_evidence(u["op"], officer_inc.incident_id, "安全员执法记录仪摘要")
        svc.add_evidence(u["op"], venue_inc.incident_id, "场馆监控摘要")

        svc.merge_reports(u["manager_a"], venue_inc.incident_id, officer_inc.incident_id,
                          reason="时间地点人员一致")
        svc.merge_reports(u["manager_a"], venue_inc.incident_id, school_inc.incident_id)

        merged = repo.get_incident(venue_inc.incident_id)
        orgs = {s.reporter_org.value for s in merged.sources}
        self.assertEqual(orgs, {"安全员", "场馆", "学校"})
        # 原外部单号、建档人全部保留
        self.assertIn("SAFE-2026-77", [s.external_ref for s in merged.sources])
        self.assertEqual(len(merged.evidence), 2)
        self.assertEqual(merged.merged_source_incident_ids,
                         [officer_inc.incident_id, school_inc.incident_id])
        # 被合并事件标记去向，默认列表不再展示
        self.assertEqual(repo.get_incident(school_inc.incident_id).merged_into,
                         venue_inc.incident_id)
        visible = repo.list_incidents()
        self.assertEqual([i.incident_id for i in visible], [venue_inc.incident_id])
        # 合并动作进入时间线，保证可追溯
        self.assertTrue(any(e.kind == "合并" for e in merged.timeline))

    def test_cannot_merge_self_or_archived(self):
        repo, svc, u = make_world()
        a = svc.report_incident(u["op"], "场馆", "A", "")
        b = svc.report_incident(u["op"], "场馆", "B", "")
        with self.assertRaises(DomainError):
            svc.merge_reports(u["manager_a"], a.incident_id, a.incident_id)
        # 归档 b：提交→确认→执行→核验结案
        svc.submit_for_confirmation(u["op"], b.incident_id)
        svc.confirm_incident(u["manager_a"], b.incident_id)
        svc.start_execution(u["op"], b.incident_id)
        req = svc.request_verification(u["op"], b.incident_id, "结案", "处置完毕")
        svc.decide_verification(u["manager_b"], req.verification_id, True)
        with self.assertRaises(DomainError):
            svc.merge_reports(u["manager_a"], a.incident_id, b.incident_id)


class VerificationTest(unittest.TestCase):
    def _confirmed_running(self, svc, u):
        inc = svc.report_incident(u["op"], "场馆", "待处置事件", "摘要")
        svc.submit_for_confirmation(u["op"], inc.incident_id)
        svc.confirm_incident(u["manager_a"], inc.incident_id)
        svc.start_execution(u["op"], inc.incident_id)
        return inc

    def test_escalation_needs_independent_verifier(self):
        repo, svc, u = make_world()
        inc = self._confirmed_running(svc, u)
        req = svc.request_verification(
            u["op"], inc.incident_id, "升级", "出现肿胀需送医",
            payload={"target_severity": 3},
        )
        # 生效前沿线状态不变
        self.assertEqual(repo.get_incident(inc.incident_id).severity, 1)
        # 申请人本人不能核验
        with self.assertRaises(DomainError):
            svc.decide_verification(u["op"], req.verification_id, True)
        # 志愿者无权核验
        with self.assertRaises(DomainError):
            svc.decide_verification(u["volunteer"], req.verification_id, True)
        svc.decide_verification(u["manager_b"], req.verification_id, True, "同意升级")
        self.assertEqual(repo.get_incident(inc.incident_id).severity, 3)
        decided = repo.get_incident(inc.incident_id).verifications[-1]
        self.assertIsNotNone(decided.verifier_id)
        self.assertNotEqual(decided.verifier_id, decided.requested_by)

    def test_rejected_verification_has_no_effect(self):
        repo, svc, u = make_world()
        inc = self._confirmed_running(svc, u)
        req = svc.request_verification(
            u["op"], inc.incident_id, "转交", "学校想接管",
            payload={"target_org": "第三中学"},
        )
        svc.decide_verification(u["manager_b"], req.verification_id, False, "材料不足")
        saved = repo.get_incident(inc.incident_id)
        self.assertEqual(saved.owner_org, "文博中心")
        self.assertEqual(saved.verifications[-1].status, VerificationStatus.REJECTED)
        # 同一申请不能重复决定
        with self.assertRaises(DomainError):
            svc.decide_verification(u["manager_b"], req.verification_id, True)

    def test_close_blocked_by_open_tasks_then_reopen_flow(self):
        repo, svc, u = make_world(FixedClock())
        inc = self._confirmed_running(svc, u)
        task = svc.add_task(
            u["op"], inc.incident_id, "回访", "", assignee_id="op",
            due_at=svc.clock.now() + timedelta(days=1), basis="处置规范",
        )
        close_req = svc.request_verification(u["op"], inc.incident_id, "结案", "初判无碍")
        with self.assertRaises(DomainError):
            svc.decide_verification(u["manager_b"], close_req.verification_id, True)
        # 申请保持待决，事件未归档，且未写入核验人（可换人重新核验）
        pending = repo.get_incident(inc.incident_id).verifications[-1]
        self.assertEqual(pending.status, VerificationStatus.PENDING)
        self.assertIsNone(pending.verifier_id)
        self.assertEqual(repo.get_incident(inc.incident_id).status,
                         IncidentStatus.IN_PROGRESS)

        svc.complete_task(u["op"], inc.incident_id, task.task_id, "回访完成")
        svc.decide_verification(u["manager_b"], close_req.verification_id, True, "任务齐")
        self.assertEqual(repo.get_incident(inc.incident_id).status,
                         IncidentStatus.ARCHIVED)

        # 复开同样要独立核验
        reopen = svc.request_verification(u["op"], inc.incident_id, "复开", "家长反馈反复")
        svc.decide_verification(u["manager_a"], reopen.verification_id, True)
        self.assertEqual(repo.get_incident(inc.incident_id).status,
                         IncidentStatus.IN_PROGRESS)

    def test_invalid_escalation_rejected_at_request(self):
        repo, svc, u = make_world()
        inc = self._confirmed_running(svc, u)
        with self.assertRaises(DomainError):
            svc.request_verification(u["op"], inc.incident_id, "升级", "理由",
                                     payload={"target_severity": 1})
        with self.assertRaises(DomainError):
            svc.request_verification(u["op"], inc.incident_id, "未知动作", "理由")


class ReminderClockTest(unittest.TestCase):
    def test_overdue_reminders_follow_injected_clock_and_are_idempotent(self):
        clock = FixedClock()
        repo, svc, u = make_world(clock)
        inc = svc.report_incident(u["op"], "场馆", "有时限的任务", "")
        svc.submit_for_confirmation(u["op"], inc.incident_id)
        svc.confirm_incident(u["manager_a"], inc.incident_id)
        task = svc.add_task(
            u["op"], inc.incident_id, "2 小时内提交整改说明", "",
            assignee_id="op", due_at=clock.now() + timedelta(hours=2),
            basis="安全员要求",
        )

        # 未逾期：无提醒
        self.assertEqual(svc.sweep_reminders(), [])
        self.assertFalse(repo.get_incident(inc.incident_id).get_task(task.task_id).overdue)

        # 越过截止时间：第 1 次提醒
        clock.advance(timedelta(hours=3))
        first = svc.sweep_reminders()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].level, 1)
        # 幂等：立即再扫不重复
        self.assertEqual(svc.sweep_reminders(), [])

        # 24 小时阶梯：第 2 次
        clock.advance(timedelta(hours=24))
        second = svc.sweep_reminders()
        self.assertEqual([r.level for r in second], [2])

        # 72 小时阶梯：第 3 次
        clock.advance(timedelta(hours=48))
        third = svc.sweep_reminders()
        self.assertEqual([r.level for r in third], [3])
        self.assertEqual(svc.sweep_reminders(), [])

        saved_task = repo.get_incident(inc.incident_id).get_task(task.task_id)
        self.assertTrue(saved_task.overdue)
        self.assertEqual(len(saved_task.reminders), 3)

        # 完成后不再产生提醒
        svc.start_task(u["op"], inc.incident_id, task.task_id)
        svc.complete_task(u["op"], inc.incident_id, task.task_id, "补交整改说明")
        clock.advance(timedelta(days=10))
        self.assertEqual(svc.sweep_reminders(), [])
        self.assertFalse(
            repo.get_incident(inc.incident_id).get_task(task.task_id).overdue
        )

    def test_archived_incident_has_no_reminders(self):
        clock = FixedClock()
        repo, svc, u = make_world(clock)
        inc = svc.report_incident(u["op"], "场馆", "结案事件", "")
        svc.submit_for_confirmation(u["op"], inc.incident_id)
        svc.confirm_incident(u["manager_a"], inc.incident_id)
        svc.start_execution(u["op"], inc.incident_id)
        task = svc.add_task(
            u["op"], inc.incident_id, "t", "", assignee_id="op",
            due_at=clock.now() + timedelta(hours=1), basis="b",
        )
        svc.complete_task(u["op"], inc.incident_id, task.task_id, "done")
        req = svc.request_verification(u["op"], inc.incident_id, "结案", "ok")
        svc.decide_verification(u["manager_b"], req.verification_id, True)
        clock.advance(timedelta(days=30))
        self.assertEqual(svc.sweep_reminders(), [])


class FieldRedactionTest(unittest.TestCase):
    def _incident_with_student(self):
        repo, svc, u = make_world()
        inc = svc.report_incident(u["op"], "场馆", "擦伤", "摘要")
        svc.add_person(
            u["op"], inc.incident_id, "受伤学生", "王小明",
            contact="13800000000", health_summary="右膝擦伤",
            medical_action="碘伏消毒", guardian_user_id="g1",
        )
        svc.add_evidence(u["op"], inc.incident_id, "监控显示学生自行滑倒")
        return repo, svc, u, inc

    def test_volunteer_sees_neither_identity_nor_health(self):
        repo, svc, u, inc = self._incident_with_student()
        view = serialize_incident(repo.get_incident(inc.incident_id), u["volunteer"])
        person = view["people"][0]
        self.assertEqual(person["name"], "***")
        self.assertEqual(person["contact"], "***")
        self.assertEqual(person["health_summary"], "***")
        self.assertEqual(person["medical_action"], "***")
        self.assertTrue(person["fields_redacted"]["identity"])
        self.assertTrue(person["fields_redacted"]["health"])
        # 志愿者连建档外部单号也看不到
        self.assertTrue(all(s["external_ref"] in ("", "***") for s in view["sources"]))

    def test_guardian_sees_only_own_child(self):
        repo, svc, u, inc = self._incident_with_student()
        view = serialize_incident(repo.get_incident(inc.incident_id), u["guardian"])
        self.assertEqual(len(view["people"]), 1)
        self.assertEqual(view["people"][0]["name"], "王小明")
        self.assertEqual(view["people"][0]["health_summary"], "右膝擦伤")
        # 其他监护人看不到任何人员
        other = User("g9", "别的家长", UserRole.GUARDIAN, "第三中学")
        repo.add_user(other)
        view_other = serialize_incident(repo.get_incident(inc.incident_id), other)
        self.assertEqual(view_other["people"], [])

    def test_manager_and_operator_see_full_fields(self):
        repo, svc, u, inc = self._incident_with_student()
        for key in ("manager_a", "op"):
            view = serialize_incident(repo.get_incident(inc.incident_id), u[key])
            person = view["people"][0]
            self.assertEqual(person["name"], "王小明")
            self.assertEqual(person["health_summary"], "右膝擦伤")
            self.assertFalse(person["fields_redacted"]["health"])

    def test_volunteer_cannot_report_or_manage(self):
        repo, svc, u, inc = self._incident_with_student()
        with self.assertRaises(DomainError):
            svc.report_incident(u["volunteer"], "场馆", "x", "y")
        with self.assertRaises(DomainError):
            svc.add_measure(u["volunteer"], inc.incident_id, "m", "", basis="b")


if __name__ == "__main__":
    unittest.main()
