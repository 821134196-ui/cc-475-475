# -*- coding: utf-8 -*-
"""
写字楼公共冰箱格位与过期清理系统
- SQLite 事件溯源存储（物品事件 + 温度/异常/检查）
- 本地目录存放标签 SVG 与清理照片 SVG
- 进程内定时任务：到期入队、申诉期满处置、温度轮询、门磁模拟
- 模拟适配器：门磁 / 温度传感器
仅依赖 Python 标准库。
"""
import json
import os
import sqlite3
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FRIDGE_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "fridge.db")
MEDIA_DIR = os.path.join(DATA_DIR, "media")

DEFAULT_RETENTION_HOURS = 48          # 默认保留期限
APPEAL_WINDOW_HOURS = 24              # 临时保留区申诉期
DOOR_CANCEL_SECONDS = 30              # 关门确认时限（超出本次存放失效，需重新贴签）
TEMP_LIMITS = (0.0, 8.0)              # 冷藏温度阈值 ℃

# 视觉规范：办公白 / 薄荷绿 / 深灰
COLOR_BG = "#F7F9F8"
COLOR_MINT = "#3FBF9B"
COLOR_MINT_SOFT = "#E3F5EF"
COLOR_DARK = "#2E3A38"
COLOR_GRAY = "#6B7A77"
COLOR_LINE = "#DDE6E3"
COLOR_WARN = "#E0A13C"
COLOR_BAD = "#D96A55"


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------
class Clock:
    """可注入/可快进的时钟，便于边界测试。"""

    def __init__(self, now=None):
        self._offset = 0.0
        if now is not None:
            self._offset = now.timestamp() - time.time()

    def now(self):
        return datetime.fromtimestamp(time.time() + self._offset)

    def advance(self, seconds):
        self._offset += seconds

    def freeze(self, dt):
        self._offset = dt.timestamp() - time.time()

    def reset(self):
        self._offset = 0.0


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# 模拟适配器
# --------------------------------------------------------------------------
class DoorSensorAdapter:
    """模拟门磁：open_door 后读到 OPEN，close_door 后读到 CLOSED。"""

    def __init__(self):
        self._states = {}

    def open_door(self, fridge_id):
        self._states[fridge_id] = "OPEN"

    def close_door(self, fridge_id):
        self._states[fridge_id] = "CLOSED"

    def read(self, fridge_id):
        return self._states.get(fridge_id, "CLOSED")


class TempSensorAdapter:
    """模拟温度传感器：正常时在区间内小幅波动；fault 时持续超限。"""

    def __init__(self, clock):
        self.clock = clock
        self._fault = {}
        self._tick = 0

    def set_fault(self, fridge_id, fault=True):
        self._fault[fridge_id] = fault

    def read(self, fridge_id):
        if self._fault.get(fridge_id):
            return round(10.6 + (self._tick % 3) * 0.4, 1)   # 超限 10.6~11.4
        self._tick += 1
        base = 4.2 + (self._tick % 4) * 0.35                  # 正常 4.2~5.25
        return round(base, 1)


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS fridges (
  id TEXT PRIMARY KEY, name TEXT, floor TEXT,
  shelf_count INTEGER, cols INTEGER,
  width_ratio REAL DEFAULT 1.0,
  status TEXT DEFAULT 'normal',           -- normal / outage
  outage_at TEXT, resume_at TEXT,
  door_state TEXT DEFAULT 'CLOSED'
);
CREATE TABLE IF NOT EXISTS departments (
  id TEXT PRIMARY KEY, name TEXT, short TEXT, color TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, name TEXT, dept_id TEXT,
  role TEXT                               -- employee / cleaner / tenant_admin / property
);
CREATE TABLE IF NOT EXISTS item_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT, ts TEXT, actor TEXT,
  type TEXT, cell TEXT, to_cell TEXT,
  payload TEXT
);
CREATE TABLE IF NOT EXISTS labels (
  code TEXT PRIMARY KEY, dept_id TEXT, user_id TEXT,
  created_at TEXT, used INTEGER DEFAULT 0, revoked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS temps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fridge_id TEXT, ts TEXT, value REAL, alarm INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, ts TEXT, fridge_id TEXT, cell TEXT,
  item_id TEXT, detail TEXT, photo TEXT,
  status TEXT DEFAULT 'open', handled_by TEXT, handled_at TEXT, resolution TEXT
);
CREATE TABLE IF NOT EXISTS cleaning_batches (
  id TEXT PRIMARY KEY, created_at TEXT, actor TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS hygiene_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, fridge_id TEXT, inspector TEXT,
  score INTEGER, finding TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_item ON item_events(item_id, seq);
CREATE INDEX IF NOT EXISTS idx_temps_fridge ON temps(fridge_id, id);
"""


def db_connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class Store:
    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.RLock()
        self.conn = db_connect()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 基础 ----
    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def execute(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def event(self, item_id, etype, actor, cell=None, to_cell=None, payload=None):
        self.execute(
            "INSERT INTO item_events(item_id,ts,actor,type,cell,to_cell,payload) VALUES(?,?,?,?,?,?,?)",
            (item_id, iso(self.clock.now()), actor, etype, cell, to_cell,
             json.dumps(payload or {}, ensure_ascii=False)),
        )

    # ---- 格位 ----
    def all_cells(self, fridge_id=None):
        rows = self.q("SELECT * FROM fridges" + (" WHERE id=?" if fridge_id else ""),
                      (fridge_id,) if fridge_id else ())
        out = []
        for f in rows:
            for s in range(1, f["shelf_count"] + 1):
                for c in range(1, f["cols"] + 1):
                    out.append({"fridge_id": f["id"], "fridge": f["name"], "floor": f["floor"],
                                "shelf": s, "col": c,
                                "code": f"{f['id']}-S{s}-{c:02d}"})
        return out

    def fridge_cells(self, fridge_id):
        return [x for x in self.all_cells(fridge_id)]

    # ---- 物品状态机（从事件投影）----
    def items_snapshot(self):
        """把事件流投影成当前物品状态。"""
        items = {}
        for e in self.q("SELECT * FROM item_events ORDER BY seq"):
            it = items.get(e["item_id"])
            if it is None:
                it = {"id": e["item_id"], "state": None, "cell": None,
                      "label": None, "dept_id": None, "stored_at": None,
                      "expire_at": None, "actor": None, "batch_id": None,
                      "removed_at": None, "appeal_deadline": None}
                items[e["item_id"]] = it
            p = json.loads(e["payload"] or "{}")
            t = e["type"]
            if t == "stored":
                it.update(state="stored", cell=e["cell"], actor=e["actor"],
                         stored_at=e["ts"], label=p.get("label"),
                         dept_id=p.get("dept_id"),
                         expire_at=p.get("expire_at"))
            elif t == "expired":
                it["state"] = "due"
            elif t == "relocated":
                it["cell"] = e["to_cell"]
                it["fridge_id"] = p.get("fridge_id")
            elif t == "undone":
                it["state"] = "undone"
                it["cell"] = None
            elif t == "removed":
                it.update(state="removed", cell=None, batch_id=p.get("batch_id"),
                         removed_at=e["ts"],
                         appeal_deadline=p.get("appeal_deadline"))
                it["removed_reason"] = p.get("reason", "expired")
            elif t == "appealed":
                it["state"] = "appealed"
            elif t in ("restored",):
                it.update(state="stored", cell=e["to_cell"],
                         restored_at=e["ts"])
            elif t in ("disposed", "claimed", "discarded_unlabeled"):
                it["state"] = "closed"
                it["closed_reason"] = t
            elif t == "migration_missing":
                it["state"] = "missing"
        return items

    def item(self, item_id):
        return self.items_snapshot().get(item_id)

    def cell_occupancy(self):
        """返回 {cell_code: item} —— 仅当前仍占格的物品（stored/due/appealed-restored）。"""
        occ = {}
        for it in self.items_snapshot().values():
            if it["cell"] and it["state"] in ("stored", "due"):
                occ[it["cell"]] = it
        return occ

    # ---- 标签 ----
    def create_label(self, user):
        code = "L" + uuid.uuid4().hex[:8].upper()
        self.execute("INSERT INTO labels(code,dept_id,user_id,created_at) VALUES(?,?,?,?)",
                     (code, user["dept_id"], user["id"], iso(self.clock.now())))
        return code

    def get_label(self, code):
        return self.q1("SELECT * FROM labels WHERE code=?", (code,))


# --------------------------------------------------------------------------
# 业务服务
# --------------------------------------------------------------------------
class BusinessError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class Service:
    def __init__(self, store, clock=None):
        self.clock = clock or Clock()
        self.store = store
        self.doors = DoorSensorAdapter()
        self.temp_sensor = TempSensorAdapter(self.clock)
        # 存放会话：关门确认前的暂存（item_id 尚未真正入库 stored）
        self.pending = {}   # token -> dict

    # ---------- 人员辅助 ----------
    def user(self, uid):
        row = self.store.q1("SELECT * FROM users WHERE id=?", (uid,))
        if not row:
            raise BusinessError("no_user", "人员不存在")
        return dict(row)

    def require_role(self, uid, roles):
        u = self.user(uid)
        if u["role"] not in roles:
            raise BusinessError("forbidden", f"{u['name']} 的角色 {u['role']} 无权执行该操作")
        return u

    # ---------- 标签 ----------
    def issue_label(self, uid):
        u = self.require_role(uid, ["employee", "tenant_admin"])
        code = self.store.create_label(u)
        dept = self.store.q1("SELECT * FROM departments WHERE id=?", (u["dept_id"],))
        write_label_svg(code, u, dict(dept) if dept else {})
        return {"code": code, "uid": u["id"], "url": f"/media/labels/{code}.svg"}

    def revoke_label(self, uid, code):
        u = self.require_role(uid, ["tenant_admin", "property"])
        lab = self.store.get_label(code)
        if not lab:
            raise BusinessError("bad_label", "标签不存在")
        self.store.execute("UPDATE labels SET revoked=1 WHERE code=?", (code,))

    # ---------- 存放：① 选空余格位（门开） ----------
    def start_store(self, uid, label_code, cell):
        u = self.require_role(uid, ["employee", "tenant_admin"])
        lab = self.store.get_label(label_code)
        if not lab:
            raise BusinessError("bad_label", "标签无效：查无此签")
        if lab["revoked"]:
            raise BusinessError("label_revoked", "标签已作废，请重新贴签")
        if lab["used"]:
            raise BusinessError("duplicate_scan", "重复扫码：该标签已使用，一件物品只允许一签")
        if lab["user_id"] != uid and u["role"] != "tenant_admin":
            raise BusinessError("tenant_isolation", "不能使用其他租户的标签")

        fridge_id = cell.split("-")[0]
        f = self.store.q1("SELECT * FROM fridges WHERE id=?", (fridge_id,))
        if not f:
            raise BusinessError("bad_cell", "格位不存在")
        if not any(c["code"] == cell for c in self.store.fridge_cells(fridge_id)):
            raise BusinessError("bad_cell", "格位不存在")
        if f["status"] == "outage":
            raise BusinessError("fridge_outage", "该冰箱已停机，不能存放")
        # 并发：一个格位只允许一件有效物品，仅首个成功
        occ = self.store.cell_occupancy()
        if cell in occ:
            other = occ[cell]
            raise BusinessError("cell_taken",
                                f"格位已被 {other['label']} 占用，请选择空余格位")
        for tok, p in self.pending.items():
            if p["cell"] == cell and not p.get("canceled"):
                raise BusinessError("cell_holding",
                                    "格位正被他人开门占用（待关门确认），请另选格位")

        self.doors.open_door(fridge_id)
        token = uuid.uuid4().hex[:12]
        item_id = "I" + uuid.uuid4().hex[:8].upper()
        self.pending[token] = {
            "item_id": item_id, "label": label_code, "cell": cell,
            "fridge_id": fridge_id, "uid": uid, "dept_id": lab["dept_id"],
            "started": self.clock.now(), "door_open": True,
        }
        return {"token": token, "item_id": item_id, "cell": cell,
                "door": "OPEN", "hint": "门已打开，请放入物品后关门确认"}

    # ---------- 存放：② 关门确认 ----------
    def confirm_store(self, uid, token):
        p = self.pending.get(token)
        if not p or p.get("canceled"):
            raise BusinessError("no_session", "存放会话不存在或已撤销，请重新贴签")
        if p["uid"] != uid:
            raise BusinessError("forbidden", "不能确认他人的存放会话")
        u = self.user(uid)
        # 再次检查格位（关门之前可能被迁移/停机占用）
        if p["cell"] in self.store.cell_occupancy():
            self.doors.close_door(p["fridge_id"])
            p["canceled"] = True
            raise BusinessError("cell_taken", "关门确认时格位已被占用，存放未完成")
        door = self.doors.read(p["fridge_id"])
        if door != "OPEN" or not p.get("door_open"):
            p["canceled"] = True
            raise BusinessError("door_not_open", "门磁未检测到开门动作，请重新贴签存放")
        elapsed = (self.clock.now() - p["started"]).total_seconds()
        if elapsed > DOOR_CANCEL_SECONDS:
            self.doors.close_door(p["fridge_id"])
            p["canceled"] = True
            raise BusinessError("store_timeout",
                                f"超过 {DOOR_CANCEL_SECONDS} 秒未关门确认，本次存放已失效")

        expire = self.clock.now() + timedelta(hours=DEFAULT_RETENTION_HOURS)
        self.store.event(p["item_id"], "stored", uid, cell=p["cell"], payload={
            "label": p["label"], "dept_id": p["dept_id"],
            "retention_hours": DEFAULT_RETENTION_HOURS,
            "expire_at": iso(expire),
        })
        self.store.execute("UPDATE labels SET used=1 WHERE code=?", (p["label"],))
        # 同冰箱仍有其他进行中存放会话时保持门磁 OPEN，最后一人关门
        others = [q for q in self.pending.values()
                  if not q.get("canceled") and q is not p
                  and q["fridge_id"] == p["fridge_id"]]
        if not others:
            self.doors.close_door(p["fridge_id"])
        p["canceled"] = True
        return {"item_id": p["item_id"], "cell": p["cell"],
                "stored_at": iso(self.clock.now()), "expire_at": iso(expire)}

    # ---------- 撤销存放（关门后短时间内可撤销并释放格位与标签） ----------
    def undo_store(self, uid, item_id):
        it = self.store.item(item_id)
        if not it or it["state"] not in ("stored", "due"):
            raise BusinessError("cannot_undo", "当前状态不可撤销")
        u = self.user(uid)
        if it["actor"] != uid and u["role"] != "property":
            raise BusinessError("forbidden", "只能撤销本人的存放")
        self.store.event(item_id, "undone", uid, cell=it["cell"],
                         payload={"label": it["label"]})
        self.store.execute("UPDATE labels SET used=0, revoked=1 WHERE code=?",
                           (it["label"],))
        return {"ok": True, "cell_freed": it["cell"]}

    # ---------- 到期 ----------
    def scan_expiry(self):
        """定时任务：到期物品进入待清理清单。"""
        due = []
        now = self.clock.now()
        for it in self.store.items_snapshot().values():
            if it["state"] == "stored" and it["expire_at"]:
                if parse_iso(it["expire_at"]) <= now:
                    self.store.event(it["id"], "expired", "scheduler",
                                     cell=it["cell"])
                    due.append(it["id"])
        return due

    def expiry_queue(self):
        self.scan_expiry()
        rows = []
        depts = {d["id"]: d for d in self.store.q("SELECT * FROM departments")}
        for it in self.store.items_snapshot().values():
            if it["state"] == "due":
                d = depts.get(it["dept_id"])
                rows.append({
                    "item_id": it["id"], "label": it["label"],
                    "dept": d["short"] if d else "?", "cell": it["cell"],
                    "stored_at": it["stored_at"], "expire_at": it["expire_at"],
                    "overdue_h": round(
                        (self.clock.now() - parse_iso(it["expire_at"])).total_seconds() / 3600, 1),
                })
        rows.sort(key=lambda r: r["expire_at"])
        return rows

    # ---------- 清理：保洁拍照 → 临时保留区 ----------
    def clean_items(self, uid, item_ids, note=""):
        u = self.require_role(uid, ["cleaner", "property"])
        if not item_ids:
            raise BusinessError("empty", "未选择物品")
        snap = self.store.items_snapshot()
        batch_id = "B" + uuid.uuid4().hex[:8].upper()
        self.store.execute("INSERT INTO cleaning_batches(id,created_at,actor,note) VALUES(?,?,?,?)",
                           (batch_id, iso(self.clock.now()), uid, note))
        moved = []
        appeal_deadline = self.clock.now() + timedelta(hours=APPEAL_WINDOW_HOURS)
        for iid in item_ids:
            it = snap.get(iid)
            if not it or it["state"] != "due":
                continue   # 只能清理待清理清单内物品
            photo = write_cleanup_photo(iid, it, self.clock.now())
            self.store.event(iid, "removed", uid, cell=it["cell"], payload={
                "batch_id": batch_id, "reason": "expired",
                "appeal_deadline": iso(appeal_deadline), "photo": photo,
            })
            self.store.execute(
                "INSERT INTO issues(kind,ts,fridge_id,cell,item_id,detail,photo) "
                "VALUES(?,?,?,?,?,?,?)",
                ("cleaned", iso(self.clock.now()), it["cell"].split("-")[0],
                 it["cell"], iid, f"到期清理入临时保留区，申诉截止 {iso(appeal_deadline)}",
                 photo))
            moved.append(iid)
        return {"batch_id": batch_id, "items": moved,
                "appeal_deadline": iso(appeal_deadline)}

    def batches(self):
        out = []
        for b in self.store.q("SELECT * FROM cleaning_batches ORDER BY rowid DESC"):
            evs = self.store.q(
                "SELECT * FROM item_events WHERE type='removed' AND json_extract(payload,'$.batch_id')=?",
                (b["id"],))
            out.append({"id": b["id"], "created_at": b["created_at"],
                        "actor": b["actor"], "note": b["note"], "count": len(evs)})
        return out

    # ---------- 申诉 ----------
    def appeal(self, uid, item_id, reason):
        u = self.require_role(uid, ["employee", "tenant_admin"])
        it = self.store.item(item_id)
        if not it or it["state"] != "removed":
            raise BusinessError("not_appealable", "该物品不在可申诉状态")
        lab = self.store.get_label(it["label"])
        if lab and lab["dept_id"] != u["dept_id"]:
            raise BusinessError("tenant_isolation", "只能申诉本部门物品")
        self.store.event(item_id, "appealed", uid, payload={"reason": reason})
        return {"ok": True}

    def appeals_open(self):
        rows = []
        depts = {d["id"]: d for d in self.store.q("SELECT * FROM departments")}
        for it in self.store.items_snapshot().values():
            if it["state"] in ("removed", "appealed"):
                photo = batch_id = None
                ev = self.store.q1(
                    "SELECT payload FROM item_events WHERE item_id=? AND type='removed' "
                    "ORDER BY seq DESC LIMIT 1", (it["id"],))
                if ev:
                    pl = json.loads(ev["payload"])
                    photo = pl.get("photo")
                    batch_id = pl.get("batch_id")
                reason = None
                ev2 = self.store.q1(
                    "SELECT payload FROM item_events WHERE item_id=? AND type='appealed' "
                    "ORDER BY seq DESC LIMIT 1", (it["id"],))
                if ev2:
                    reason = json.loads(ev2["payload"]).get("reason")
                d = depts.get(it["dept_id"])
                rows.append({
                    "item_id": it["id"], "label": it["label"],
                    "dept": d["short"] if d else "?", "state": it["state"],
                    "batch_id": batch_id,
                    "appeal_deadline": it["appeal_deadline"],
                    "remaining_h": round((parse_iso(it["appeal_deadline"]) -
                                          self.clock.now()).total_seconds() / 3600, 1)
                    if it["appeal_deadline"] else None,
                    "photo": photo, "reason": reason,
                    "removed_at": it["removed_at"],
                })
        return sorted(rows, key=lambda r: r["removed_at"], reverse=True)

    def review_appeal(self, uid, item_id, verdict, cell=None):
        """租户管理员复核：uphold 维持处置 / return 认领返还（指定空余格位放回）。"""
        self.require_role(uid, ["tenant_admin"])
        it = self.store.item(item_id)
        if not it or it["state"] not in ("removed", "appealed"):
            raise BusinessError("bad_state", "当前状态不可复核")
        u = self.user(uid)
        if it["dept_id"] != u["dept_id"]:
            raise BusinessError("tenant_isolation", "租户管理员只能复核本部门争议")
        if verdict == "uphold":
            self.store.event(item_id, "disposed", uid,
                             payload={"note": "申诉被驳回，维持处置"})
            return {"ok": True, "verdict": "uphold"}
        if verdict == "return":
            target = cell or self.find_free_cell()
            if not target:
                raise BusinessError("no_cell", "楼内无空余格位可返还")
            if target in self.store.cell_occupancy():
                raise BusinessError("cell_taken", "目标格位已占用")
            self.store.event(item_id, "restored", uid, to_cell=target,
                             payload={"note": "申诉成立，物品放回"})
            return {"ok": True, "verdict": "return", "cell": target}
        raise BusinessError("bad_verdict", "复核结论无效")

    def scan_appeal_deadline(self):
        """定时任务：申诉期满未获支持 → 处置。"""
        done = []
        now = self.clock.now()
        for it in self.store.items_snapshot().values():
            if it["state"] == "removed" and it["appeal_deadline"]:
                if parse_iso(it["appeal_deadline"]) <= now:
                    self.store.event(it["id"], "disposed", "scheduler",
                                     payload={"note": "申诉期满，自动处置"})
                    done.append(it["id"])
        return done

    def claim(self, uid, item_id):
        """临时保留区内直接认领（申诉期内租户本人）。"""
        u = self.user(uid)
        it = self.store.item(item_id)
        if not it or it["state"] not in ("removed", "appealed"):
            raise BusinessError("not_claimable", "物品当前不可认领")
        lab = self.store.get_label(it["label"])
        if lab and lab["dept_id"] != u["dept_id"] and u["role"] != "tenant_admin":
            raise BusinessError("tenant_isolation", "只能认领本部门物品")
        self.store.event(item_id, "claimed", uid)
        return {"ok": True}

    # ---------- 停机与迁移（物业） ----------
    def begin_outage(self, uid, fridge_id, resume_minutes=120):
        self.require_role(uid, ["property"])
        f = self.store.q1("SELECT * FROM fridges WHERE id=?", (fridge_id,))
        if not f:
            raise BusinessError("no_fridge", "冰箱不存在")
        if f["status"] == "outage":
            raise BusinessError("already_outage", "该冰箱已在停机中")
        resume = self.clock.now() + timedelta(minutes=resume_minutes)
        self.store.execute("UPDATE fridges SET status='outage',outage_at=?,resume_at=? WHERE id=?",
                           (iso(self.clock.now()), iso(resume), fridge_id))
        return {"ok": True, "resume_at": iso(resume)}

    def find_free_cell(self, exclude_fridge=None):
        occ = self.store.cell_occupancy()
        held = {p["cell"] for p in self.pending.values() if not p.get("canceled")}
        for c in self.store.all_cells():
            if c["fridge_id"] == exclude_fridge:
                continue
            f = self.store.q1("SELECT status FROM fridges WHERE id=?", (c["fridge_id"],))
            if f["status"] == "outage":
                continue
            if c["code"] not in occ and c["code"] not in held:
                return c["code"]
        return None

    def migration_targets(self, fridge_id):
        """预览停机迁移：未到期物品清单 + 自动分配的目标格位（保留原标签）。"""
        occ = self.store.cell_occupancy()
        used = set(occ) | {p["cell"] for p in self.pending.values() if not p.get("canceled")}
        targets, items = [], []
        for code in [c["code"] for c in self.store.fridge_cells(fridge_id)]:
            it = occ.get(code)
            if not it:
                continue
            if it["state"] == "due":
                items.append({"item_id": it["id"], "label": it["label"], "from": code,
                              "to": None, "note": "已到期，转清理队列不迁移"})
                continue
            tgt = None
            for c in self.store.all_cells():
                if c["fridge_id"] == fridge_id:
                    continue
                f = self.store.q1("SELECT status FROM fridges WHERE id=?", (c["fridge_id"],))
                if f["status"] == "outage":
                    continue
                if c["code"] not in used:
                    tgt = c["code"]
                    used.add(tgt)
                    break
            items.append({"item_id": it["id"], "label": it["label"], "from": code,
                          "to": tgt, "note": "迁移并保留原标签" if tgt else "无空余格位"})
        return items

    def migrate_outage(self, uid, fridge_id, missing_ids=None):
        """物业执行批量迁移；missing_ids 为现场清点未找到的物品（迁移缺件）。"""
        self.require_role(uid, ["property"])
        missing_ids = set(missing_ids or [])
        f = self.store.q1("SELECT * FROM fridges WHERE id=?", (fridge_id,))
        if not f or f["status"] != "outage":
            raise BusinessError("not_outage", "冰箱不在停机状态，无法迁移")
        plan = self.migration_targets(fridge_id)
        moved, missing, skipped = [], [], []
        for row in plan:
            it = self.store.item(row["item_id"])
            if row["item_id"] in missing_ids or not it or it["cell"] != row["from"]:
                # 迁移缺件：现场清点与登记清单不符
                self.store.execute(
                    "INSERT INTO issues(kind,ts,fridge_id,cell,item_id,detail,status) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("migration_missing", iso(self.clock.now()), fridge_id, row["from"],
                     row["item_id"], "停机迁移清点时未找到该物品", "open"))
                self.store.event(row["item_id"], "migration_missing", uid,
                                 cell=row["from"])
                missing.append(row["item_id"])
                continue
            if row["to"] is None:
                if it["state"] == "due":
                    skipped.append(row["item_id"])
                else:
                    self.store.execute(
                        "INSERT INTO issues(kind,ts,fridge_id,cell,item_id,detail,status) "
                        "VALUES(?,?,?,?,?,?,?)",
                        ("migration_missing", iso(self.clock.now()), fridge_id, row["from"],
                         row["item_id"], "无可用目标格位，物品暂留停机冰箱", "open"))
                    missing.append(row["item_id"])
                continue
            self.store.event(row["item_id"], "relocated", uid,
                             cell=row["from"], to_cell=row["to"],
                             payload={"fridge_id": fridge_id, "label_kept": True})
            moved.append(row["item_id"])
        return {"moved": moved, "missing": missing, "skipped_expired": skipped}

    def end_outage(self, uid, fridge_id):
        self.require_role(uid, ["property"])
        self.store.execute(
            "UPDATE fridges SET status='normal',outage_at=NULL,resume_at=NULL WHERE id=?",
            (fridge_id,))
        return {"ok": True}

    # ---------- 温度 ----------
    def poll_temperature(self):
        for f in self.store.q("SELECT id FROM fridges"):
            val = self.temp_sensor.read(f["id"])
            lo, hi = TEMP_LIMITS
            alarm = not (lo <= val <= hi)
            self.store.execute(
                "INSERT INTO temps(fridge_id,ts,value,alarm) VALUES(?,?,?,?)",
                (f["id"], iso(self.clock.now()), val, 1 if alarm else 0))
            if alarm:
                last = self.store.q1(
                    "SELECT id FROM issues WHERE kind='temp' AND fridge_id=? AND status='open' "
                    "ORDER BY id DESC LIMIT 1", (f["id"],))
                if not last:
                    self.store.execute(
                        "INSERT INTO issues(kind,ts,fridge_id,detail,status) VALUES(?,?,?,?,?)",
                        ("temp", iso(self.clock.now()), f["id"],
                         f"温度 {val}℃ 超出 {lo}~{hi}℃ 区间", "open"))
        return True

    def temps(self, fridge_id, limit=48):
        return [dict(r) for r in self.store.q(
            "SELECT * FROM temps WHERE fridge_id=? ORDER BY id DESC LIMIT ?",
            (fridge_id, limit))][::-1]

    # ---------- 卫生 / 巡检异常 ----------
    def report_unlabeled(self, uid, fridge_id, cell, detail="巡检发现无标签物品"):
        u = self.require_role(uid, ["cleaner", "property", "tenant_admin"])
        iid = "U" + uuid.uuid4().hex[:8].upper()
        photo = write_issue_photo(iid, "无标签物品", self.clock.now())
        self.store.event(iid, "found_unlabeled", uid, cell=cell)
        cur = self.store.execute(
            "INSERT INTO issues(kind,ts,fridge_id,cell,item_id,detail,photo,status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("unlabeled", iso(self.clock.now()), fridge_id, cell, iid, detail, photo,
             "open")).lastrowid
        return {"issue_id": cur, "item_id": iid, "photo": photo}

    def report_wrong_cell(self, uid, item_id, actual_cell):
        u = self.require_role(uid, ["cleaner", "tenant_admin"])
        it = self.store.item(item_id)
        if not it:
            raise BusinessError("no_item", "物品不存在")
        self.store.execute(
            "INSERT INTO issues(kind,ts,fridge_id,cell,item_id,detail,status) VALUES(?,?,?,?,?,?,?)",
            ("wrong_cell", iso(self.clock.now()), actual_cell.split("-")[0], actual_cell,
             item_id, f"标签 {it['label']} 占用了非登记格位（登记 {it['cell']}）", "open"))
        return {"ok": True}

    def report_leak(self, uid, fridge_id, cell, detail):
        u = self.require_role(uid, ["cleaner", "property"])
        photo = write_issue_photo("LEAK" + uuid.uuid4().hex[:6], "液体泄漏",
                                  self.clock.now())
        cur = self.store.execute(
            "INSERT INTO issues(kind,ts,fridge_id,cell,detail,photo,status) VALUES(?,?,?,?,?,?,?)",
            ("leak", iso(self.clock.now()), fridge_id, cell, detail, photo, "open")).lastrowid
        return {"issue_id": cur}

    def resolve_issue(self, uid, issue_id, resolution):
        """保洁只能执行清理；异常处置需租户管理员/物业。"""
        u = self.user(uid)
        if u["role"] not in ("tenant_admin", "property"):
            raise BusinessError("forbidden", "保洁仅可执行清理，异常关闭需管理员/物业")
        self.store.execute(
            "UPDATE issues SET status='resolved',handled_by=?,handled_at=?,resolution=? WHERE id=?",
            (uid, iso(self.clock.now()), resolution, issue_id))
        iss = self.store.q1("SELECT * FROM issues WHERE id=?", (issue_id,))
        if iss and iss["kind"] == "unlabeled" and iss["item_id"]:
            self.store.event(iss["item_id"], "discarded_unlabeled", uid,
                             payload={"resolution": resolution})
        return {"ok": True}

    def hygiene_check(self, uid, fridge_id, score, finding):
        u = self.require_role(uid, ["cleaner", "property", "tenant_admin"])
        cur = self.store.execute(
            "INSERT INTO hygiene_checks(ts,fridge_id,inspector,score,finding) VALUES(?,?,?,?,?)",
            (iso(self.clock.now()), fridge_id, uid, score, finding)).lastrowid
        if score and score < 80:
            self.store.execute(
                "INSERT INTO issues(kind,ts,fridge_id,detail,status) VALUES(?,?,?,?,?)",
                ("hygiene", iso(self.clock.now()), fridge_id,
                 f"卫生评分 {score}：{finding}", "open"))
        return {"check_id": cur}

    def issues(self, kind=None, status=None):
        sql, args = "SELECT * FROM issues WHERE 1=1", []
        if kind:
            sql += " AND kind=?"; args.append(kind)
        if status:
            sql += " AND status=?"; args.append(status)
        sql += " ORDER BY id DESC LIMIT 100"
        out = []
        for r in self.store.q(sql, args):
            d = dict(r)
            d["photo_url"] = ("/media/" + os.path.basename(d["photo"])) if d["photo"] else None
            if d["photo"]:
                d["photo_url"] = "/media/" + os.path.relpath(d["photo"], MEDIA_DIR)
            out.append(d)
        return out

    # ---------- 查询汇总 ----------
    def map_data(self):
        fridges = [dict(f) for f in self.store.q("SELECT * FROM fridges ORDER BY id")]
        depts = {d["id"]: dict(d) for d in self.store.q("SELECT * FROM departments")}
        occ = self.store.cell_occupancy()
        held = {p["cell"]: p for p in self.pending.values() if not p.get("canceled")}
        out = []
        for f in fridges:
            cells = []
            for c in self.store.fridge_cells(f["id"]):
                it = occ.get(c["code"])
                cell = dict(c)
                if it:
                    d = depts.get(it["dept_id"], {})
                    cell.update(occupied=True, item_id=it["id"], label=it["label"],
                                dept_short=d.get("short", "?"), color=d.get("color", COLOR_MINT),
                                stored_at=it["stored_at"][5:10], expire_at=it["expire_at"][5:10],
                                state=it["state"])
                elif c["code"] in held:
                    cell.update(held=True)
                cells.append(cell)
            out.append({"fridge": f, "cells": cells})
        return out

    def users(self):
        return [dict(r) for r in self.store.q(
            "SELECT u.*, d.short dept_short FROM users u LEFT JOIN departments d ON u.dept_id=d.id "
            "ORDER BY u.role,u.id")]

    def labels(self):
        return [dict(r) for r in self.store.q("SELECT * FROM labels ORDER BY rowid DESC LIMIT 50")]

    def checks(self):
        return [dict(r) for r in self.store.q(
            "SELECT h.*, f.name fridge_name FROM hygiene_checks h "
            "JOIN fridges f ON h.fridge_id=f.id ORDER BY h.id DESC LIMIT 30")]


# --------------------------------------------------------------------------
# 本地媒体：标签 SVG / 清理照片 SVG / 异常照片 SVG
# --------------------------------------------------------------------------
def _svg_path(*parts):
    p = os.path.join(MEDIA_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def write_label_svg(code, user, dept):
    short = dept.get("short", "??")
    today = datetime.now().strftime("%m/%d")
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='240' height='120' viewBox='0 0 240 120'>
<rect width='240' height='120' rx='10' fill='white' stroke='{COLOR_MINT}' stroke-width='2'/>
<rect width='8' height='120' rx='4' fill='{COLOR_MINT}'/>
<text x='24' y='34' font-family='sans-serif' font-size='13' fill='{COLOR_GRAY}'>部门</text>
<text x='24' y='70' font-family='sans-serif' font-size='34' font-weight='700' fill='{COLOR_DARK}'>{short}</text>
<text x='120' y='34' font-family='sans-serif' font-size='13' fill='{COLOR_GRAY}'>日期 {today}</text>
<text x='120' y='70' font-family='monospace' font-size='15' fill='{COLOR_DARK}'>{code}</text>
<text x='24' y='102' font-family='sans-serif' font-size='11' fill='{COLOR_GRAY}'>公共冰箱 · 保留 48h · 过期自动清理</text>
</svg>"""
    path = _svg_path("labels", f"{code}.svg")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(svg)
    return path


def write_cleanup_photo(item_id, it, when):
    dept = (it.get("dept_id") or "??")[-2:]
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='320' height='220' viewBox='0 0 320 220'>
<rect width='320' height='220' fill='#EDF2F0'/>
<rect x='20' y='150' width='280' height='50' fill='#D9E2DF'/>
<rect x='70' y='70' width='180' height='90' rx='8' fill='{COLOR_MINT_SOFT}' stroke='{COLOR_MINT}' stroke-width='2'/>
<rect x='92' y='88' width='90' height='10' rx='3' fill='{COLOR_MINT}'/>
<rect x='92' y='106' width='130' height='8' rx='3' fill='#B9DCD1'/>
<rect x='92' y='122' width='70' height='8' rx='3' fill='#B9DCD1'/>
<text x='20' y='30' font-family='sans-serif' font-size='13' fill='{COLOR_DARK}'>清理拍照 · {iso(when)}</text>
<text x='20' y='50' font-family='monospace' font-size='12' fill='{COLOR_GRAY}'>{item_id} / {it.get('label')} / {it.get('cell')}</text>
<circle cx='282' cy='34' r='9' fill='{COLOR_BAD}'/>
</svg>"""
    path = _svg_path(f"clean_{item_id}_{when.strftime('%Y%m%d%H%M%S')}.svg")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(svg)
    return path


def write_issue_photo(item_id, title, when):
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='320' height='200' viewBox='0 0 320 200'>
<rect width='320' height='200' fill='#F3EEE9'/>
<rect x='20' y='136' width='280' height='44' fill='#E4DACF'/>
<ellipse cx='160' cy='130' rx='70' ry='16' fill='#C9B9A4'/>
<rect x='110' y='86' width='100' height='42' rx='6' fill='#E8D9C4' stroke='#B79F82'/>
<text x='20' y='32' font-family='sans-serif' font-size='14' fill='{COLOR_DARK}'>{title} · 取证照片</text>
<text x='20' y='52' font-family='monospace' font-size='11' fill='{COLOR_GRAY}'>{item_id} · {iso(when)}</text>
</svg>"""
    path = _svg_path(f"issue_{item_id}_{when.strftime('%Y%m%d%H%M%S')}.svg")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(svg)
    return path
