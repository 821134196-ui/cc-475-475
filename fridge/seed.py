"""示例数据：楼层冰箱、部门人员、在架/到期物品，以及无标签、停机、申诉样例。"""
from __future__ import annotations

import time

from .db import Database
from .services import DAY, FridgeService

DEPARTMENTS = [("研发部", "RD"), ("市场部", "MKT"), ("行政部", "ADM"),
               ("财务部", "FIN"), ("设计部", "DSN")]

USERS = [
    # name, role, dept_short
    ("陈一鸣", "employee", "RD"),
    ("林小满", "employee", "RD"),
    ("周芷若", "employee", "MKT"),
    ("吴启航", "employee", "ADM"),
    ("郑晚舟", "employee", "FIN"),
    ("何丹青", "employee", "DSN"),
    ("王建国", "cleaner", None),
    ("李桂香", "cleaner", None),
    ("赵明诚", "tenant_admin", "RD"),
    ("孙雅婷", "tenant_admin", "MKT"),
    ("高志远", "tenant_admin", "ADM"),
    ("孟书华", "tenant_admin", "FIN"),
    ("钟可欣", "tenant_admin", "DSN"),
    ("钱守业", "property", None),
]

FRIDGES = [
    # code, floor, name, rows, cols
    ("F12-A", "12F", "12层 茶水间冰箱 A", 4, 5),
    ("F12-B", "12F", "12层 茶水间冰箱 B", 3, 4),
    ("F15-A", "15F", "15层 休息区冰箱 A", 4, 5),
    ("F15-B", "15F", "15层 休息区冰箱 B（停机样例）", 3, 4),
]

SAMPLE_ITEMS = [
    # owner_idx, fridge_code, 名称, keep_days, 距今天数偏移（负=已存放天数）, 状态
    (0, "F12-A", "低脂牛奶", 5, -2, "stored"),
    (1, "F12-A", "鸡胸肉沙拉", 3, -1, "stored"),
    (2, "F12-A", "酸奶杯", 4, -4, "stored"),      # 今天到期边界
    (3, "F12-A", "自制便当", 2, -3, "stored"),     # 已过期 → 待清理
    (4, "F12-B", "鲜榨果汁", 3, -4, "stored"),     # 已过期 → 待清理
    (5, "F12-B", "三明治", 2, -1, "stored"),
    (0, "F15-A", "冷萃咖啡", 7, -2, "stored"),
    (2, "F15-A", "水果盒", 3, -1, "stored"),
    (4, "F15-B", "豆浆", 5, -1, "stored"),         # 停机冰箱上的未到期物品
    (5, "F15-B", "饭团", 2, -1, "stored"),         # 停机冰箱上的未到期物品
]


def seed(db: Database, svc: FridgeService) -> None:
    if db.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        return  # 已初始化
    now = time.time()

    dept_ids: dict[str, int] = {}
    for name, short in DEPARTMENTS:
        cur = db.conn.execute("INSERT INTO departments(name, short) VALUES (?,?)",
                              (name, short))
        dept_ids[short] = cur.lastrowid

    user_ids: list[int] = []
    for name, role, short in USERS:
        cur = db.conn.execute(
            "INSERT INTO users(name, role, dept_id) VALUES (?,?,?)",
            (name, role, dept_ids.get(short)))
        user_ids.append(cur.lastrowid)

    fridge_ids: dict[str, int] = {}
    for code, floor, name, rows, cols in FRIDGES:
        cur = db.conn.execute(
            "INSERT INTO fridges(code, floor, name, rows, cols) VALUES (?,?,?,?,?)",
            (code, floor, name, rows, cols))
        fid = cur.lastrowid
        fridge_ids[code] = fid
        row_names = "ABCDEFGH"
        for r in range(1, rows + 1):
            for c in range(1, cols + 1):
                db.conn.execute(
                    "INSERT INTO slots(fridge_id, row, col, label) VALUES (?,?,?,?)",
                    (fid, r, c, f"{row_names[r-1]}-{c}"))

    def free_slot(fridge_code: str):
        return db.conn.execute(
            """SELECT s.id FROM slots s WHERE s.fridge_id=? AND s.blocked=0
                 AND NOT EXISTS (SELECT 1 FROM items i WHERE i.slot_id=s.id
                                 AND i.status IN ('pending_store','stored','expired'))
               ORDER BY s.row, s.col LIMIT 1""",
            (fridge_ids[fridge_code],)).fetchone()["id"]

    # ---- 在架 / 到期物品（直接写入历史时间，绕过存放流程）
    seq = 0
    retention_item_id = None
    for owner_idx, fcode, name, keep_days, day_offset, status in SAMPLE_ITEMS:
        seq += 1
        owner_id = user_ids[owner_idx]
        dept_id = db.conn.execute(
            "SELECT dept_id FROM users WHERE id=?", (owner_id,)).fetchone()["dept_id"]
        short = db.conn.execute(
            "SELECT short FROM departments WHERE id=?", (dept_id,)).fetchone()["short"]
        code = f"LBL-{short}-{seq:04d}"
        stored_at = now + day_offset * DAY
        expires_at = stored_at + keep_days * DAY
        db.conn.execute(
            """INSERT INTO labels(code, dept_id, owner_id, item_name, keep_days,
                                  status, created_at)
               VALUES (?,?,?,?,?, 'used', ?)""",
            (code, dept_id, owner_id, name, keep_days, stored_at))
        slot_id = free_slot(fcode)
        item_status = "expired" if expires_at <= now else "stored"
        cur = db.conn.execute(
            """INSERT INTO items(label_code, dept_id, owner_id, name, fridge_id, slot_id,
                                 status, keep_days, created_at, stored_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (code, dept_id, owner_id, name, fridge_ids[fcode], slot_id,
             item_status, keep_days, stored_at, stored_at, expires_at))
        db.conn.execute(
            "INSERT INTO events(item_id, type, actor, detail, at) VALUES (?,?,?,?,?)",
            (cur.lastrowid, "stored", owner_id, "示例数据", stored_at))
        if name == "鲜榨果汁":
            retention_item_id = cur.lastrowid

    # ---- 申诉样例：一件到期物品已被保洁收入保留区，员工发起申诉待复核
    if retention_item_id:
        cur = db.conn.execute(
            "INSERT INTO batches(fridge_id, cleaner_id, status, created_at)"
            " VALUES (?,?, 'open', ?)", (fridge_ids["F12-B"], user_ids[6], now - 3600))
        batch_id = cur.lastrowid
        photo_path = svc._save_photo(retention_item_id, None, now - 3600)
        db.conn.execute(
            """UPDATE items SET status='retention', batch_id=?, photo_path=?,
                               retention_until=?, slot_id=NULL, fridge_id=NULL
               WHERE id=?""",
            (batch_id, photo_path, now + 20 * 3600, retention_item_id))  # 申诉期还剩 20h
        db.conn.execute(
            "INSERT INTO events(item_id, type, actor, detail, at) VALUES (?,?,?,?,?)",
            (retention_item_id, "collected", user_ids[6], f"批次#{batch_id}", now - 3600))
        db.conn.execute(
            """INSERT INTO appeals(item_id, user_id, dept_id, reason, status, created_at)
               VALUES (?,?,?,?, 'open', ?)""",
            (retention_item_id, user_ids[4], dept_ids["FIN"],
             "是客户样品，周五还要用，请保留", now - 1800))

    # ---- 停机样例：F15-B 故障停机，等待迁移
    db.conn.execute("UPDATE fridges SET status='shutdown' WHERE id=?",
                    (fridge_ids["F15-B"],))
    db.conn.execute(
        "INSERT INTO shutdowns(fridge_id, reason, status, started_at)"
        " VALUES (?,?, 'open', ?)",
        (fridge_ids["F15-B"], "压缩机异响，厂商上门检修", now - 7200))

    # ---- 无标签样例：保洁在 F12-A 发现无标签物品
    slot_a1 = db.conn.execute(
        "SELECT id FROM slots WHERE fridge_id=? AND label='A-1'",
        (fridge_ids["F12-A"],)).fetchone()["id"]
    db.conn.execute(
        """INSERT INTO exceptions(type, fridge_id, slot_id, note, status, created_by,
                                  created_at)
           VALUES ('unlabeled', ?, ?, 'A-1 格发现无标签饭盒一个，已拍照留证',
                   'open', ?, ?)""",
        (fridge_ids["F12-A"], slot_a1, user_ids[6], now - 5400))

    # ---- 温度历史（含一次超限）与卫生检查
    for i in range(12):
        at = now - (12 - i) * 600
        val = 4.0 + ((i % 5) - 2) * 0.6
        if i == 8:
            val = 9.4  # 一次超限
        svc.record_temp(fridge_ids["F12-A"], val, at)
    db.conn.execute(
        "INSERT INTO inspections(fridge_id, inspector_id, result, notes, at)"
        " VALUES (?,?,?,?,?)",
        (fridge_ids["F12-A"], user_ids[-1], "pass", "层架整洁，标签规范", now - DAY))

    db.conn.execute("COMMIT") if db.conn.in_transaction else None
