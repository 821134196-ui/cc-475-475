"""模拟硬件适配器：门磁与温度传感器。

真实部署时替换为串口/物联网平台实现，接口保持不变。
"""
from __future__ import annotations

import random
import threading


class DoorSensorAdapter:
    """模拟门磁：记录每台冰箱门的开/关状态。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[int, bool] = {}  # fridge_id -> 门是否打开

    def set(self, fridge_id: int, is_open: bool) -> None:
        with self._lock:
            self._state[fridge_id] = is_open

    def is_open(self, fridge_id: int) -> bool:
        with self._lock:
            return self._state.get(fridge_id, False)

    def is_closed(self, fridge_id: int) -> bool:
        return not self.is_open(fridge_id)


class TempSensorAdapter:
    """模拟温度探头：冷藏基准 4℃，停机冰箱温度持续回升。

    测试可用 set_override 注入固定读数。
    """

    def __init__(self, seed: int = 42):
        self._rand = random.Random(seed)
        self._lock = threading.Lock()
        self._overrides: dict[int, float] = {}
        self._drift: dict[int, float] = {}  # 停机冰箱的累计升温

    def set_override(self, fridge_id: int, value: float | None) -> None:
        with self._lock:
            if value is None:
                self._overrides.pop(fridge_id, None)
            else:
                self._overrides[fridge_id] = value

    def read(self, fridge_id: int, fridge_status: str = "active") -> float:
        with self._lock:
            if fridge_id in self._overrides:
                return self._overrides[fridge_id]
            if fridge_status == "shutdown":
                drift = self._drift.get(fridge_id, 0.0) + self._rand.uniform(0.2, 0.6)
                self._drift[fridge_id] = min(drift, 14.0)
                return round(4.0 + drift, 2)
            self._drift.pop(fridge_id, None)
            return round(4.0 + self._rand.uniform(-1.2, 1.2), 2)
