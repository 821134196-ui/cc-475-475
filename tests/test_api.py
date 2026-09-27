"""API 冒烟测试：应用可启动、核心链路可走通。"""
import time

import pytest
from fastapi.testclient import TestClient

from fridge.app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDGE_APPEAL_SECONDS", "3600")
    app = create_app(db_path=str(tmp_path / "api.db"),
                     data_dir=str(tmp_path / "files"),
                     start_scheduler=False)
    with TestClient(app) as c:
        yield c


def test_full_store_flow_and_slot_map(client):
    users = client.get("/api/users").json()
    emp = next(u for u in users if u["role"] == "employee")
    h = {"X-User-Id": str(emp["id"])}

    label = client.post("/api/labels", json={"item_name": "气泡水", "keep_days": 3},
                        headers=h).json()
    assert label["code"].startswith("LBL-")

    fridges = client.get("/api/fridges").json()
    active = next(f for f in fridges if f["status"] == "active")
    slots = client.get(f"/api/fridges/{active['id']}/slots", headers=h).json()["slots"]
    free = next(s for s in slots if not s["item_id"] and not s["blocked"])

    item = client.post("/api/items/store",
                       json={"label_code": label["code"], "slot_id": free["id"]},
                       headers=h).json()
    assert item["status"] == "pending_store"

    done = client.post(f"/api/items/{item['id']}/confirm", headers=h).json()
    assert done["status"] == "stored"

    # 格位图只暴露部门简称与日期
    slots2 = client.get(f"/api/fridges/{active['id']}/slots", headers=h).json()["slots"]
    occ = next(s for s in slots2 if s["id"] == free["id"])
    assert occ["dept_short"] and occ["stored_at"] and "item_name" not in occ

    # 重复扫码被拒
    other = next(s for s in slots2 if not s["item_id"] and not s["blocked"])
    r = client.post("/api/items/store",
                    json={"label_code": label["code"], "slot_id": other["id"]}, headers=h)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_scan"


def test_cleanup_requires_cleaner_role(client):
    users = client.get("/api/users").json()
    emp = next(u for u in users if u["role"] == "employee")
    r = client.post("/api/batches", json={"fridge_id": 1},
                    headers={"X-User-Id": str(emp["id"])})
    assert r.status_code == 403


def test_seeded_samples_present(client):
    # 停机样例
    fridges = client.get("/api/fridges").json()
    assert any(f["status"] == "shutdown" for f in fridges)
    # 无标签异常样例
    excs = client.get("/api/exceptions").json()
    assert any(e["type"] == "unlabeled" for e in excs)
    # 申诉样例（物业视角可见全部部门）
    prop = next(u for u in client.get("/api/users").json() if u["role"] == "property")
    appeals = client.get("/api/appeals", headers={"X-User-Id": str(prop["id"])}).json()
    assert any(a["status"] == "open" for a in appeals)
