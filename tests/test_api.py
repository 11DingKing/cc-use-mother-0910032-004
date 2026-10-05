"""HTTP API 端到端测试：鉴权头、路由、裁剪后的响应与措施完成依据展示。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from safety_incidents.api import build_server
from safety_incidents.clock import FixedClock
from safety_incidents.seed import seed_users


class ApiClient:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, user_id: str | None = None, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urlrequest.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if user_id:
            req.add_header("X-User-Id", user_id)
        try:
            with urlrequest.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.clock = FixedClock()
        cls.httpd, cls.state = build_server("127.0.0.1", 0, clock=cls.clock)
        seed_users(cls.state.repo)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.httpd.server_address[1]}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def test_health_and_users(self):
        status, body = self.api.call("GET", "/health")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body["status"], "ok")
        status, body = self.api.call("GET", "/users")
        self.assertGreaterEqual(len(body["users"]), 4)

    def test_end_to_end_scenario(self):
        # 1) 场馆建档
        status, venue = self.api.call("POST", "/incidents/report", "u-venue-op", {
            "reporter_org": "场馆",
            "title": "研学活动轻微擦伤",
            "summary": "学生在台阶处滑倒",
        })
        self.assertEqual(status, HTTPStatus.CREATED)
        inc_id = venue["incident_id"]

        # 2) 安全员重复建档
        status, officer = self.api.call("POST", "/incidents/report", "u-officer", {
            "reporter_org": "安全员",
            "title": "研学活动轻微擦伤",
            "summary": "巡查发现同一事件",
            "external_ref": "SAFE-2026-77",
        })
        dup_id = officer["incident_id"]

        # 志愿者不能建档
        status, body = self.api.call("POST", "/incidents/report", "u-volunteer", {
            "reporter_org": "场馆", "title": "x", "summary": "y",
        })
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)

        # 3) 提交确认
        self.api.call("POST", f"/incidents/{inc_id}/submit", "u-venue-op")
        self.api.call("POST", f"/incidents/{inc_id}/confirm", "u-manager", {"note": "监控核实"})
        self.api.call("POST", f"/incidents/{inc_id}/start", "u-venue-op")

        # 4) 登记学生（含敏感字段）与证据
        status, person = self.api.call("POST", f"/incidents/{inc_id}/people", "u-venue-op", {
            "label": "受伤学生", "name": "王小明", "contact": "13800000000",
            "health_summary": "右膝表皮擦伤", "medical_action": "碘伏消毒",
            "guardian_user_id": "u-guardian",
        })
        self.assertEqual(status, HTTPStatus.CREATED)
        status, ev = self.api.call("POST", f"/incidents/{inc_id}/evidence", "u-venue-op", {
            "summary": "监控与现场照片", "kind": "照片",
        })
        ev_id = ev["evidence_id"]

        # 5) 临时措施（带依据）并完成（带完成依据）
        status, measure = self.api.call("POST", f"/incidents/{inc_id}/measures", "u-venue-op", {
            "title": "医务室冰敷观察",
            "description": "观察 30 分钟",
            "basis": "证据显示表皮擦伤，需常规处置",
            "related_evidence_ids": [ev_id],
        })
        msr_id = measure["measure_id"]
        self.assertFalse(measure["completed"])
        status, done = self.api.call(
            "POST", f"/incidents/{inc_id}/measures/{msr_id}/complete", "u-venue-op",
            {"completion_basis": "值班医生签字单 DOC-5，观察无异常"},
        )
        self.assertTrue(done["completed"])

        # 6) 指派有截止时间的任务
        due_at = (self.clock.now() + timedelta(hours=2)).isoformat()
        status, task = self.api.call("POST", f"/incidents/{inc_id}/tasks", "u-venue-op", {
            "title": "当晚回访家长",
            "description": "确认无不适",
            "assignee_id": "u-officer",
            "due_at": due_at,
            "basis": "医务室处置建议",
        })
        tsk_id = task["task_id"]

        # 7) 合并安全员重复建档，来源保留
        status, merged = self.api.call("POST", "/incidents/merge", "u-manager", {
            "primary_id": inc_id, "duplicate_id": dup_id, "reason": "同一事件",
        })
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(len(merged["sources"]), 2)
        self.assertEqual(merged["merged_source_incident_ids"], [dup_id])

        # 8) 升级：申请 + 他人独立核验
        status, vrf = self.api.call(
            "POST", f"/incidents/{inc_id}/verifications", "u-venue-op",
            {"action": "升级", "reason": "伤口红肿", "payload": {"target_severity": 3}},
        )
        # 申请人本人核验被拒绝
        status, body = self.api.call(
            "POST", f"/verifications/{vrf['verification_id']}/decision", "u-venue-op",
            {"approve": True},
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        status, decision = self.api.call(
            "POST", f"/verifications/{vrf['verification_id']}/decision", "u-manager",
            {"approve": True, "decision_note": "同意升级送医排查"},
        )
        self.assertEqual(decision["status"], "通过")

        # 9) 逾期：拨钟并扫描
        self.clock.advance(timedelta(hours=3))
        status, sweep = self.api.call("POST", "/system/sweep-reminders", "u-venue-op")
        self.assertEqual(sweep["created_count"], 1)
        self.assertEqual(sweep["reminders"][0]["level"], 1)
        # 志愿者不能触发扫描
        status, _ = self.api.call("POST", "/system/sweep-reminders", "u-volunteer")
        self.assertEqual(status, HTTPStatus.FORBIDDEN)

        # 10) GET 事件详情：运营可见完整字段与措施完成依据
        status, detail = self.api.call("GET", f"/incidents/{inc_id}", "u-venue-op")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(detail["severity"], 3)
        measure_view = next(m for m in detail["measures"] if m["measure_id"] == msr_id)
        self.assertTrue(measure_view["completed"])
        self.assertIn("签字单", measure_view["completion_basis"])
        self.assertTrue(measure_view["basis"])
        task_view = next(t for t in detail["tasks"] if t["task_id"] == tsk_id)
        self.assertTrue(task_view["overdue"])
        self.assertEqual(len(task_view["reminders"]), 1)

        # 11) 志愿者视图：身份与健康字段全部脱敏
        status, volunteer_view = self.api.call("GET", f"/incidents/{inc_id}", "u-volunteer")
        p = volunteer_view["people"][0]
        self.assertEqual(p["name"], "***")
        self.assertEqual(p["health_summary"], "***")
        self.assertTrue(p["fields_redacted"]["identity"])
        self.assertTrue(p["fields_redacted"]["health"])
        # 措施完成情况（不含敏感信息）志愿者仍可看到
        m_vol = volunteer_view["measures"][0]
        self.assertTrue(m_vol["completed"])
        self.assertTrue(m_vol["completion_basis"])

        # 12) 监护人视图：只看到自己孩子且健康字段可见
        status, guardian_view = self.api.call("GET", f"/incidents/{inc_id}", "u-guardian")
        self.assertEqual(len(guardian_view["people"]), 1)
        self.assertEqual(guardian_view["people"][0]["name"], "王小明")
        self.assertEqual(guardian_view["people"][0]["health_summary"], "右膝表皮擦伤")

        # 13) 列表与 404 / 缺鉴权头
        status, listing = self.api.call("GET", "/incidents", "u-manager")
        self.assertEqual(listing["count"], 1)  # 被合并事件不出现
        status, _ = self.api.call("GET", f"/incidents/{dup_id}", "u-manager")
        self.assertEqual(status, HTTPStatus.OK)  # 直接按编号仍可查（含 merged_into 标记）
        status, _ = self.api.call("GET", "/incidents/NOPE", "u-manager")
        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        status, _ = self.api.call("GET", "/incidents")
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)

        # 14) 时间线完整可审计
        kinds = {e["kind"] for e in detail["timeline"]}
        for expected in ("建报", "合并", "核验申请", "核验通过", "逾期提醒", "措施完成"):
            self.assertIn(expected, kinds)

    def test_school_channel_requires_guardian(self):
        status, body = self.api.call("POST", "/incidents/report", "u-venue-op", {
            "reporter_org": "学校", "title": "学校建档", "summary": "x",
        })
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        status, inc = self.api.call("POST", "/incidents/report", "u-school", {
            "reporter_org": "学校", "title": "学校建档", "summary": "家长报平安",
        })
        self.assertEqual(status, HTTPStatus.CREATED)
        self.assertEqual(inc["sources"][0]["reporter_org"], "学校")


if __name__ == "__main__":
    unittest.main()
