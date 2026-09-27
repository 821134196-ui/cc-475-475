# -*- coding: utf-8 -*-
"""HTTP 服务 + 进程内定时任务。启动：python3 fridge/server.py [--port 8000] [--reset]"""
import argparse
import json
import os
import shutil
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import (MEDIA_DIR, BusinessError, Clock, Service, Store, TEMP_LIMITS)
import seed_data

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

TICK_SECONDS = 10

svc = None


class Handler(BaseHTTPRequestHandler):
    server_version = "FridgeGrid/1.0"

    # ---------- 工具 ----------
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            raise BusinessError("bad_json", "请求体不是合法 JSON")

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] " + fmt % args + "\n")

    # ---------- 路由 ----------
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self._serve_file(os.path.join(WEB_DIR, "index.html"), "text/html")
            if p.startswith("/media/"):
                return self._serve_media(p[len("/media/"):])
            if p == "/api/users":
                return self._json(svc.users())
            if p == "/api/map":
                return self._json(svc.map_data())
            if p == "/api/expiry":
                return self._json(svc.expiry_queue())
            if p == "/api/batches":
                return self._json(svc.batches())
            if p == "/api/appeals":
                return self._json(svc.appeals_open())
            if p == "/api/issues":
                return self._json(svc.issues(q.get("kind", [None])[0],
                                             q.get("status", [None])[0]))
            if p == "/api/checks":
                return self._json(svc.checks())
            if p == "/api/labels":
                return self._json(svc.labels())
            if p == "/api/migration":
                fid = q.get("fridge_id", [""])[0]
                return self._json({"plan": svc.migration_targets(fid),
                                   "fridge": dict(svc.store.q1(
                                       "SELECT * FROM fridges WHERE id=?", (fid,)))})
            if p == "/api/temps":
                fid = q.get("fridge_id", ["F1"])[0]
                return self._json({"points": svc.temps(fid), "limits": TEMP_LIMITS,
                                   "fridges": [dict(r) for r in
                                               svc.store.q("SELECT id,name,status FROM fridges ORDER BY id")]})
            self._json({"error": "not found"}, 404)
        except BusinessError as e:
            self._json({"error": e.code, "message": str(e)}, 400)
        except Exception as e:  # noqa
            self._json({"error": "server", "message": str(e)}, 500)

    def do_POST(self):
        from urllib.parse import urlparse
        p = urlparse(self.path).path
        try:
            b = self._body()
            if p == "/api/label":
                return self._json(svc.issue_label(b["uid"]))
            if p == "/api/label/revoke":
                return self._json(svc.revoke_label(b["uid"], b["code"]))
            if p == "/api/store/start":
                return self._json(svc.start_store(b["uid"], b["label"], b["cell"]))
            if p == "/api/store/confirm":
                return self._json(svc.confirm_store(b["uid"], b["token"]))
            if p == "/api/store/undo":
                return self._json(svc.undo_store(b["uid"], b["item_id"]))
            if p == "/api/clean":
                return self._json(svc.clean_items(b["uid"], b.get("item_ids", []),
                                                  b.get("note", "")))
            if p == "/api/appeal":
                return self._json(svc.appeal(b["uid"], b["item_id"], b.get("reason", "")))
            if p == "/api/appeal/review":
                return self._json(svc.review_appeal(b["uid"], b["item_id"],
                                                    b["verdict"], b.get("cell")))
            if p == "/api/claim":
                return self._json(svc.claim(b["uid"], b["item_id"]))
            if p == "/api/outage/begin":
                return self._json(svc.begin_outage(b["uid"], b["fridge_id"],
                                                   b.get("resume_minutes", 120)))
            if p == "/api/outage/migrate":
                return self._json(svc.migrate_outage(b["uid"], b["fridge_id"],
                                                     b.get("missing_ids", [])))
            if p == "/api/outage/end":
                return self._json(svc.end_outage(b["uid"], b["fridge_id"]))
            if p == "/api/temp/fault":
                svc.temp_sensor.set_fault(b["fridge_id"], bool(b.get("fault", True)))
                svc.poll_temperature()
                return self._json({"ok": True})
            if p == "/api/issue/unlabeled":
                return self._json(svc.report_unlabeled(b["uid"], b["fridge_id"],
                                                       b.get("cell", ""), b.get("detail", "")))
            if p == "/api/issue/wrong_cell":
                return self._json(svc.report_wrong_cell(b["uid"], b["item_id"], b["actual_cell"]))
            if p == "/api/issue/leak":
                return self._json(svc.report_leak(b["uid"], b["fridge_id"],
                                                  b.get("cell", ""), b.get("detail", "")))
            if p == "/api/issue/resolve":
                return self._json(svc.resolve_issue(b["uid"], b["issue_id"],
                                                    b.get("resolution", "")))
            if p == "/api/hygiene":
                return self._json(svc.hygiene_check(b["uid"], b["fridge_id"],
                                                    int(b["score"]), b.get("finding", "")))
            if p == "/api/tick":
                # 演示用：立即跑一轮到期/申诉期扫描
                return self._json({"expired": svc.scan_expiry(),
                                   "disposed": svc.scan_appeal_deadline()})
            self._json({"error": "not found"}, 404)
        except BusinessError as e:
            self._json({"error": e.code, "message": str(e)}, 400)
        except KeyError as e:
            self._json({"error": "bad_param", "message": f"缺少参数 {e}"}, 400)
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            self._json({"error": "server", "message": str(e)}, 500)

    # ---------- 静态 ----------
    def _serve_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_media(self, rel):
        # 防目录穿越
        base = os.path.realpath(MEDIA_DIR)
        target = os.path.realpath(os.path.join(base, rel))
        if not target.startswith(base + os.sep) or not os.path.isfile(target):
            return self._json({"error": "not found"}, 404)
        ctype = "image/svg+xml" if target.endswith(".svg") else "application/octet-stream"
        self._serve_file(target, ctype)


def scheduler_loop():
    """进程内任务：到期入队 / 申诉期满处置 / 温度轮询（含模拟适配器）。"""
    import time
    while True:
        try:
            svc.scan_expiry()
            svc.scan_appeal_deadline()
            svc.poll_temperature()
        except Exception as e:  # noqa
            print("[scheduler] error:", e, file=sys.stderr)
        time.sleep(TICK_SECONDS)


def main():
    global svc
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="清空数据并重新生成样例")
    args = ap.parse_args()

    if args.reset and os.path.exists(os.path.dirname(MEDIA_DIR)):
        shutil.rmtree(os.path.dirname(MEDIA_DIR))

    clock = Clock()
    store = Store(clock)
    svc = Service(store, clock)
    created = seed_data.run(store, svc)
    if created:
        print("[seed] 已生成示例数据（含无标签 / 停机 / 申诉样例）")

    import threading
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"冰箱格位系统已启动: http://localhost:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
