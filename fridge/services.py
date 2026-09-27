"""领域服务：存放、到期、清理、申诉、停机迁移、异常与巡检。

所有写操作在 IMMEDIATE 事务内执行；格位唯一性由部分唯一索引
`one_active_item_per_slot` 保证，并发存放仅首个成功。
"""
from __future__ import annotations

import base64
import os
import sqlite3
import time

from .adapters import DoorSensorAdapter, TempSensorAdapter
from .db import Database

# ---------------------------------------------------------------- 配置
APPEAL_SECONDS = int(os.environ.get("FRIDGE_APPEAL_SECONDS", str(48 * 3600)))  # 申诉期
PENDING_TTL = int(os.environ.get("FRIDGE_PENDING_TTL", "120"))                 # 待确认超时
DAY = 86400

ACTIVE_ITEM_STATUSES = ("pending_store", "stored", "expired")


class ServiceError(Exception):
    """业务错误：code 供前端映射文案，http 供接口层返回状态码。"""

    def __init__(self, code: str, message: str, http: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


def _row_to_dict(r):
    return dict(r) if r is not None else None


class FridgeService:
    def __init__(self, db: Database, data_dir: str,
                 door: DoorSensorAdapter | None = None,
                 temp: TempSensorAdapter | None = None,
                 appeal_seconds: int = APPEAL_SECONDS,
                 pending_ttl: int = PENDING_TTL):
        self.db = db
        self.data_dir = data_dir
        self.label_dir = os.path.join(data_dir, "labels")
        self.photo_dir = os.path.join(data_dir, "photos")
        os.makedirs(self.label_dir, exist_ok=True)
        os.makedirs(self.photo_dir, exist_ok=True)
        self.door = door or DoorSensorAdapter()
        self.temp = temp or TempSensorAdapter()
        self.appeal_seconds = appeal_seconds
        self.pending_ttl = pending_ttl

    # ------------------------------------------------------------ 工具
    def _now(self, now: float | None) -> float:
        return time.time() if now is None else now

    def _get(self, sql: str, args=(), err="not_found", msg="记录不存在"):
        row = self.db.conn.execute(sql, args).fetchone()
        if row is None:
            raise ServiceError(err, msg, 404)
        return row

    def _event(self, conn, item_id, type_, actor, detail="", at=None):
        conn.execute(
            "INSERT INTO events(item_id, type, actor, detail, at) VALUES (?,?,?,?,?)",
            (item_id, type_, actor, detail, self._now(at)))

    def _user(self, user_id: int):
        return self._get("SELECT * FROM users WHERE id=?", (user_id,), msg="用户不存在")

    # ------------------------------------------------------------ 标签
    def create_label(self, user_id: int, item_name: str, keep_days: int,
                     now: float | None = None) -> dict:
        """第 1 步：登记物品并打印标签（生成 SVG 到本地目录）。"""
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] != "employee":
            raise ServiceError("forbidden", "仅员工可登记物品标签", 403)
        if not item_name or not item_name.strip():
            raise ServiceError("name_required", "请填写物品名称")
        if not (1 <= int(keep_days) <= 30):
            raise ServiceError("keep_days_invalid", "保留期限需在 1-30 天之间")
        dept = self._get("SELECT * FROM departments WHERE id=?", (user["dept_id"],))
        with self.db.tx() as conn:
            seq = conn.execute("SELECT COUNT(*) c FROM labels").fetchone()["c"] + 1
            code = f"LBL-{dept['short']}-{seq:04d}"
            cur = conn.execute(
                """INSERT INTO labels(code, dept_id, owner_id, item_name, keep_days,
                                      status, created_at)
                   VALUES (?,?,?,?,?, 'active', ?)""",
                (code, dept["id"], user_id, item_name.strip(), int(keep_days), now))
            label_id = cur.lastrowid
        image_path = self._render_label_svg(code, dept["short"], now, item_name.strip())
        with self.db.tx() as conn:
            conn.execute("UPDATE labels SET image_path=? WHERE id=?", (image_path, label_id))
            self._event(conn, None, "label_created", user_id, f"{code} {item_name.strip()}", now)
        return self.get_label(code)

    def _render_label_svg(self, code: str, dept_short: str, at: float, name: str) -> str:
        date = time.strftime("%m-%d", time.localtime(at))
        safe_name = (name[:12]).replace("&", "&amp;").replace("<", "&lt;")
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="240" height="150">
  <rect x="1" y="1" width="238" height="148" rx="10" fill="#ffffff" stroke="#46c99a" stroke-width="3"/>
  <text x="20" y="62" font-family="sans-serif" font-size="44" font-weight="700" fill="#2e3338">{dept_short}</text>
  <text x="20" y="104" font-family="sans-serif" font-size="30" fill="#2e3338">{date}</text>
  <text x="20" y="132" font-family="monospace" font-size="14" fill="#8a9299">{code}</text>
  <text x="152" y="132" font-family="sans-serif" font-size="12" fill="#8a9299">{safe_name}</text>
</svg>"""
        rel = f"labels/{code}.svg"
        with open(os.path.join(self.data_dir, rel), "w", encoding="utf-8") as f:
            f.write(svg)
        return rel

    def get_label(self, code: str) -> dict:
        row = self._get(
            """SELECT l.*, d.short AS dept_short, u.name AS owner_name
               FROM labels l JOIN departments d ON d.id=l.dept_id
               JOIN users u ON u.id=l.owner_id WHERE l.code=?""",
            (code,), err="label_invalid", msg="标签不存在")
        return _row_to_dict(row)

    def void_label(self, user_id: int, code: str, now: float | None = None) -> dict:
        """作废标签（本人/租户管理员/物业）。作废后扫码即失效。"""
        now = self._now(now)
        user = self._user(user_id)
        label = self.get_label(code)
        allowed = (user["role"] == "property"
                   or (user["role"] == "tenant_admin" and user["dept_id"] == label["dept_id"])
                   or user["id"] == label["owner_id"])
        if not allowed:
            raise ServiceError("forbidden", "无权作废该标签", 403)
        if label["status"] != "active":
            raise ServiceError("wrong_state", "仅未使用的标签可作废")
        with self.db.tx() as conn:
            conn.execute("UPDATE labels SET status='voided', voided_at=? WHERE id=?",
                         (now, label["id"]))
            self._event(conn, None, "label_voided", user_id, code, now)
        return self.get_label(code)

    # ------------------------------------------------------------ 存放
    def store_item(self, user_id: int, label_code: str, slot_id: int,
                   now: float | None = None) -> dict:
        """第 2 步：扫码 + 选格。并发占用同一格位时仅首个事务成功。"""
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] != "employee":
            raise ServiceError("forbidden", "仅员工可存放物品", 403)
        label = self.get_label(label_code)
        if label["status"] == "voided":
            raise ServiceError("label_invalid", "标签已作废，请重新打印", 410)
        if label["status"] == "used":
            self._log_exception("duplicate_scan", None, None, None,
                                f"标签 {label_code} 重复扫码", user_id, now)
            raise ServiceError("duplicate_scan", "该标签已使用，请勿重复扫码", 409)
        if label["dept_id"] != user["dept_id"]:
            raise ServiceError("forbidden", "只能使用本部门的标签", 403)
        slot = self._get("SELECT * FROM slots WHERE id=?", (slot_id,), msg="格位不存在")
        fridge = self._get("SELECT * FROM fridges WHERE id=?", (slot["fridge_id"],))
        if fridge["status"] != "active":
            raise ServiceError("fridge_shutdown", "该冰箱已停机，请选择其他冰箱", 409)
        if slot["blocked"]:
            raise ServiceError("slot_blocked", "该格位已封锁（泄漏/维护）", 409)
        try:
            with self.db.tx() as conn:
                conn.execute("UPDATE labels SET status='used' WHERE id=?", (label["id"],))
                cur = conn.execute(
                    """INSERT INTO items(label_code, dept_id, owner_id, name, fridge_id,
                                         slot_id, status, keep_days, created_at)
                       VALUES (?,?,?,?,?,?, 'pending_store', ?, ?)""",
                    (label_code, label["dept_id"], label["owner_id"], label["item_name"],
                     slot["fridge_id"], slot_id, label["keep_days"], now))
                item_id = cur.lastrowid
                self._event(conn, item_id, "store_pending", user_id,
                            f"{fridge['code']} {slot['label']}", now)
        except sqlite3.IntegrityError as exc:
            if "label_code" in str(exc):  # 同一标签被并发使用
                self._log_exception("duplicate_scan", None, None, None,
                                    f"标签 {label_code} 并发重复扫码", user_id, now)
                raise ServiceError("duplicate_scan", "该标签已使用，请勿重复扫码", 409)
            raise ServiceError("slot_occupied", "该格位刚被占用，请另选空格", 409)
        return self.get_item(item_id)

    def confirm_store(self, user_id: int, item_id: int, now: float | None = None) -> dict:
        """第 3 步：关门确认。门磁未闭合则确认失败。"""
        now = self._now(now)
        user = self._user(user_id)
        item = self._get("SELECT * FROM items WHERE id=?", (item_id,), msg="物品不存在")
        if item["status"] != "pending_store":
            raise ServiceError("wrong_state", "该物品不在待确认状态", 409)
        if item["dept_id"] != user["dept_id"]:
            raise ServiceError("forbidden", "只能确认本部门的物品", 403)
        if self.door.is_open(item["fridge_id"]):
            raise ServiceError("door_open", "检测到冰箱门未关严，请关门后再确认", 409)
        expires_at = now + item["keep_days"] * DAY
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE items SET status='stored', stored_at=?, expires_at=? WHERE id=?",
                (now, expires_at, item_id))
            self._event(conn, item_id, "stored", user_id, "", now)
        return self.get_item(item_id)

    def cancel_pending(self, user_id: int, item_id: int, now: float | None = None) -> dict:
        """撤销未确认的存放：释放格位，标签恢复可用。"""
        now = self._now(now)
        user = self._user(user_id)
        item = self._get("SELECT * FROM items WHERE id=?", (item_id,), msg="物品不存在")
        if item["status"] != "pending_store":
            raise ServiceError("wrong_state", "仅待确认状态可撤销", 409)
        if item["dept_id"] != user["dept_id"] and user["role"] != "property":
            raise ServiceError("forbidden", "只能撤销本部门的存放", 403)
        with self.db.tx() as conn:
            conn.execute("DELETE FROM items WHERE id=?", (item_id,))
            conn.execute("UPDATE labels SET status='active' WHERE code=?",
                         (item["label_code"],))
            self._event(conn, item_id, "store_cancelled", user_id, item["label_code"], now)
        return {"cancelled": True, "label_code": item["label_code"]}

    def take_out(self, user_id: int, item_id: int, now: float | None = None) -> dict:
        """员工取回自己在架的物品。"""
        now = self._now(now)
        user = self._user(user_id)
        item = self._get("SELECT * FROM items WHERE id=?", (item_id,), msg="物品不存在")
        if item["status"] != "stored":
            raise ServiceError("wrong_state", "仅在架物品可取回", 409)
        if item["dept_id"] != user["dept_id"]:
            raise ServiceError("forbidden", "只能取回本部门的物品", 403)
        with self.db.tx() as conn:
            conn.execute("UPDATE items SET status='removed' WHERE id=?", (item_id,))
            self._event(conn, item_id, "removed", user_id, "", now)
        return self.get_item(item_id)

    # ------------------------------------------------------------ 到期
    def expire_sweep(self, now: float | None = None) -> int:
        """进程内任务：到期物品进入待清理清单。边界：expires_at <= now 即到期。"""
        now = self._now(now)
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT id FROM items WHERE status='stored' AND expires_at <= ?", (now,)
            ).fetchall()
            for r in rows:
                conn.execute("UPDATE items SET status='expired' WHERE id=?", (r["id"],))
                self._event(conn, r["id"], "expired", None, "", now)
        return len(rows)

    def release_stale_pendings(self, now: float | None = None) -> int:
        """进程内任务：超时未关门确认的暂存记录释放格位、标签恢复。"""
        now = self._now(now)
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT id, label_code FROM items WHERE status='pending_store' AND created_at < ?",
                (now - self.pending_ttl,)).fetchall()
            for r in rows:
                conn.execute("DELETE FROM items WHERE id=?", (r["id"],))
                conn.execute("UPDATE labels SET status='active' WHERE code=?",
                             (r["label_code"],))
                self._event(conn, r["id"], "pending_timeout", None, r["label_code"], now)
        return len(rows)

    # ------------------------------------------------------------ 清理
    def create_batch(self, user_id: int, fridge_id: int, now: float | None = None) -> dict:
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] != "cleaner":
            raise ServiceError("forbidden", "仅保洁人员可创建清理批次", 403)
        self._get("SELECT id FROM fridges WHERE id=?", (fridge_id,), msg="冰箱不存在")
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO batches(fridge_id, cleaner_id, status, created_at)"
                " VALUES (?,?, 'open', ?)", (fridge_id, user_id, now))
            batch_id = cur.lastrowid
        return self.get_batch(batch_id)

    def collect_item(self, user_id: int, item_id: int, batch_id: int,
                     photo_b64: str | None, now: float | None = None) -> dict:
        """保洁拍照后将到期物品移入临时保留区，申诉期自此时起算。"""
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] != "cleaner":
            raise ServiceError("forbidden", "仅保洁人员可执行清理", 403)
        item = self._get("SELECT * FROM items WHERE id=?", (item_id,), msg="物品不存在")
        if item["status"] != "expired":
            raise ServiceError("wrong_state", "该物品不在待清理清单中", 409)
        batch = self._get("SELECT * FROM batches WHERE id=?", (batch_id,), msg="批次不存在")
        if batch["status"] != "open":
            raise ServiceError("wrong_state", "清理批次已关闭", 409)
        photo_path = self._save_photo(item_id, photo_b64, now)
        retention_until = now + self.appeal_seconds
        with self.db.tx() as conn:
            conn.execute(
                """UPDATE items SET status='retention', batch_id=?, photo_path=?,
                                     retention_until=?, slot_id=NULL, fridge_id=NULL
                   WHERE id=?""",
                (batch_id, photo_path, retention_until, item_id))
            self._event(conn, item_id, "collected", user_id, f"批次#{batch_id}", now)
        return self.get_item(item_id)

    def _save_photo(self, item_id: int, photo_b64: str | None, now: float) -> str:
        if photo_b64:
            if len(photo_b64) > 3_000_000:
                raise ServiceError("photo_too_large", "照片不能超过 2MB")
            raw = base64.b64decode(photo_b64)
            ext = "png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
        else:  # 无照片时生成占位图，保证清理记录可追溯
            raw = None
            ext = "svg"
        rel = f"photos/cleanup_{item_id}_{int(now)}.{ext}"
        path = os.path.join(self.data_dir, rel)
        if raw is not None:
            with open(path, "wb") as f:
                f.write(raw)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">
  <rect width="320" height="200" fill="#eef2f0"/>
  <rect x="12" y="12" width="296" height="176" rx="8" fill="none" stroke="#46c99a"
        stroke-width="2" stroke-dasharray="6 4"/>
  <text x="160" y="96" text-anchor="middle" font-family="sans-serif" font-size="16"
        fill="#2e3338">清理照片 · 物品 #{item_id}</text>
  <text x="160" y="122" text-anchor="middle" font-family="sans-serif" font-size="12"
        fill="#8a9299">{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}</text>
</svg>""")
        return rel

    def dispose_item(self, user_id: int, item_id: int, now: float | None = None) -> dict:
        """申诉期结束且无未决申诉，方可处置。"""
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] != "cleaner":
            raise ServiceError("forbidden", "仅保洁人员可执行处置", 403)
        item = self._get("SELECT * FROM items WHERE id=?", (item_id,), msg="物品不存在")
        if item["status"] != "retention":
            raise ServiceError("wrong_state", "该物品不在临时保留区", 409)
        if now < item["retention_until"]:
            raise ServiceError("retention_not_over", "申诉期未结束，暂不能处置", 409)
        open_appeal = self.db.conn.execute(
            "SELECT id FROM appeals WHERE item_id=? AND status='open'", (item_id,)
        ).fetchone()
        if open_appeal:
            raise ServiceError("has_open_appeal", "存在未决申诉，需租户管理员复核", 409)
        with self.db.tx() as conn:
            conn.execute("UPDATE items SET status='disposed' WHERE id=?", (item_id,))
            self._event(conn, item_id, "disposed", user_id, "", now)
        return self.get_item(item_id)

    # ------------------------------------------------------------ 申诉
    def create_appeal(self, user_id: int, item_id: int, reason: str,
                      now: float | None = None) -> dict:
        """租户在申诉期内认领申诉。边界：now < retention_until 才受理。"""
        now = self._now(now)
        user = self._user(user_id)
        item = self._get("SELECT * FROM items WHERE id=?", (item_id,), msg="物品不存在")
        if item["status"] != "retention":
            raise ServiceError("wrong_state", "仅临时保留区物品可申诉", 409)
        if item["dept_id"] != user["dept_id"]:
            raise ServiceError("forbidden", "只能申诉本部门的物品", 403)
        if now >= item["retention_until"]:
            raise ServiceError("appeal_window_closed", "申诉期已结束", 409)
        dup = self.db.conn.execute(
            "SELECT id FROM appeals WHERE item_id=? AND status='open'", (item_id,)
        ).fetchone()
        if dup:
            raise ServiceError("appeal_exists", "该物品已有未决申诉", 409)
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO appeals(item_id, user_id, dept_id, reason, status, created_at)"
                " VALUES (?,?,?,?, 'open', ?)",
                (item_id, user_id, item["dept_id"], (reason or "").strip(), now))
            self._event(conn, item_id, "appeal_created", user_id, (reason or "")[:80], now)
            appeal_id = cur.lastrowid
        return self.get_appeal(appeal_id)

    def resolve_appeal(self, user_id: int, appeal_id: int, approve: bool,
                       now: float | None = None) -> dict:
        """租户管理员复核争议（仅限本部门）。批准则物品返还登记。"""
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] != "tenant_admin":
            raise ServiceError("forbidden", "仅租户管理员可复核申诉", 403)
        appeal = self._get("SELECT * FROM appeals WHERE id=?", (appeal_id,), msg="申诉不存在")
        if appeal["dept_id"] != user["dept_id"]:
            raise ServiceError("forbidden", "只能复核本部门的申诉", 403)
        if appeal["status"] != "open":
            raise ServiceError("wrong_state", "该申诉已处理", 409)
        new_status = "approved" if approve else "rejected"
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE appeals SET status=?, resolved_by=?, resolved_at=? WHERE id=?",
                (new_status, user_id, now, appeal_id))
            if approve:
                conn.execute("UPDATE items SET status='claimed_back' WHERE id=?",
                             (appeal["item_id"],))
            self._event(conn, appeal["item_id"], f"appeal_{new_status}", user_id, "", now)
        return self.get_appeal(appeal_id)

    # ------------------------------------------------------------ 停机迁移
    def start_shutdown(self, user_id: int, fridge_id: int, reason: str,
                       now: float | None = None) -> dict:
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] != "property":
            raise ServiceError("forbidden", "仅物业可执行停机", 403)
        fridge = self._get("SELECT * FROM fridges WHERE id=?", (fridge_id,), msg="冰箱不存在")
        if fridge["status"] == "shutdown":
            raise ServiceError("wrong_state", "该冰箱已处于停机状态", 409)
        with self.db.tx() as conn:
            conn.execute("UPDATE fridges SET status='shutdown' WHERE id=?", (fridge_id,))
            cur = conn.execute(
                "INSERT INTO shutdowns(fridge_id, reason, status, started_at)"
                " VALUES (?,?, 'open', ?)", (fridge_id, (reason or "").strip(), now))
            shutdown_id = cur.lastrowid
            for r in conn.execute(
                    "SELECT id FROM items WHERE fridge_id=? AND status='stored'",
                    (fridge_id,)).fetchall():
                self._event(conn, r["id"], "fridge_shutdown", user_id, reason or "", now)
        return self.get_shutdown(shutdown_id)

    def end_shutdown(self, user_id: int, shutdown_id: int, now: float | None = None) -> dict:
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] != "property":
            raise ServiceError("forbidden", "仅物业可恢复冰箱", 403)
        sd = self._get("SELECT * FROM shutdowns WHERE id=?", (shutdown_id,), msg="停机记录不存在")
        if sd["status"] != "open":
            raise ServiceError("wrong_state", "该停机已结束", 409)
        leftover = self.db.conn.execute(
            "SELECT COUNT(*) c FROM items WHERE fridge_id=? AND status='stored'",
            (sd["fridge_id"],)).fetchone()["c"]
        if leftover:
            raise ServiceError("items_remaining", "仍有在架物品，请先完成迁移", 409)
        with self.db.tx() as conn:
            conn.execute("UPDATE shutdowns SET status='closed', ended_at=? WHERE id=?",
                         (now, shutdown_id))
            conn.execute("UPDATE fridges SET status='active' WHERE id=?", (sd["fridge_id"],))
        return self.get_shutdown(shutdown_id)

    def migrate(self, user_id: int, shutdown_id: int, to_fridge_id: int,
                now: float | None = None) -> dict:
        """把停机冰箱的未到期物品批量迁入目标冰箱，保留原标签。

        目标格位不足时，剩余物品记为迁移缺件并登记异常。
        """
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] != "property":
            raise ServiceError("forbidden", "仅物业可执行迁移", 403)
        sd = self._get("SELECT * FROM shutdowns WHERE id=?", (shutdown_id,), msg="停机记录不存在")
        if sd["status"] != "open":
            raise ServiceError("wrong_state", "该停机已结束", 409)
        target = self._get("SELECT * FROM fridges WHERE id=?", (to_fridge_id,), msg="目标冰箱不存在")
        if target["status"] != "active":
            raise ServiceError("fridge_shutdown", "目标冰箱不可用", 409)
        if to_fridge_id == sd["fridge_id"]:
            raise ServiceError("same_fridge", "目标冰箱不能是停机冰箱本身", 409)
        with self.db.tx() as conn:
            items = conn.execute(
                """SELECT id, label_code FROM items
                   WHERE fridge_id=? AND status='stored' AND expires_at > ?
                   ORDER BY stored_at""", (sd["fridge_id"], now)).fetchall()
            free_slots = conn.execute(
                """SELECT s.id, s.label FROM slots s
                   WHERE s.fridge_id=? AND s.blocked=0 AND NOT EXISTS (
                       SELECT 1 FROM items i WHERE i.slot_id=s.id
                         AND i.status IN ('pending_store','stored','expired'))
                   ORDER BY s.row, s.col""", (to_fridge_id,)).fetchall()
            moved, missing = 0, 0
            for idx, item in enumerate(items):
                if idx < len(free_slots):
                    slot = free_slots[idx]
                    conn.execute(
                        "UPDATE items SET fridge_id=?, slot_id=? WHERE id=?",
                        (to_fridge_id, slot["id"], item["id"]))
                    self._event(conn, item["id"], "migrated", user_id,
                                f"→ {target['code']} {slot['label']}（保留标签 "
                                f"{item['label_code']}）", now)
                    moved += 1
                else:
                    missing += 1
                    conn.execute(
                        """INSERT INTO exceptions(type, fridge_id, item_id, note,
                                                  status, created_by, created_at)
                           VALUES ('migration_missing', ?, ?, ?, 'open', ?, ?)""",
                        (sd["fridge_id"], item["id"],
                         f"迁移缺件：{item['label_code']} 未能迁入 {target['code']}",
                         user_id, now))
                    self._event(conn, item["id"], "migration_missing", user_id, "", now)
            cur = conn.execute(
                """INSERT INTO migrations(shutdown_id, from_fridge, to_fridge, moved,
                                          missing, operator, at)
                   VALUES (?,?,?,?,?,?,?)""",
                (shutdown_id, sd["fridge_id"], to_fridge_id, moved, missing, user_id, now))
            migration_id = cur.lastrowid
        return self.get_migration(migration_id)

    # ------------------------------------------------------------ 异常
    def report_exception(self, user_id: int, type_: str, fridge_id=None, slot_id=None,
                         item_id=None, note="", now: float | None = None) -> dict:
        now = self._now(now)
        self._user(user_id)
        allowed = {"unlabeled", "wrong_slot", "leak"}
        if type_ not in allowed:
            raise ServiceError("bad_type", f"异常类型仅支持：{'/'.join(sorted(allowed))}")
        with self.db.tx() as conn:
            exc_id = self._log_exception(type_, fridge_id, slot_id, item_id, note,
                                         user_id, now, conn)
            if type_ == "leak" and slot_id:  # 泄漏格位封锁待清洁
                conn.execute("UPDATE slots SET blocked=1 WHERE id=?", (slot_id,))
        return self.get_exception(exc_id)

    def _log_exception(self, type_, fridge_id, slot_id, item_id, note,
                       user_id, now, conn=None) -> int:
        own = conn is None
        conn = conn or self.db.conn
        if own:
            conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """INSERT INTO exceptions(type, fridge_id, slot_id, item_id, note,
                                          status, created_by, created_at)
                   VALUES (?,?,?,?,?, 'open', ?, ?)""",
                (type_, fridge_id, slot_id, item_id, note, user_id, now))
            exc_id = cur.lastrowid
            if own:
                conn.execute("COMMIT")
        except Exception:
            if own:
                conn.execute("ROLLBACK")
            raise
        return exc_id

    def resolve_exception(self, user_id: int, exc_id: int, now: float | None = None) -> dict:
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] not in ("property", "cleaner"):
            raise ServiceError("forbidden", "仅物业/保洁可处理异常", 403)
        exc = self._get("SELECT * FROM exceptions WHERE id=?", (exc_id,), msg="异常不存在")
        if exc["status"] != "open":
            raise ServiceError("wrong_state", "该异常已处理", 409)
        if user["role"] == "cleaner" and exc["type"] not in ("leak", "unlabeled"):
            raise ServiceError("forbidden", "保洁仅可处理泄漏与无标签异常", 403)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE exceptions SET status='resolved', resolved_by=?, resolved_at=?"
                " WHERE id=?", (now, user_id, exc_id))
            if exc["type"] == "leak" and exc["slot_id"]:
                conn.execute("UPDATE slots SET blocked=0 WHERE id=?", (exc["slot_id"],))
        return self.get_exception(exc_id)

    # ------------------------------------------------------------ 温度与卫生
    def record_temp(self, fridge_id: int, value: float | None = None,
                    now: float | None = None) -> dict:
        """记录温度；超限自动生成 temp_alert 异常，恢复后自动结案。"""
        now = self._now(now)
        fridge = self._get("SELECT * FROM fridges WHERE id=?", (fridge_id,), msg="冰箱不存在")
        value = self.temp.read(fridge_id, fridge["status"]) if value is None else value
        with self.db.tx() as conn:
            conn.execute("INSERT INTO temps(fridge_id, value, at) VALUES (?,?,?)",
                         (fridge_id, value, now))
            open_alert = conn.execute(
                "SELECT id FROM exceptions WHERE type='temp_alert' AND fridge_id=?"
                " AND status='open'", (fridge_id,)).fetchone()
            out_of_range = not (fridge["temp_min"] <= value <= fridge["temp_max"])
            if out_of_range and not open_alert:
                conn.execute(
                    """INSERT INTO exceptions(type, fridge_id, note, status, created_at)
                       VALUES ('temp_alert', ?, ?, 'open', ?)""",
                    (fridge_id,
                     f"温度 {value}℃ 超出 [{fridge['temp_min']}, {fridge['temp_max']}]℃", now))
            elif not out_of_range and open_alert:
                conn.execute(
                    "UPDATE exceptions SET status='resolved', resolved_at=? WHERE id=?",
                    (now, open_alert["id"]))
        return {"fridge_id": fridge_id, "value": value, "at": now,
                "out_of_range": out_of_range}

    def inspect(self, user_id: int, fridge_id: int, result: str, notes: str = "",
                now: float | None = None) -> dict:
        now = self._now(now)
        user = self._user(user_id)
        if user["role"] != "property":
            raise ServiceError("forbidden", "仅物业可登记卫生检查", 403)
        if result not in ("pass", "fail"):
            raise ServiceError("bad_result", "检查结果仅支持 pass/fail")
        self._get("SELECT id FROM fridges WHERE id=?", (fridge_id,), msg="冰箱不存在")
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO inspections(fridge_id, inspector_id, result, notes, at)"
                " VALUES (?,?,?,?,?)", (fridge_id, user_id, result, notes, now))
            insp_id = cur.lastrowid
        return _row_to_dict(self.db.conn.execute(
            "SELECT * FROM inspections WHERE id=?", (insp_id,)).fetchone())

    # ------------------------------------------------------------ 查询
    def get_item(self, item_id: int) -> dict:
        row = self._get(
            """SELECT i.*, d.short AS dept_short, u.name AS owner_name,
                      s.label AS slot_label, f.code AS fridge_code
               FROM items i
               JOIN departments d ON d.id=i.dept_id
               JOIN users u ON u.id=i.owner_id
               LEFT JOIN slots s ON s.id=i.slot_id
               LEFT JOIN fridges f ON f.id=i.fridge_id
               WHERE i.id=?""", (item_id,))
        return _row_to_dict(row)

    def get_batch(self, batch_id: int) -> dict:
        row = self._get(
            """SELECT b.*, f.code AS fridge_code, u.name AS cleaner_name
               FROM batches b JOIN fridges f ON f.id=b.fridge_id
               JOIN users u ON u.id=b.cleaner_id WHERE b.id=?""", (batch_id,))
        d = _row_to_dict(row)
        d["items"] = [self.get_item(r["id"]) for r in self.db.conn.execute(
            "SELECT id FROM items WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()]
        return d

    def get_appeal(self, appeal_id: int) -> dict:
        row = self._get(
            """SELECT a.*, u.name AS user_name, d.short AS dept_short,
                      i.name AS item_name, i.photo_path, i.retention_until
               FROM appeals a
               JOIN users u ON u.id=a.user_id
               JOIN departments d ON d.id=a.dept_id
               JOIN items i ON i.id=a.item_id
               WHERE a.id=?""", (appeal_id,))
        return _row_to_dict(row)

    def get_exception(self, exc_id: int) -> dict:
        row = self._get(
            """SELECT e.*, f.code AS fridge_code, s.label AS slot_label
               FROM exceptions e
               LEFT JOIN fridges f ON f.id=e.fridge_id
               LEFT JOIN slots s ON s.id=e.slot_id
               WHERE e.id=?""", (exc_id,))
        return _row_to_dict(row)

    def get_shutdown(self, shutdown_id: int) -> dict:
        row = self._get(
            """SELECT sd.*, f.code AS fridge_code, f.name AS fridge_name
               FROM shutdowns sd JOIN fridges f ON f.id=sd.fridge_id
               WHERE sd.id=?""", (shutdown_id,))
        d = _row_to_dict(row)
        d["remaining"] = self.db.conn.execute(
            "SELECT COUNT(*) c FROM items WHERE fridge_id=? AND status='stored'",
            (d["fridge_id"],)).fetchone()["c"]
        return d

    def get_migration(self, migration_id: int) -> dict:
        row = self._get(
            """SELECT m.*, fa.code AS from_code, fb.code AS to_code, u.name AS operator_name
               FROM migrations m
               JOIN fridges fa ON fa.id=m.from_fridge
               JOIN fridges fb ON fb.id=m.to_fridge
               JOIN users u ON u.id=m.operator WHERE m.id=?""", (migration_id,))
        return _row_to_dict(row)

    def list_fridges(self) -> list[dict]:
        out = []
        for f in self.db.conn.execute(
                "SELECT * FROM fridges ORDER BY code").fetchall():
            d = _row_to_dict(f)
            stats = self.db.conn.execute(
                """SELECT
                     SUM(CASE WHEN i.status IN ('pending_store','stored','expired') THEN 1 ELSE 0 END) AS used,
                     SUM(CASE WHEN i.status='stored' AND i.expires_at<=? THEN 1 ELSE 0 END) AS expiring
                   FROM slots s LEFT JOIN items i ON i.slot_id=s.id
                   WHERE s.fridge_id=?""", (time.time(), f["id"])).fetchone()
            d["used_slots"] = stats["used"] or 0
            d["expiring"] = stats["expiring"] or 0
            d["total_slots"] = f["rows"] * f["cols"]
            d["door_open"] = self.door.is_open(f["id"])
            t = self.db.conn.execute(
                "SELECT value, at FROM temps WHERE fridge_id=? ORDER BY at DESC LIMIT 1",
                (f["id"],)).fetchone()
            d["last_temp"] = t["value"] if t else None
            out.append(d)
        return out

    def slot_map(self, fridge_id: int, viewer_dept_id: int | None) -> dict:
        fridge = self._get("SELECT * FROM fridges WHERE id=?", (fridge_id,), msg="冰箱不存在")
        rows = self.db.conn.execute(
            """SELECT s.*, i.id AS item_id, i.status AS item_status, i.name AS item_name,
                      i.stored_at, i.expires_at, i.dept_id, d.short AS dept_short
               FROM slots s
               LEFT JOIN items i ON i.slot_id=s.id
                 AND i.status IN ('pending_store','stored','expired')
               LEFT JOIN departments d ON d.id=i.dept_id
               WHERE s.fridge_id=? ORDER BY s.row, s.col""", (fridge_id,)).fetchall()
        now = time.time()
        slots = []
        for r in rows:
            d = _row_to_dict(r)
            d["mine"] = bool(d["dept_id"] and viewer_dept_id and d["dept_id"] == viewer_dept_id)
            d["expired"] = bool(d["item_status"] == "expired" or
                                (d["item_status"] == "stored" and d["expires_at"]
                                 and d["expires_at"] <= now))
            # 标签隐私：格位图只暴露部门简称与日期
            d.pop("item_name", None)
            slots.append(d)
        return {"fridge": _row_to_dict(fridge), "slots": slots}

    def cleanup_queue(self, fridge_id: int | None = None) -> list[dict]:
        sql = """SELECT i.*, d.short AS dept_short, u.name AS owner_name,
                        s.label AS slot_label, f.code AS fridge_code
                 FROM items i
                 JOIN departments d ON d.id=i.dept_id
                 JOIN users u ON u.id=i.owner_id
                 LEFT JOIN slots s ON s.id=i.slot_id
                 LEFT JOIN fridges f ON f.id=i.fridge_id
                 WHERE i.status='expired'"""
        args: list = []
        if fridge_id:
            sql += " AND i.fridge_id=?"
            args.append(fridge_id)
        sql += " ORDER BY i.expires_at"
        return [_row_to_dict(r) for r in self.db.conn.execute(sql, args).fetchall()]

    def retention_list(self) -> list[dict]:
        now = time.time()
        out = []
        for r in self.db.conn.execute(
                """SELECT i.*, d.short AS dept_short, u.name AS owner_name
                   FROM items i JOIN departments d ON d.id=i.dept_id
                   JOIN users u ON u.id=i.owner_id
                   WHERE i.status='retention' ORDER BY i.retention_until""").fetchall():
            d = _row_to_dict(r)
            d["seconds_left"] = max(0, int(d["retention_until"] - now))
            d["open_appeal"] = bool(self.db.conn.execute(
                "SELECT 1 FROM appeals WHERE item_id=? AND status='open'",
                (d["id"],)).fetchone())
            out.append(d)
        return out

    def dept_items(self, dept_id: int) -> list[dict]:
        """租户视角：仅本部门物品（租户隔离）。"""
        return [_row_to_dict(r) for r in self.db.conn.execute(
            """SELECT i.*, d.short AS dept_short, u.name AS owner_name,
                      s.label AS slot_label, f.code AS fridge_code
               FROM items i
               JOIN departments d ON d.id=i.dept_id
               JOIN users u ON u.id=i.owner_id
               LEFT JOIN slots s ON s.id=i.slot_id
               LEFT JOIN fridges f ON f.id=i.fridge_id
               WHERE i.dept_id=? AND i.status IN
                     ('pending_store','stored','expired','retention')
               ORDER BY i.id DESC""", (dept_id,)).fetchall()]

    def list_appeals(self, user_id: int) -> list[dict]:
        """申诉列表按角色隔离：员工/管理员看本部门，物业/保洁看全部。"""
        user = self._user(user_id)
        sql = """SELECT a.*, u.name AS user_name, d.short AS dept_short,
                        i.name AS item_name, i.photo_path, i.retention_until
                 FROM appeals a
                 JOIN users u ON u.id=a.user_id
                 JOIN departments d ON d.id=a.dept_id
                 JOIN items i ON i.id=a.item_id"""
        args: list = []
        if user["role"] in ("employee", "tenant_admin"):
            sql += " WHERE a.dept_id=?"
            args.append(user["dept_id"])
        sql += " ORDER BY a.status='open' DESC, a.id DESC"
        return [_row_to_dict(r) for r in self.db.conn.execute(sql, args).fetchall()]

    def list_exceptions(self, status: str | None = None) -> list[dict]:
        sql = """SELECT e.*, f.code AS fridge_code, s.label AS slot_label,
                        u.name AS reporter
                 FROM exceptions e
                 LEFT JOIN fridges f ON f.id=e.fridge_id
                 LEFT JOIN slots s ON s.id=e.slot_id
                 LEFT JOIN users u ON u.id=e.created_by"""
        args: list = []
        if status:
            sql += " WHERE e.status=?"
            args.append(status)
        sql += " ORDER BY e.status='open' DESC, e.id DESC"
        return [_row_to_dict(r) for r in self.db.conn.execute(sql, args).fetchall()]

    def list_batches(self) -> list[dict]:
        return [self.get_batch(r["id"]) for r in self.db.conn.execute(
            "SELECT id FROM batches ORDER BY id DESC").fetchall()]

    def list_shutdowns(self) -> list[dict]:
        return [self.get_shutdown(r["id"]) for r in self.db.conn.execute(
            "SELECT id FROM shutdowns ORDER BY id DESC").fetchall()]

    def list_migrations(self) -> list[dict]:
        return [self.get_migration(r["id"]) for r in self.db.conn.execute(
            "SELECT id FROM migrations ORDER BY id DESC").fetchall()]

    def list_temps(self, fridge_id: int, limit: int = 60) -> list[dict]:
        return [_row_to_dict(r) for r in self.db.conn.execute(
            "SELECT * FROM temps WHERE fridge_id=? ORDER BY at DESC LIMIT ?",
            (fridge_id, limit)).fetchall()][::-1]

    def list_inspections(self) -> list[dict]:
        return [_row_to_dict(r) for r in self.db.conn.execute(
            """SELECT i.*, f.code AS fridge_code, u.name AS inspector_name
               FROM inspections i JOIN fridges f ON f.id=i.fridge_id
               JOIN users u ON u.id=i.inspector_id ORDER BY i.at DESC""").fetchall()]

    def item_events(self, item_id: int) -> list[dict]:
        return [_row_to_dict(r) for r in self.db.conn.execute(
            """SELECT e.*, u.name AS actor_name FROM events e
               LEFT JOIN users u ON u.id=e.actor
               WHERE e.item_id=? ORDER BY e.at""", (item_id,)).fetchall()]

    def list_users(self) -> list[dict]:
        return [_row_to_dict(r) for r in self.db.conn.execute(
            """SELECT u.*, d.short AS dept_short, d.name AS dept_name
               FROM users u LEFT JOIN departments d ON d.id=u.dept_id
               ORDER BY u.id""").fetchall()]
