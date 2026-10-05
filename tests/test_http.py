"""HTTP 端到端测试：真实起服务 + FixedClock，覆盖完整协同场景。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from safety_service.app import ApiHandler, build_service
from safety_service.clock import FixedClock
from safety_service.models import User
from safety_service.scheduler import ReminderScheduler

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class HttpScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(T0)
        self.service = build_service(self.clock)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        ApiHandler.service = self.service
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def req(self, method: str, path: str, user_id: str | None = "u-op-1", body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if user_id:
            request.add_header("X-User-Id", user_id)
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_scenario_over_http(self) -> None:
        # 1. 三方分别建档
        s, venue = self.req("POST", "/api/incidents", "u-op-1", {
            "title": "学生擦伤（场馆建档）", "location": "三层活动室",
            "description": "膝盖轻微擦伤", "report_content": "前台接带队老师报告"})
        self.assertEqual(s, 201)
        vid = venue["incident_id"]

        _, safety = self.req("POST", "/api/incidents", "u-safe-1", {
            "title": "学生擦伤（安全办建档）", "location": "三层活动室",
            "report_content": "安全员已到场消毒"})
        sid = safety["incident_id"]
        # 同地点 24h 内 → 提示潜在重复
        self.assertIn(vid, safety["duplicate_candidates"])

        _, school = self.req("POST", "/api/incidents", "u-school-1", {
            "title": "学生擦伤（学校建档）", "location": "三层活动室",
            "report_content": "学校教务建档跟进"})

        # 2. 无令牌 → 401
        s, err = self.req("GET", "/api/incidents", user_id=None)
        self.assertEqual(s, 401)

        # 3. 合单（志愿者无权限）
        s, err = self.req("POST", "/api/incidents/merge", "u-vol-1",
                          {"source_id": sid, "target_id": vid})
        self.assertEqual(s, 403)
        s, merged = self.req("POST", "/api/incidents/merge", "u-mgr-1",
                             {"source_id": sid, "target_id": vid, "reason": "同一伤情"})
        self.assertEqual(s, 200)
        s, merged = self.req("POST", "/api/incidents/merge", "u-mgr-1",
                             {"source_id": school["incident_id"], "target_id": vid})
        self.assertEqual(s, 200)
        self.assertEqual(merged["source_count"], 3)

        # 访问被并入的旧 ID 会落到主事件
        s, redirected = self.req("GET", f"/api/incidents/{sid}", "u-op-1")
        self.assertEqual(redirected["incident_id"], vid)

        # 4. 人员 / 证据 / 措施 / 任务
        s, person = self.req("POST", f"/api/incidents/{vid}/people", "u-op-1", {
            "name": "张小明", "kind": "injured_student", "contact": "13800000000",
            "injury_description": "膝盖擦伤", "medical_notes": "无过敏史",
            "guardian_user_id": "u-guard-1"})
        self.assertEqual(s, 201)

        s, ev = self.req("POST", f"/api/incidents/{vid}/evidence", "u-safe-1", {
            "kind": "document", "summary": "复诊记录", "ref": "s3://ev/note"})
        self.assertEqual(s, 201)

        s, measure = self.req("POST", f"/api/incidents/{vid}/measures", "u-safe-1", {
            "title": "消毒并安排复诊", "basis_note": "现场处置规范"})
        mid = measure["measure_id"]
        self.assertFalse(measure["is_completed"])

        s, task = self.req("POST", f"/api/incidents/{vid}/tasks", "u-op-1", {
            "title": "收回复诊结论", "assignee": "孙教务",
            "due_at": "2026-10-02T09:00:00+00:00"})
        self.assertEqual(s, 201)
        tid = task["task_id"]

        # 5. 措施完成缺依据 → 400；补依据 → 200 且 API 展示完成与依据
        s, err = self.req("POST", f"/api/incidents/{vid}/measures/{mid}/resolve", "u-safe-1",
                          {"status": "completed"})
        self.assertEqual(s, 400)
        s, done = self.req("POST", f"/api/incidents/{vid}/measures/{mid}/resolve", "u-safe-1", {
            "status": "completed", "completion_note": "复诊无异常，监护人确认",
            "basis_evidence_ids": [ev["evidence_id"]]})
        self.assertEqual(s, 200)
        self.assertTrue(done["is_completed"])
        self.assertEqual(done["completion_note"], "复诊无异常，监护人确认")

        # 6. 提报 → 非本人独立确认
        self.req("POST", f"/api/incidents/{vid}/submit", "u-op-1")
        s, err = self.req("POST", f"/api/incidents/{vid}/confirm", "u-op-1", {"note": "自核"})
        self.assertEqual(s, 403)
        s, confirmed = self.req("POST", f"/api/incidents/{vid}/confirm", "u-safe-1", {"note": "核实"})
        self.assertEqual(s, 200)
        self.assertEqual(confirmed["state"], "已确认")
        self.req("POST", f"/api/incidents/{vid}/start", "u-op-1")

        # 7. 完成任务后申请结案，非申请人核验通过
        self.req("POST", f"/api/tasks/{tid}/complete", "u-school-1", {"note": "结论已归档"})
        s, creq = self.req("POST", f"/api/incidents/{vid}/verifications", "u-school-1",
                           {"action": "close", "reason": "措施任务全部完成"})
        self.assertEqual(s, 201)
        s, decision = self.req("POST", f"/api/verifications/{creq['request_id']}/decide", "u-mgr-1",
                               {"approve": True, "note": "同意结案"})
        self.assertEqual(s, 200)
        self.assertEqual(decision["status"], "approved")
        _, closed = self.req("GET", f"/api/incidents/{vid}", "u-op-1")
        self.assertEqual(closed["state"], "已归档")

        # 8. 注入时钟推进 → 逾期提醒（任务已完成，不应提醒；新增一个逾期任务验证）
        _, t2 = self.req("POST", f"/api/incidents/{vid}/tasks", "u-op-1", {
            "title": "结案后回访", "assignee": "陈安全", "due_at": "2026-10-04T09:00:00+00:00"})
        # 结案状态也能扫描任务（任务扫描不依赖事件状态）
        self.clock.set(datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))
        s, reminders = self.req("GET", "/api/reminders/due", "u-op-1")
        self.assertEqual(s, 200)
        kinds = {r["task_id"]: r["kind"] for r in reminders["reminders"]}
        self.assertEqual(kinds.get(t2["task_id"]), "overdue")
        self.assertNotIn(tid, kinds)

        # 9. 志愿者视角：身份/健康/来源正文全部裁剪
        s, vol_view = self.req("GET", f"/api/incidents/{vid}", "u-vol-1")
        self.assertEqual(s, 200)
        p = vol_view["people"][0]
        self.assertNotIn("medical_notes", p)
        self.assertNotIn("contact", p)
        self.assertEqual(p["name"], "张**")
        self.assertTrue(all(src["content_redacted"] for src in vol_view["sources"]))
        self.assertTrue(vol_view["description_redacted"])

        # 10. 监护人视角：仅监护对象可见健康字段
        s, guard_view = self.req("GET", f"/api/incidents/{vid}", "u-guard-1")
        self.assertEqual(guard_view["people"][0]["medical_notes"], "无过敏史")

        # 时间线贯穿全程
        kinds = [t["kind"] for t in guard_view["timeline"]]
        for expected in ["created", "merged", "measure_resolved", "confirmed", "closed", "task_added"]:
            self.assertIn(expected, kinds)

    def test_scheduler_run_once_with_fixed_clock(self) -> None:
        # 直接在同服务上建任务，验证调度器 run_once 的确定性
        _, inc = self.req("POST", "/api/incidents", "u-safe-1",
                          {"title": "调度器场景", "location": "门厅"})
        _, task = self.req("POST", f"/api/incidents/{inc['incident_id']}/tasks", "u-op-1",
                           {"title": "逾期任务", "assignee": "陈安全",
                            "due_at": "2026-10-01T10:00:00+00:00"})
        self.clock.set(datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc))
        collected: list = []
        operator = self.service.repo.get_user("u-op-1")
        assert operator is not None
        scheduler = ReminderScheduler(self.service, operator, sink=collected.append)
        reminders = scheduler.run_once()
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].kind, "overdue")
        self.assertEqual(collected[0][0].task_id, task["task_id"])
        self.assertEqual(scheduler.run_once(), [])


if __name__ == "__main__":
    unittest.main()
