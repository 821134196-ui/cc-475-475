"""系统测试：格位竞争、标签失效、迁移完整、期限边界、租户隔离。"""
import threading
import time

import pytest

from fridge.adapters import DoorSensorAdapter, TempSensorAdapter
from fridge.db import Database
from fridge.services import DAY, FridgeService, ServiceError


@pytest.fixture()
def world(tmp_path):
    """每个用例一套独立 SQLite + 文件目录，内置两个部门与四类角色。"""
    db = Database(str(tmp_path / "test.db"))
    svc = FridgeService(db, str(tmp_path / "files"),
                        DoorSensorAdapter(), TempSensorAdapter(),
                        appeal_seconds=3600, pending_ttl=60)
    now = time.time()
    dept = {}
    for name, short in [("研发部", "RD"), ("市场部", "MKT")]:
        dept[short] = db.conn.execute(
            "INSERT INTO departments(name, short) VALUES (?,?)", (name, short)).lastrowid
    users = {}
    for name, role, d in [("小研", "employee", "RD"), ("小市", "employee", "MKT"),
                          ("保洁王", "cleaner", None), ("研主管", "tenant_admin", "RD"),
                          ("市主管", "tenant_admin", "MKT"), ("物业钱", "property", None)]:
        users[name] = db.conn.execute(
            "INSERT INTO users(name, role, dept_id) VALUES (?,?,?)",
            (name, role, dept.get(d))).lastrowid
    fridges = {}
    for code, rows, cols in [("FA", 2, 2), ("FB", 2, 2), ("FC", 1, 1)]:
        fid = db.conn.execute(
            "INSERT INTO fridges(code, floor, name, rows, cols) VALUES (?,?,?,?,?)",
            (code, "12F", code, rows, cols)).lastrowid
        fridges[code] = fid
        for r in range(1, rows + 1):
            for c in range(1, cols + 1):
                db.conn.execute(
                    "INSERT INTO slots(fridge_id, row, col, label) VALUES (?,?,?,?)",
                    (fid, r, c, f"{r}-{c}"))
    return svc, db, users, fridges, now


def free_slot(db, fridge_id):
    return db.conn.execute(
        """SELECT id FROM slots WHERE fridge_id=? AND NOT EXISTS (
             SELECT 1 FROM items i WHERE i.slot_id=slots.id
               AND i.status IN ('pending_store','stored','expired'))
           ORDER BY row, col LIMIT 1""", (fridge_id,)).fetchone()["id"]


def store_one(svc, db, users, fridges, owner, keep_days=3, fridge="FA", now=None):
    """完整走一遍三步存放，返回 item。"""
    label = svc.create_label(users[owner], "测试物品", keep_days, now)
    item = svc.store_item(users[owner], label["code"], free_slot(db, fridges[fridge]), now)
    return svc.confirm_store(users[owner], item["id"], now)


# ---------------------------------------------------------------- 格位竞争
def test_slot_contention_only_first_wins(world):
    svc, db, users, fridges, now = world
    slot_id = free_slot(db, fridges["FA"])
    label_a = svc.create_label(users["小研"], "牛奶", 3, now)
    label_b = svc.create_label(users["小研"], "果汁", 3, now)
    barrier = threading.Barrier(2)
    results = {}

    def attempt(idx, code):
        barrier.wait()
        try:
            svc.store_item(users["小研"], code, slot_id, now)
            results[idx] = "ok"
        except ServiceError as e:
            results[idx] = e.code

    t1 = threading.Thread(target=attempt, args=(1, label_a["code"]))
    t2 = threading.Thread(target=attempt, args=(2, label_b["code"]))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == ["ok", "slot_occupied"]
    # 格位上只有一件有效物品
    cnt = db.conn.execute(
        "SELECT COUNT(*) c FROM items WHERE slot_id=? AND status IN ('pending_store','stored')",
        (slot_id,)).fetchone()["c"]
    assert cnt == 1


# ---------------------------------------------------------------- 标签失效
def test_voided_label_rejected(world):
    svc, db, users, fridges, now = world
    label = svc.create_label(users["小研"], "酸奶", 3, now)
    svc.void_label(users["小研"], label["code"], now)
    with pytest.raises(ServiceError) as e:
        svc.store_item(users["小研"], label["code"], free_slot(db, fridges["FA"]), now)
    assert e.value.code == "label_invalid"


def test_duplicate_scan_rejected_and_logged(world):
    svc, db, users, fridges, now = world
    label = svc.create_label(users["小研"], "酸奶", 3, now)
    svc.store_item(users["小研"], label["code"], free_slot(db, fridges["FA"]), now)
    with pytest.raises(ServiceError) as e:
        svc.store_item(users["小研"], label["code"], free_slot(db, fridges["FA"]), now)
    assert e.value.code == "duplicate_scan"
    exc = db.conn.execute(
        "SELECT * FROM exceptions WHERE type='duplicate_scan'").fetchone()
    assert exc is not None


def test_door_open_blocks_confirm(world):
    svc, db, users, fridges, now = world
    label = svc.create_label(users["小研"], "酸奶", 3, now)
    item = svc.store_item(users["小研"], label["code"], free_slot(db, fridges["FA"]), now)
    svc.door.set(fridges["FA"], True)   # 模拟门未关
    with pytest.raises(ServiceError) as e:
        svc.confirm_store(users["小研"], item["id"], now)
    assert e.value.code == "door_open"
    svc.door.set(fridges["FA"], False)
    assert svc.confirm_store(users["小研"], item["id"], now)["status"] == "stored"


# ---------------------------------------------------------------- 迁移完整
def test_migration_moves_all_unexpired_with_labels(world):
    svc, db, users, fridges, now = world
    i1 = store_one(svc, db, users, fridges, "小研", fridge="FA", now=now)
    i2 = store_one(svc, db, users, fridges, "小市", fridge="FA", now=now)
    # 一件已到期物品不应迁移
    i3 = store_one(svc, db, users, fridges, "小研", keep_days=1, fridge="FA", now=now - 2 * DAY)
    svc.expire_sweep(now)
    assert svc.get_item(i3["id"])["status"] == "expired"

    sd = svc.start_shutdown(users["物业钱"], fridges["FA"], "检修", now)
    mig = svc.migrate(users["物业钱"], sd["id"], fridges["FB"], now)

    assert mig["moved"] == 2 and mig["missing"] == 0
    for old in (i1, i2):
        moved = svc.get_item(old["id"])
        assert moved["fridge_id"] == fridges["FB"]
        assert moved["label_code"] == old["label_code"]   # 保留原标签
        assert moved["status"] == "stored"
    assert svc.get_item(i3["id"])["fridge_id"] == fridges["FA"]  # 到期物品留在原地


def test_migration_missing_when_target_full(world):
    svc, db, users, fridges, now = world
    store_one(svc, db, users, fridges, "小研", fridge="FA", now=now)
    store_one(svc, db, users, fridges, "小市", fridge="FA", now=now)
    sd = svc.start_shutdown(users["物业钱"], fridges["FA"], "检修", now)
    mig = svc.migrate(users["物业钱"], sd["id"], fridges["FC"], now)  # FC 只有 1 格
    assert mig["moved"] == 1 and mig["missing"] == 1
    exc = svc.list_exceptions("open")
    assert any(e["type"] == "migration_missing" for e in exc)


# ---------------------------------------------------------------- 期限边界
def test_expiry_boundary(world):
    svc, db, users, fridges, now = world
    on_time = store_one(svc, db, users, fridges, "小研", keep_days=1, fridge="FA", now=now)
    later = store_one(svc, db, users, fridges, "小市", keep_days=1, fridge="FA", now=now)
    boundary = now + DAY
    db.conn.execute("UPDATE items SET expires_at=? WHERE id=?", (boundary, on_time["id"]))
    db.conn.execute("UPDATE items SET expires_at=? WHERE id=?", (boundary + 1, later["id"]))
    assert svc.expire_sweep(boundary) == 1          # expires_at <= now 即到期
    assert svc.get_item(on_time["id"])["status"] == "expired"
    assert svc.get_item(later["id"])["status"] == "stored"


def test_appeal_and_dispose_boundary(world):
    svc, db, users, fridges, now = world
    item = store_one(svc, db, users, fridges, "小研", keep_days=1, fridge="FA", now=now)
    svc.expire_sweep(now + DAY)
    batch = svc.create_batch(users["保洁王"], fridges["FA"], now + DAY)
    collected = svc.collect_item(users["保洁王"], item["id"], batch["id"], None, now + DAY)
    deadline = collected["retention_until"]
    assert deadline == now + DAY + 3600

    # 申诉窗口：now < retention_until 受理，到期瞬间关闭
    with pytest.raises(ServiceError) as e:
        svc.create_appeal(users["小研"], item["id"], "还要吃", deadline)
    assert e.value.code == "appeal_window_closed"

    appeal = svc.create_appeal(users["小研"], item["id"], "还要吃", deadline - 1)
    # 未决申诉阻止处置，即使申诉期已过
    with pytest.raises(ServiceError) as e:
        svc.dispose_item(users["保洁王"], item["id"], deadline)
    assert e.value.code == "has_open_appeal"
    svc.resolve_appeal(users["研主管"], appeal["id"], approve=False, now=deadline)
    # 申诉期结束前一秒不可处置，到期可处置
    with pytest.raises(ServiceError) as e:
        svc.dispose_item(users["保洁王"], item["id"], deadline - 1)
    assert e.value.code == "retention_not_over"
    assert svc.dispose_item(users["保洁王"], item["id"], deadline)["status"] == "disposed"


def test_approved_appeal_returns_item(world):
    svc, db, users, fridges, now = world
    item = store_one(svc, db, users, fridges, "小研", keep_days=1, fridge="FA", now=now)
    svc.expire_sweep(now + DAY)
    batch = svc.create_batch(users["保洁王"], fridges["FA"], now + DAY)
    svc.collect_item(users["保洁王"], item["id"], batch["id"], None, now + DAY)
    appeal = svc.create_appeal(users["小研"], item["id"], "客户样品", now + DAY + 10)
    svc.resolve_appeal(users["研主管"], appeal["id"], approve=True, now=now + DAY + 20)
    assert svc.get_item(item["id"])["status"] == "claimed_back"


# ---------------------------------------------------------------- 租户隔离
def test_tenant_admin_cannot_resolve_other_dept(world):
    svc, db, users, fridges, now = world
    item = store_one(svc, db, users, fridges, "小研", keep_days=1, fridge="FA", now=now)
    svc.expire_sweep(now + DAY)
    batch = svc.create_batch(users["保洁王"], fridges["FA"], now + DAY)
    svc.collect_item(users["保洁王"], item["id"], batch["id"], None, now + DAY)
    appeal = svc.create_appeal(users["小研"], item["id"], "还要吃", now + DAY + 10)
    with pytest.raises(ServiceError) as e:
        svc.resolve_appeal(users["市主管"], appeal["id"], approve=True, now=now + DAY + 20)
    assert e.value.code == "forbidden"
    # 本部门管理员可以复核
    svc.resolve_appeal(users["研主管"], appeal["id"], approve=True, now=now + DAY + 30)


def test_dept_items_and_appeals_scoped(world):
    svc, db, users, fridges, now = world
    store_one(svc, db, users, fridges, "小研", fridge="FA", now=now)
    store_one(svc, db, users, fridges, "小市", fridge="FA", now=now)
    rd_items = svc.dept_items(db.conn.execute(
        "SELECT id FROM departments WHERE short='RD'").fetchone()["id"])
    assert len(rd_items) == 1 and all(i["dept_short"] == "RD" for i in rd_items)
    # 员工申诉列表只含本部门
    rd_appeals = svc.list_appeals(users["小研"])
    assert all(a["dept_short"] == "RD" for a in rd_appeals)
    # 员工不能用其他部门的标签
    label = svc.create_label(users["小市"], "果汁", 3, now)
    with pytest.raises(ServiceError) as e:
        svc.store_item(users["小研"], label["code"], free_slot(db, fridges["FA"]), now)
    assert e.value.code == "forbidden"


def test_role_permissions(world):
    svc, db, users, fridges, now = world
    with pytest.raises(ServiceError):      # 保洁不能停机
        svc.start_shutdown(users["保洁王"], fridges["FA"], "x", now)
    with pytest.raises(ServiceError):      # 员工不能清理
        svc.create_batch(users["小研"], fridges["FA"], now)
    with pytest.raises(ServiceError):      # 员工不能复核申诉
        svc.resolve_appeal(users["小研"], 1, True, now)
