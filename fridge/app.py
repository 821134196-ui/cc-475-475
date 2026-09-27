"""FastAPI 接口层：角色守卫 + 租户隔离 + 静态页面。"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .adapters import DoorSensorAdapter, TempSensorAdapter
from .db import Database
from .seed import seed
from .services import ServiceError, FridgeService
from .tasks import Scheduler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


def create_app(db_path: str | None = None, data_dir: str | None = None,
               start_scheduler: bool = True) -> FastAPI:
    db_path = db_path or to_data("fridge.db")
    data_dir = data_dir or os.path.join(os.path.dirname(db_path) or ".", "files")
    data_dir = os.path.abspath(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    db = Database(db_path)
    door = DoorSensorAdapter()
    temp = TempSensorAdapter()
    svc = FridgeService(db, data_dir, door, temp)
    seed(db, svc)
    scheduler = Scheduler(svc, interval=float(os.environ.get("FRIDGE_TICK", "5")))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="写字楼公共冰箱格位与过期清理系统", lifespan=_lifespan)
    app.state.svc = svc
    app.state.scheduler = scheduler

    @app.exception_handler(ServiceError)
    def _service_error(_: Request, exc: ServiceError):
        return JSONResponse({"error": exc.code, "message": exc.message},
                            status_code=exc.http)

    # -------------------------------------------------- 身份（演示用请求头）
    def current_user(x_user_id: int = Header(default=1)) -> dict:
        row = db.conn.execute(
            """SELECT u.*, d.short AS dept_short FROM users u
               LEFT JOIN departments d ON d.id=u.dept_id WHERE u.id=?""",
            (x_user_id,)).fetchone()
        if row is None:
            raise ServiceError("no_user", "用户不存在", 401)
        return dict(row)

    def require(*roles):
        def guard(user=Depends(current_user)):
            if user["role"] not in roles:
                raise ServiceError("forbidden", "当前角色无权执行该操作", 403)
            return user
        return guard

    # -------------------------------------------------- 基础数据
    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return user

    @app.get("/api/users")
    def users():
        return svc.list_users()

    @app.get("/api/fridges")
    def fridges():
        return svc.list_fridges()

    @app.get("/api/fridges/{fridge_id}/slots")
    def slots(fridge_id: int, user=Depends(current_user)):
        return svc.slot_map(fridge_id, user.get("dept_id"))

    # -------------------------------------------------- 存放三步
    @app.post("/api/labels")
    def create_label(body: dict, user=Depends(require("employee"))):
        return svc.create_label(user["id"], body.get("item_name", ""),
                                int(body.get("keep_days", 3)))

    @app.get("/api/labels/{code}")
    def get_label(code: str):
        return svc.get_label(code)

    @app.post("/api/labels/{code}/void")
    def void_label(code: str, user=Depends(current_user)):
        return svc.void_label(user["id"], code)

    @app.post("/api/items/store")
    def store(body: dict, user=Depends(require("employee"))):
        return svc.store_item(user["id"], body.get("label_code", ""),
                              int(body.get("slot_id", 0)))

    @app.post("/api/items/{item_id}/confirm")
    def confirm(item_id: int, user=Depends(require("employee"))):
        return svc.confirm_store(user["id"], item_id)

    @app.post("/api/items/{item_id}/cancel")
    def cancel(item_id: int, user=Depends(current_user)):
        return svc.cancel_pending(user["id"], item_id)

    @app.post("/api/items/{item_id}/take-out")
    def take_out(item_id: int, user=Depends(require("employee"))):
        return svc.take_out(user["id"], item_id)

    @app.get("/api/my/items")
    def my_items(user=Depends(current_user)):
        if not user.get("dept_id"):
            return []
        return svc.dept_items(user["dept_id"])

    @app.get("/api/items/{item_id}/events")
    def item_events(item_id: int):
        return svc.item_events(item_id)

    # -------------------------------------------------- 清理（保洁）
    @app.get("/api/cleanup/queue")
    def cleanup_queue(fridge_id: int | None = None,
                      user=Depends(require("cleaner", "property"))):
        return svc.cleanup_queue(fridge_id)

    @app.get("/api/cleanup/retention")
    def retention(user=Depends(require("cleaner", "property"))):
        return svc.retention_list()

    @app.post("/api/batches")
    def create_batch(body: dict, user=Depends(require("cleaner"))):
        return svc.create_batch(user["id"], int(body.get("fridge_id", 0)))

    @app.get("/api/batches")
    def list_batches(user=Depends(require("cleaner", "property"))):
        return svc.list_batches()

    @app.post("/api/batches/{batch_id}/collect/{item_id}")
    def collect(batch_id: int, item_id: int, body: dict,
                user=Depends(require("cleaner"))):
        return svc.collect_item(user["id"], item_id, batch_id, body.get("photo"))

    @app.post("/api/items/{item_id}/dispose")
    def dispose(item_id: int, user=Depends(require("cleaner"))):
        return svc.dispose_item(user["id"], item_id)

    # -------------------------------------------------- 申诉
    @app.get("/api/appeals")
    def appeals(user=Depends(current_user)):
        return svc.list_appeals(user["id"])

    @app.post("/api/items/{item_id}/appeal")
    def create_appeal(item_id: int, body: dict, user=Depends(require("employee"))):
        return svc.create_appeal(user["id"], item_id, body.get("reason", ""))

    @app.post("/api/appeals/{appeal_id}/resolve")
    def resolve_appeal(appeal_id: int, body: dict,
                       user=Depends(require("tenant_admin"))):
        return svc.resolve_appeal(user["id"], appeal_id, bool(body.get("approve")))

    # -------------------------------------------------- 物业：停机迁移 / 异常 / 温度 / 卫生
    @app.post("/api/fridges/{fridge_id}/shutdown")
    def start_shutdown(fridge_id: int, body: dict,
                       user=Depends(require("property"))):
        return svc.start_shutdown(user["id"], fridge_id, body.get("reason", ""))

    @app.post("/api/shutdowns/{shutdown_id}/end")
    def end_shutdown(shutdown_id: int, user=Depends(require("property"))):
        return svc.end_shutdown(user["id"], shutdown_id)

    @app.post("/api/shutdowns/{shutdown_id}/migrate")
    def migrate(shutdown_id: int, body: dict, user=Depends(require("property"))):
        return svc.migrate(user["id"], shutdown_id, int(body.get("to_fridge_id", 0)))

    @app.get("/api/shutdowns")
    def shutdowns(user=Depends(require("property"))):
        return svc.list_shutdowns()

    @app.get("/api/migrations")
    def migrations(user=Depends(require("property"))):
        return svc.list_migrations()

    @app.get("/api/exceptions")
    def exceptions(status: str | None = None, user=Depends(current_user)):
        return svc.list_exceptions(status)

    @app.post("/api/exceptions")
    def report_exception(body: dict, user=Depends(current_user)):
        return svc.report_exception(user["id"], body.get("type", ""),
                                    body.get("fridge_id"), body.get("slot_id"),
                                    body.get("item_id"), body.get("note", ""))

    @app.post("/api/exceptions/{exc_id}/resolve")
    def resolve_exception(exc_id: int, user=Depends(require("property", "cleaner"))):
        return svc.resolve_exception(user["id"], exc_id)

    @app.get("/api/fridges/{fridge_id}/temps")
    def temps(fridge_id: int, user=Depends(current_user)):
        return svc.list_temps(fridge_id)

    @app.post("/api/fridges/{fridge_id}/inspect")
    def inspect(fridge_id: int, body: dict, user=Depends(require("property"))):
        return svc.inspect(user["id"], fridge_id, body.get("result", "pass"),
                           body.get("notes", ""))

    @app.get("/api/inspections")
    def inspections(user=Depends(current_user)):
        return svc.list_inspections()

    # -------------------------------------------------- 模拟适配器调试口
    @app.post("/api/sim/door")
    def sim_door(body: dict, user=Depends(current_user)):
        fid = int(body.get("fridge_id", 0))
        door.set(fid, bool(body.get("open")))
        return {"fridge_id": fid, "open": door.is_open(fid)}

    @app.get("/api/sim/door/{fridge_id}")
    def sim_door_state(fridge_id: int):
        return {"fridge_id": fridge_id, "open": door.is_open(fridge_id)}

    @app.post("/api/sim/temp")
    def sim_temp(body: dict, user=Depends(require("property"))):
        fid = int(body.get("fridge_id", 0))
        temp.set_override(fid, body.get("value"))
        return {"fridge_id": fid, "override": body.get("value")}

    @app.post("/api/sim/tick")
    def sim_tick(user=Depends(current_user)):
        return scheduler.tick()

    # -------------------------------------------------- 文件与页面
    @app.get("/files/{path:path}")
    def files(path: str):
        full = os.path.normpath(os.path.join(data_dir, path))
        if not full.startswith(os.path.abspath(data_dir)):
            raise ServiceError("forbidden", "非法路径", 403)
        if not os.path.isfile(full):
            raise ServiceError("not_found", "文件不存在", 404)
        return FileResponse(full)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def to_data(name: str) -> str:
    root = os.environ.get("FRIDGE_DATA", os.path.join(os.getcwd(), "data"))
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, name)


app = create_app()
