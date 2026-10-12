"""validate -> enrich -> rules -> SQLite -> CSV export -> notify, with DLQ at every stage."""
import csv
import json
import logging
import os
import sys

import yaml

from .engine import enrich, evaluate
from .models import MODELS, REF, Config
from .notify import Notifier, NotifyError
from .store import Store, now

COLS = ["created_at", "kind", "ref", "email", "total", "currency", "country", "payment", "risk", "flags"]


class JsonFormatter(logging.Formatter):
    def format(self, rec):
        d = {"ts": now(), "level": rec.levelname, "msg": rec.getMessage()}
        d.update(getattr(rec, "ctx", {}))
        return json.dumps(d, default=str)


def get_logger():
    log = logging.getLogger("ops")
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        log.addHandler(h)
        log.setLevel(os.getenv("OPS_LOG_LEVEL", "INFO"))
    return log


class Pipeline:
    def __init__(self, store: Store, cfg: Config, notifier: Notifier, export_csv: str):
        self.store, self.cfg, self.notifier, self.export_csv = store, cfg, notifier, export_csv
        self.log = get_logger()

    def _log(self, msg, **ctx):
        self.log.info(msg, extra={"ctx": ctx})

    # --- entry point ---------------------------------------------------------------------
    def handle(self, kind: str, raw: dict, key: str | None = None) -> dict:
        model = MODELS[kind].model_validate(raw)  # pydantic.ValidationError -> caller (422)
        key = key or f"{kind}:{getattr(model, REF[kind])}"
        dup = self.store.claim(key, kind, getattr(model, REF[kind]), raw)
        if dup:
            self.store.audit("webhook", "duplicate", key)
            self._log("duplicate_ignored", key=key)
            return {**json.loads(dup["result"] or "{}"), "idempotency_key": key,
                    "status": dup["status"], "duplicate": True}
        self.store.audit("webhook", "received", key, {"kind": kind})
        return self.execute(key, kind, model)

    def execute(self, key, kind, model) -> dict:
        try:
            ctx = enrich(kind, model, self.store, self.cfg)
            hits = evaluate(self.cfg, kind, ctx)
            flags = [h.flag for h in hits]
            risky = ctx.get("risk_score", 0) >= self.cfg.risk.review_threshold
            row = self.store.complete(key, ctx, flags, risky)
        except Exception as e:  # unexpected processing failure -> park for replay
            self.store.set_status(key, "failed")
            self.store.dlq_add(key, "process", {"kind": kind, "payload": model.model_dump()}, e)
            self.store.audit("pipeline", "process_failed", key, str(e))
            self._log("process_failed", key=key, error=str(e))
            return {"idempotency_key": key, "status": "failed", "queued_for_replay": True}
        degraded = []
        try:
            self.export(row)
        except Exception as e:
            degraded.append("export")
            self.store.dlq_add(key, "export", row, e)
        notified = False
        if any(h.notify for h in hits):
            text = self.message(row)
            try:
                attempts = self.notifier.send(text)
                notified = attempts > 0
                self.store.audit("pipeline", "notified", key, {"attempts": attempts})
            except NotifyError as e:
                degraded.append("notify")
                self.store.dlq_add(key, "notify", {"text": text}, e, e.attempts)
                self.store.audit("pipeline", "notify_dead_lettered", key, str(e))
        result = {"idempotency_key": key, "status": "done", "risk": ctx.get("risk_score"),
                  "flags": flags, "notified": notified, "degraded": degraded}
        self.store.set_status(key, "done", int(notified), result)
        self._log("processed", key=key, kind=kind, risk=ctx.get("risk_score"), flags=flags, degraded=degraded)
        return result

    # --- helpers -------------------------------------------------------------------------
    def export(self, row: dict):
        os.makedirs(os.path.dirname(os.path.abspath(self.export_csv)), exist_ok=True)
        new = not os.path.exists(self.export_csv)
        with open(self.export_csv, "a", newline="") as f:
            w = csv.DictWriter(f, COLS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    @staticmethod
    def message(row: dict) -> str:
        money = f" {row['total']:.2f} {row['currency']}" if row["total"] is not None else ""
        risk = f" | risk {row['risk']}" if row["risk"] is not None else ""
        return f"[{row['flags']}] {row['kind']} {row['ref']}{money}{risk} | {row['email']}"

    # --- replay --------------------------------------------------------------------------
    def replay(self, id: int | None = None) -> dict:
        out = {"resolved": 0, "failed": 0}
        for d in self.store.dlq_open(id):
            p = json.loads(d["payload"])
            try:
                if d["stage"] == "notify":
                    self.notifier.send(p["text"])
                    self.store.set_status(d["idem_key"], "done", 1)
                elif d["stage"] == "export":
                    self.export(p)
                else:  # process
                    res = self.execute(d["idem_key"], p["kind"], MODELS[p["kind"]].model_validate(p["payload"]))
                    if res["status"] != "done":
                        raise RuntimeError("still failing")
                self.store.dlq_resolve(d["id"], True)
                self.store.audit("replay", "resolved", d["idem_key"], {"dlq_id": d["id"], "stage": d["stage"]})
                out["resolved"] += 1
            except Exception as e:
                self.store.dlq_resolve(d["id"], False, str(e))
                out["failed"] += 1
        return out


def load_config(path: str) -> Config:
    with open(path) as f:
        return Config.model_validate(yaml.safe_load(f))


def from_env() -> Pipeline:
    cfg = load_config(os.getenv("OPS_RULES", "config/rules.yaml"))
    notifier = Notifier(os.getenv("OPS_WEBHOOK_URL", "http://127.0.0.1:9000/hook"), cfg.retry,
                        os.getenv("OPS_WEBHOOK_FORMAT", "slack"))
    return Pipeline(Store(os.getenv("OPS_DB", "data/ops.db")), cfg, notifier,
                    os.getenv("OPS_EXPORT_CSV", "data/export.csv"))
