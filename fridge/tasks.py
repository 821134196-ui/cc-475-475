"""进程内后台任务：到期扫描、暂存超时释放、温度巡检。"""
from __future__ import annotations

import threading

from .services import FridgeService


class Scheduler:
    def __init__(self, service: FridgeService, interval: float = 5.0):
        self.service = service
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> dict:
        """单轮任务（测试可直接调用）。"""
        expired = self.service.expire_sweep()
        released = self.service.release_stale_pendings()
        temps = []
        for f in self.service.list_fridges():
            temps.append(self.service.record_temp(f["id"]))
        return {"expired": expired, "released": released,
                "alerts": sum(1 for t in temps if t["out_of_range"])}

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # 后台任务不拖垮进程
                print(f"[scheduler] tick failed: {exc}")
            self._stop.wait(self.interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="fridge-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
