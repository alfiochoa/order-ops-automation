"""Synthetic traffic simulator (all data is fake).
HTTP mode:       python -m tools.simulate --url http://127.0.0.1:8000 -n 200
In-process mode: python -m tools.simulate --inprocess -n 300   (flaky webhook, then DLQ replay)"""
import argparse
import json
import random
import tempfile
import os

import httpx

COUNTRIES = ["US", "usa", "United States", "UK", "Spain", "españa", "DE", "Germany", "FR", "MX", "NG", "Italy"]
DOMAINS = ["gmail.com", "yahoo.com", "acme-shop.com", "northwind.test", "example.org"]


def addr(rng, country):
    return {"line1": f"  {rng.randint(1, 99)}  Main  St ", "city": rng.choice(["madrid", "BERLIN", "austin"]),
            "postal": f"{rng.randint(10000, 99999)}", "country": country}


def make(rng, i, customers):
    email = rng.choice(customers)
    kind = rng.choices(["order", "lead", "support"], [70, 15, 15])[0]
    if kind == "order":
        c = rng.choice(COUNTRIES)
        ship = c if rng.random() > 0.15 else rng.choice(COUNTRIES)
        return kind, {"order_id": f"O{i}", "email": email, "currency": "EUR",
                      "payment_method": rng.choices(["card", "paypal", "cod", "bank_transfer"], [55, 20, 20, 5])[0],
                      "items": [{"sku": f"SKU{rng.randint(1, 30)}", "qty": rng.randint(1, 4),
                                 "price": round(rng.choice([9.9, 25, 49, 120, 260]), 2)} for _ in range(rng.randint(1, 3))],
                      "billing": addr(rng, c), "shipping": addr(rng, ship)}
    if kind == "lead":
        return kind, {"lead_id": f"L{i}", "email": email, "message": "Interested in wholesale pricing"}
    subj = rng.choice(["Where is my parcel?", "Urgent: refund please", "Item arrived broken", "Change address"])
    return kind, {"ticket_id": f"T{i}", "email": email, "subject": subj}


def drive(post, n, seed):
    rng = random.Random(seed)
    customers = [f"user{k}@{rng.choice(DOMAINS)}" for k in range(max(10, n // 8))]
    sent, tally = [], {"ok": 0, "duplicate": 0, "rejected": 0, "failed": 0}
    for i in range(n):
        if sent and rng.random() < 0.10:  # client retry: same idempotency key + body
            kind, body, key = rng.choice(sent)
        elif rng.random() < 0.05:  # malformed
            kind, body, key = "order", {"order_id": f"BAD{i}", "email": "nope", "items": []}, None
        else:
            kind, body = make(rng, i, customers)
            key = f"sim-{kind}-{i}"
            sent.append((kind, body, key))
        r = post(kind, body, key)
        j = r.json()
        tally["rejected" if r.status_code == 422 else "duplicate" if j.get("duplicate")
              else "failed" if j.get("status") == "failed" else "ok"] += 1
    return tally


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("-n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--inprocess", action="store_true")
    a = ap.parse_args()
    if not a.inprocess:
        c = httpx.Client(base_url=a.url, timeout=30)
        print(json.dumps(drive(lambda k, b, key: c.post(f"/webhook/{k}", json=b, headers={"Idempotency-Key": key} if key else {}), a.n, a.seed)))
        return
    from fastapi.testclient import TestClient
    from opsauto.app import create_app
    from opsauto.notify import Notifier
    from opsauto.pipeline import Pipeline, load_config
    from opsauto.store import Store

    rng, healthy = random.Random(a.seed), [False]
    delivered = []

    def hook(req):
        if not healthy[0] and rng.random() < 0.6:
            return httpx.Response(503)
        delivered.append(1)
        return httpx.Response(200)

    cfg = load_config("config/rules.yaml")
    d = tempfile.mkdtemp()
    p = Pipeline(Store(os.path.join(d, "ops.db")), cfg,
                 Notifier("http://hook.test", cfg.retry, transport=httpx.MockTransport(hook), sleep=lambda s: None),
                 os.path.join(d, "export.csv"))
    tc = TestClient(create_app(p))
    tally = drive(lambda k, b, key: tc.post(f"/webhook/{k}", json=b, headers={"Idempotency-Key": key} if key else {}), a.n, a.seed)
    s = p.store.stats()
    print("traffic     ", json.dumps(tally))
    print("events      ", s["total"], "by_kind", s["by_kind"])
    print("flags       ", s["flags"], "avg_risk", s["avg_risk"])
    print("notified    ", s["notified"], "| dead-lettered:", s["dlq_open"], "(webhook 60% flaky, 4 attempts each)")
    healthy[0] = True
    print("replay      ", p.replay(), "| dlq_open now:", p.store.stats()["dlq_open"])


if __name__ == "__main__":
    main()
