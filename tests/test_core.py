import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from opsauto.app import create_app
from opsauto.notify import Notifier, NotifyError
from opsauto.models import Retry
from opsauto.pipeline import Pipeline, load_config
from opsauto.store import Store


def order(oid="O1", pay="card", price=50, bill="US", ship="US", email="a@acme.test"):
    a = lambda c: {"line1": "1 Main St", "city": "austin", "postal": "78 701", "country": c}  # noqa: E731
    return {"order_id": oid, "email": email, "payment_method": pay,
            "items": [{"sku": "S", "qty": 1, "price": price}], "billing": a(bill), "shipping": a(ship)}


@pytest.fixture
def env(tmp_path):
    state = {"fail": 0, "calls": 0, "got": []}

    def hook(req):
        state["calls"] += 1
        if state["fail"] > 0:
            state["fail"] -= 1
            return httpx.Response(503)
        state["got"].append(json.loads(req.content))
        return httpx.Response(200)

    cfg = load_config("config/rules.yaml")
    n = Notifier("http://h.test", cfg.retry, transport=httpx.MockTransport(hook), sleep=lambda s: None)
    p = Pipeline(Store(str(tmp_path / "t.db")), cfg, n, str(tmp_path / "e.csv"))
    return p, TestClient(create_app(p)), state


def test_flags_and_risk(env):
    p, c, _ = env
    r = c.post("/webhook/order", json=order(pay="cod", price=600, bill="US", ship="Nigeria")).json()
    assert {"HIGH_VALUE", "COD", "ADDR_MISMATCH", "REVIEW"} <= set(r["flags"])
    assert r["risk"] >= 70 and r["notified"]


def test_normalisation_and_csv(env, tmp_path):
    p, c, _ = env
    c.post("/webhook/order", json=order(bill="usa", ship="United States"))
    row = p.store.events()[0]
    assert row["country"] == "US" and "ADDR_MISMATCH" not in row["flags"]
    assert (tmp_path / "e.csv").read_text().count("\n") == 2
    assert c.get("/export.xlsx").status_code == 200


def test_idempotency(env):
    p, c, st = env
    a = c.post("/webhook/order", json=order(price=900), headers={"Idempotency-Key": "k1"}).json()
    b = c.post("/webhook/order", json=order(price=900), headers={"Idempotency-Key": "k1"}).json()
    assert b["duplicate"] and b["flags"] == a["flags"]
    assert len(st["got"]) == 1 and p.store.customer("a@acme.test")["orders"] == 1


def test_validation_rejected(env):
    _, c, _ = env
    assert c.post("/webhook/order", json={"order_id": "x", "email": "bad"}).status_code == 422
    assert c.post("/webhook/nope", json={}).status_code == 404


def test_vip_after_three_orders(env):
    _, c, _ = env
    for i in range(3):
        c.post("/webhook/order", json=order(oid=f"V{i}"))
    assert "VIP" in c.post("/webhook/order", json=order(oid="V9")).json()["flags"]


def test_retry_then_success(env):
    p, c, st = env
    st["fail"] = 2
    assert c.post("/webhook/order", json=order(price=900)).json()["notified"]
    assert st["calls"] == 3 and not p.store.dlq_open()


def test_dlq_and_replay(env):
    p, c, st = env
    st["fail"] = 99
    r = c.post("/webhook/order", json=order(price=900)).json()
    assert r["degraded"] == ["notify"] and st["calls"] == 4
    assert len(p.store.dlq_open()) == 1
    st["fail"] = 0
    assert p.replay() == {"resolved": 1, "failed": 0}
    assert len(st["got"]) == 1 and not p.store.dlq_open()


def test_process_failure_replays(env, monkeypatch):
    p, c, _ = env
    import opsauto.pipeline as pl
    real = pl.enrich
    monkeypatch.setattr(pl, "enrich", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert c.post("/webhook/order", json=order(oid="F1")).status_code == 202
    monkeypatch.setattr(pl, "enrich", real)
    assert p.replay()["resolved"] == 1
    assert p.store.events()[0]["status"] == "done"


def test_no_retry_on_4xx():
    n = Notifier("http://h.test", Retry(), transport=httpx.MockTransport(lambda r: httpx.Response(404)), sleep=lambda s: None)
    with pytest.raises(NotifyError) as e:
        n.send("x")
    assert e.value.attempts == 1


def test_signature_health_and_lead(env, monkeypatch):
    p, _, _ = env
    monkeypatch.setenv("OPS_WEBHOOK_SECRET", "s3")
    c = TestClient(create_app(p))
    body = json.dumps({"lead_id": "L1", "email": "ceo@acme.test"}).encode()
    assert c.post("/webhook/lead", content=body).status_code == 401
    sig = hmac.new(b"s3", body, hashlib.sha256).hexdigest()
    r = c.post("/webhook/lead", content=body, headers={"X-Signature": sig}).json()
    assert r["flags"] == ["B2B_LEAD"]
    assert c.get("/health").json()["status"] == "ok" and c.get("/").status_code == 200
