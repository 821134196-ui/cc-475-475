# -*- coding: utf-8 -*-
"""示例主数据与样例场景。人员与物品均为示例数据。"""
from datetime import timedelta

from app import (Clock, Service, Store, COLOR_MINT, iso)

FRIDGES = [
    # id, 名称, 楼层, 层数, 每层格位数, 宽度比例（用于格位图按层架比例绘制）
    ("F1", "8F · 茶水间冰箱 A", "8F",  4, 4, 1.00),
    ("F2", "8F · 茶水间冰箱 B", "8F",  3, 3, 0.78),
    ("F3", "12F · 共享冰箱",    "12F", 4, 3, 0.88),
    ("F4", "15F · 行政冰箱",    "15F", 3, 4, 0.92),
]

DEPARTMENTS = [
    ("D1", "市场部", "MKT", COLOR_MINT),
    ("D2", "研发部", "R&D", "#5B8DEF"),
    ("D3", "财务部", "FIN", "#E0A13C"),
    ("D4", "行政部", "ADM", "#8B9AA8"),
]

USERS = [
    # id, 姓名, 部门, 角色
    ("U1", "林晓", "D1", "employee"),       # 市场部员工
    ("U2", "陈默", "D2", "employee"),       # 研发部员工
    ("U3", "苏晴", "D3", "employee"),       # 财务部员工
    ("U4", "周敏", "D1", "tenant_admin"),   # 市场部租户管理员（确认争议）
    ("U5", "何芳", "D2", "tenant_admin"),   # 研发部租户管理员
    ("U6", "吴桂兰", "D4", "cleaner"),      # 保洁（只能执行清理）
    ("U8", "高洁", "D4", "cleaner"),
    ("U7", "郑凯", "D4", "property"),       # 物业（停机迁移）
]


def seed_master(store):
    for fid, name, floor, shelves, cols, ratio in FRIDGES:
        store.execute(
            "INSERT OR IGNORE INTO fridges(id,name,floor,shelf_count,cols,width_ratio) "
            "VALUES(?,?,?,?,?,?)", (fid, name, floor, shelves, cols, ratio))
    for did, name, short, color in DEPARTMENTS:
        store.execute("INSERT OR IGNORE INTO departments(id,name,short,color) VALUES(?,?,?,?)",
                      (did, name, short, color))
    for uid, name, did, role in USERS:
        store.execute("INSERT OR IGNORE INTO users(id,name,dept_id,role) VALUES(?,?,?,?)",
                      (uid, name, did, role))


def _place(svc, clock, uid, cell, hours_ago, tag=""):
    """在 now-hours_ago 时刻完成一次贴签存放，返回 item_id。"""
    lab = svc.issue_label(uid)["code"]
    clock.freeze(svc.clock.now() - timedelta(hours=hours_ago))
    tok = svc.start_store(uid, lab, cell)["token"]
    res = svc.confirm_store(uid, tok)
    clock.reset()
    return res["item_id"], lab


def run(store, svc):
    """写入样例场景；已存在数据时跳过。"""
    if store.q1("SELECT COUNT(*) c FROM users")["c"] > 0:
        return False
    clock = svc.clock
    seed_master(store)

    # 1) 各冰箱的在存物品（未到期）
    _place(svc, clock, "U1", "F1-S2-01", 6)
    _place(svc, clock, "U2", "F1-S2-03", 20)
    _place(svc, clock, "U3", "F1-S3-02", 30)
    _place(svc, clock, "U1", "F2-S1-01", 10)
    _place(svc, clock, "U3", "F2-S2-02", 40)      # 临近到期
    _place(svc, clock, "U2", "F4-S1-02", 5)

    # 2) 到期队列：两件已过保留期限
    due1, _ = _place(svc, clock, "U2", "F1-S1-01", 50)
    due2, _ = _place(svc, clock, "U3", "F1-S1-02", 53)

    # 3) 已清理进临时保留区：一件已提申诉（等待租户管理员复核），一件未申诉
    ap_item, _ = _place(svc, clock, "U1", "F1-S3-04", 56)
    cl_item, _ = _place(svc, clock, "U3", "F2-S3-01", 60)
    clock.freeze(clock.now() - timedelta(hours=4))
    svc.scan_expiry()
    svc.clean_items("U6", [ap_item, cl_item], note="晚间例行到期清理")
    clock.reset()
    clock.freeze(clock.now() - timedelta(hours=2))
    svc.appeal("U1", ap_item, "周五出差未及时取走，标签日期未到，申请认领返还")
    clock.reset()

    # 4) 无标签物品（保洁巡检拍照登记）
    svc.report_unlabeled("U6", "F1", "F1-S4-04",
                         "巡检发现无标签便当盒，无法确认归属部门")

    # 5) 液体泄漏
    svc.report_leak("U6", "F2", "F2-S2-03", "层架角落约 10cm 液体渗漏，已临时垫吸油纸")

    # 6) 占错格位（物品登记格位与实际不符）
    svc.report_wrong_cell("U8", due2, "F1-S4-02")

    # 7) F3 停机样例：先有两件未到期物品，物业登记停机（待执行批量迁移）
    m1, _ = _place(svc, clock, "U2", "F3-S1-01", 8)
    m2, _ = _place(svc, clock, "U1", "F3-S2-02", 26)
    _place(svc, clock, "U2", "F3-S4-03", 55)   # 已到期：迁移时应转清理而非迁移
    svc.begin_outage("U7", "F3", resume_minutes=150)

    # 8) 温度记录：24 小时逐时采样；F2 中段故障超限后恢复（产生温度超限异常）
    t0 = clock.now() - timedelta(hours=23)
    for i in range(24):
        clock.freeze(t0 + timedelta(hours=i))
        svc.temp_sensor.set_fault("F2", 9 <= i <= 14)
        svc.poll_temperature()
    svc.temp_sensor.set_fault("F2", False)
    clock.reset()

    # 9) 卫生检查：一次不达标生成异常，一次达标
    svc.hygiene_check("U8", "F1", 72, "层架有汤汁残渍，密封条轻微霉斑")
    svc.hygiene_check("U6", "F4", 96, "整洁无异味")

    # 10) 标签样例：一张未使用（供快速存放演示）、一张已作废（标签失效演示）
    svc.issue_label("U1")
    dead = svc.issue_label("U2")["code"]
    svc.revoke_label("U5", dead)
    return True
