# -*- coding: utf-8 -*-
"""系统测试：格位竞争 / 标签失效 / 迁移完整 / 期限边界 / 租户隔离 / 角色权限 / 关门确认。
运行：python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fridge"))

import app  # noqa: E402
from app import BusinessError, Clock, Service, Store  # noqa: E402
import seed_data  # noqa: E402


class FridgeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fridge_test_")
        app.DB_PATH = os.path.join(self.tmp, "fridge.db")
        app.MEDIA_DIR = os.path.join(self.tmp, "media")
        self.clock = Clock()
        self.store = Store(self.clock)
        self.svc = Service(self.store, self.clock)
        seed_data.seed_master(self.store)

    def place(self, uid, cell, hours_ago=0):
        """完成一次贴签→关门存放。"""
        lab = self.svc.issue_label(uid)["code"]
        self.clock.freeze(self.clock.now() - timedelta(hours=hours_ago))
        tok = self.svc.start_store(uid, lab, cell)["token"]
        res = self.svc.confirm_store(uid, tok)
        self.clock.reset()
        return res["item_id"], lab

    def expect_error(self, code, fn, *a, **k):
        with self.assertRaises(BusinessError) as cm:
            fn(*a, **k)
        self.assertEqual(cm.exception.code, code, msg=str(cm.exception))
        return cm.exception


class TestCellCompetition(FridgeTestBase):
    def test_concurrent_first_wins(self):
        # U1、U2 同时开门抢同一格位：仅首个 start 成功
        lab1 = self.svc.issue_label("U1")["code"]
        lab2 = self.svc.issue_label("U2")["code"]
        t1 = self.svc.start_store("U1", lab1, "F1-S1-01")["token"]
        self.expect_error("cell_holding", self.svc.start_store, "U2", lab2, "F1-S1-01")

        # U2 改选其他格位，两人都可完成
        t2 = self.svc.start_store("U2", lab2, "F1-S1-02")["token"]
        self.svc.confirm_store("U1", t1)
        self.svc.confirm_store("U2", t2)
        self.assertEqual(set(self.svc.store.cell_occupancy().keys()),
                         {"F1-S1-01", "F1-S1-02"})

    def test_taken_cell_rejected(self):
        self.place("U1", "F1-S2-02")
        lab = self.svc.issue_label("U2")["code"]
        self.expect_error("cell_taken", self.svc.start_store, "U2", lab, "F1-S2-02")

    def test_one_item_per_cell_undo_releases(self):
        iid, lab = self.place("U1", "F1-S3-03")
        self.svc.undo_store("U1", iid)
        self.assertNotIn("F1-S3-03", self.svc.store.cell_occupancy())
        # 撤销释放标签占用但原签作废，需重新贴签
        self.expect_error("label_revoked",
                          lambda: self.svc.start_store("U1", lab, "F1-S3-03"))
        # 新签可立刻使用被释放的格位
        iid2, _ = self.place("U2", "F1-S3-03")
        self.assertIn("F1-S3-03", self.svc.store.cell_occupancy())


class TestLabelInvalid(FridgeTestBase):
    def test_unknown_revoked_duplicate(self):
        self.expect_error("bad_label", self.svc.start_store, "U1", "LNOPE0000", "F1-S1-01")

        code = self.svc.issue_label("U2")["code"]
        self.svc.revoke_label("U5", code)
        self.expect_error("label_revoked", self.svc.start_store, "U2", code, "F1-S1-01")

        iid, lab = self.place("U1", "F1-S1-03")
        # 同一签再次扫码 → 重复扫码
        self.expect_error("duplicate_scan", self.svc.start_store, "U1", lab, "F1-S1-04")


class TestDoorConfirm(FridgeTestBase):
    def test_confirm_without_session(self):
        self.expect_error("no_session", self.svc.confirm_store, "U1", "deadbeef")

    def test_door_timeout(self):
        lab = self.svc.issue_label("U1")["code"]
        tok = self.svc.start_store("U1", lab, "F1-S2-04")["token"]
        self.clock.advance(app.DOOR_CANCEL_SECONDS + 1)
        self.expect_error("store_timeout", self.svc.confirm_store, "U1", tok)
        # 失效会话不占格位，可重新存放
        self.place("U2", "F1-S2-04")

    def test_others_cannot_confirm(self):
        lab = self.svc.issue_label("U1")["code"]
        tok = self.svc.start_store("U1", lab, "F1-S3-01")["token"]
        self.expect_error("forbidden", self.svc.confirm_store, "U2", tok)


class TestDeadlineBoundary(FridgeTestBase):
    def test_expiry_boundary_48h(self):
        lab = self.svc.issue_label("U1")["code"]
        base = self.clock.now().replace(microsecond=0)
        self.clock.freeze(base)
        tok = self.svc.start_store("U1", lab, "F1-S1-01")["token"]
        self.svc.confirm_store("U1", tok)
        expire = base + timedelta(hours=app.DEFAULT_RETENTION_HOURS)

        self.clock.freeze(expire - timedelta(seconds=1))
        self.assertEqual(self.svc.scan_expiry(), [])
        self.assertEqual(len(self.svc.expiry_queue()), 0)

        self.clock.freeze(expire)   # 恰好到期即入队
        self.assertEqual(len(self.svc.scan_expiry()), 1)
        self.assertEqual(len(self.svc.expiry_queue()), 1)

    def test_appeal_window_boundary_24h(self):
        iid, _ = self.place("U1", "F1-S1-02", hours_ago=50)
        self.svc.scan_expiry()
        self.clock.freeze(self.clock.now())
        res = self.svc.clean_items("U6", [iid])
        deadline = datetime.strptime(res["appeal_deadline"], "%Y-%m-%d %H:%M:%S")

        self.clock.freeze(deadline - timedelta(seconds=1))
        self.assertEqual(self.svc.scan_appeal_deadline(), [])
        self.assertEqual(self.svc.store.item(iid)["state"], "removed")

        self.clock.freeze(deadline)   # 申诉期满自动处置
        self.assertEqual(self.svc.scan_appeal_deadline(), [iid])
        self.assertEqual(self.svc.store.item(iid)["state"], "closed")

    def test_appeal_within_window_returns(self):
        iid, _ = self.place("U1", "F1-S4-01", hours_ago=50)
        self.svc.scan_expiry()
        self.svc.clean_items("U6", [iid])
        self.svc.appeal("U1", iid, "出差未取")
        r = self.svc.review_appeal("U4", iid, "return")
        self.assertIn("F", r["cell"])
        self.assertEqual(self.svc.store.item(iid)["state"], "stored")
        self.assertIn(r["cell"], self.svc.store.cell_occupancy())


class TestMigrationIntegrity(FridgeTestBase):
    def _setup_outage(self):
        fresh1, l1 = self.place("U2", "F3-S1-01", hours_ago=4)
        fresh2, l2 = self.place("U1", "F3-S2-02", hours_ago=30)
        due1, l3 = self.place("U2", "F3-S4-03", hours_ago=55)
        self.svc.scan_expiry()
        self.svc.begin_outage("U7", "F3")
        return fresh1, fresh2, due1, l1, l2

    def test_plan_preview(self):
        f1, f2, due1, l1, l2 = self._setup_outage()
        plan = {p["item_id"]: p for p in self.svc.migration_targets("F3")}
        self.assertIsNotNone(plan[f1]["to"])
        self.assertIsNotNone(plan[f2]["to"])
        self.assertNotEqual(plan[f1]["to"], plan[f2]["to"])   # 不撞格
        self.assertIsNone(plan[due1]["to"])                    # 到期不迁移
        self.assertNotIn("F3", plan[f1]["to"])

    def test_migrate_keeps_labels_and_cells(self):
        f1, f2, due1, l1, l2 = self._setup_outage()
        r = self.svc.migrate_outage("U7", "F3", missing_ids=[f2])

        self.assertEqual(r["moved"], [f1])
        self.assertEqual(r["missing"], [f2])
        self.assertEqual(r["skipped_expired"], [due1])

        it = self.svc.store.item(f1)
        self.assertFalse(it["cell"].startswith("F3"))
        # 保留原标签：标签码不变、仍为已使用状态
        self.assertEqual(it["label"], l1)
        self.assertEqual(self.store.get_label(l1)["used"], 1)

        # 缺件登记异常且物品状态为 missing
        issues = self.svc.issues(kind="migration_missing")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["item_id"], f2)
        self.assertEqual(self.svc.store.item(f2)["state"], "missing")

        # 到期物品仍留在到期队列（转清理，不迁移）
        self.assertIn(due1, [q["item_id"] for q in self.svc.expiry_queue()])

        # 目标格位占用唯一
        occ = self.store.cell_occupancy()
        self.assertEqual(len([c for c in occ if not c.startswith("F3")]), 1)


class TestTenantIsolationAndRoles(FridgeTestBase):
    def test_cannot_use_other_dept_label(self):
        lab = self.svc.issue_label("U1")["code"]      # D1 市场
        self.expect_error("tenant_isolation",
                          self.svc.start_store, "U2", lab, "F1-S1-01")  # U2 是 D2 研发

    def test_appeal_isolation(self):
        iid, _ = self.place("U1", "F1-S1-04", hours_ago=50)   # D1 物品
        self.svc.scan_expiry()
        self.svc.clean_items("U6", [iid])
        self.expect_error("tenant_isolation", self.svc.appeal, "U2", iid, "不是我的")
        self.expect_error("tenant_isolation", self.svc.claim, "U2", iid)
        # 本部门员工可申诉
        self.svc.appeal("U1", iid, "我的")

    def test_admin_review_isolation(self):
        iid, _ = self.place("U2", "F1-S2-04", hours_ago=50)   # D2 物品
        self.svc.scan_expiry()
        self.svc.clean_items("U6", [iid])
        self.svc.appeal("U2", iid, "有异议")
        # D1 租户管理员不能复核 D2 争议
        self.expect_error("tenant_isolation",
                          self.svc.review_appeal, "U4", iid, "uphold")
        # D2 租户管理员可以
        self.svc.review_appeal("U5", iid, "uphold")
        self.assertEqual(self.svc.store.item(iid)["state"], "closed")

    def test_role_matrix(self):
        iid, _ = self.place("U1", "F1-S3-02", hours_ago=50)
        self.svc.scan_expiry()
        # 员工不能清理
        self.expect_error("forbidden", self.svc.clean_items, "U1", [iid])
        # 保洁可以清理
        self.svc.clean_items("U6", [iid])
        # 保洁不能停机/迁移/复核申诉/关异常
        self.expect_error("forbidden", self.svc.begin_outage, "U6", "F4")
        self.expect_error("forbidden", self.svc.review_appeal, "U6", iid, "uphold")
        iss = self.svc.report_unlabeled("U6", "F1", "F1-S4-04")
        self.expect_error("forbidden", self.svc.resolve_issue, "U6", iss["issue_id"], "清理")
        # 物业可关异常，但不能复核租户争议
        self.svc.resolve_issue("U7", iss["issue_id"], "无主物品按规处置")
        # 租户管理员不能执行停机迁移
        self.expect_error("forbidden", self.svc.begin_outage, "U4", "F4")
        # 物业可以停机
        self.svc.begin_outage("U7", "F4")


class TestIssuesAndTemperature(FridgeTestBase):
    def test_unlabeled_flow_and_photo(self):
        r = self.svc.report_unlabeled("U6", "F1", "F1-S4-02")
        self.assertTrue(os.path.exists(os.path.join(app.MEDIA_DIR,
                        os.path.basename(r["photo"]))))
        issues = self.svc.issues(kind="unlabeled")
        self.assertEqual(issues[0]["status"], "open")
        self.svc.resolve_issue("U7", r["issue_id"], "公告 48h 无人认领后处置")
        self.assertEqual(self.svc.store.item(r["item_id"])["state"], "closed")

    def test_leak_and_wrong_cell(self):
        self.svc.report_leak("U6", "F2", "F2-S2-03", "酱料瓶渗漏")
        iid, _ = self.place("U3", "F1-S1-01")
        self.svc.report_wrong_cell("U8", iid, "F1-S4-02")
        self.assertEqual(len(self.svc.issues(kind="leak")), 1)
        self.assertEqual(len(self.svc.issues(kind="wrong_cell")), 1)

    def test_temperature_alarm(self):
        self.svc.temp_sensor.set_fault("F2", True)
        self.svc.poll_temperature()
        row = self.store.q1("SELECT * FROM temps WHERE fridge_id='F2' ORDER BY id DESC LIMIT 1")
        self.assertEqual(row["alarm"], 1)
        self.assertEqual(len(self.svc.issues(kind="temp")), 1)
        # 恢复后持续轮询不应重复开单
        self.svc.temp_sensor.set_fault("F2", False)
        self.svc.poll_temperature()
        self.assertEqual(len(self.svc.issues(kind="temp", status="open")), 1)

    def test_hygiene_low_score_opens_issue(self):
        self.svc.hygiene_check("U8", "F1", 65, "明显异味")
        self.assertEqual(len(self.svc.issues(kind="hygiene")), 1)
        self.svc.hygiene_check("U6", "F1", 95, "整洁")
        self.assertEqual(len(self.svc.issues(kind="hygiene")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
