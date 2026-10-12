"""Self-seeding synthetic demo: in-process pipeline + a flaky *mock* webhook. No network, no secrets."""
import os
import random
import tempfile

import httpx
from pydantic import ValidationError

from .notify import Notifier
from .pipeline import Pipeline, load_config
from .store import Store


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


class Demo:
    """Pipeline whose notification webhook fails ~60% of calls until `recover()` is called."""

    def __init__(self, rules="config/rules.yaml", seed=7):
        self.rng, self.healthy, self.delivered = random.Random(seed), False, 0
        cfg = load_config(rules)
        d = tempfile.mkdtemp(prefix="opsdemo-")
        self.pipeline = Pipeline(Store(os.path.join(d, "ops.db")), cfg,
                                 Notifier("http://hook.test", cfg.retry, transport=httpx.MockTransport(self._hook),
                                          sleep=lambda s: None), os.path.join(d, "export.csv"))
        self.pipeline.log.setLevel("WARNING")

    def _hook(self, req):
        if not self.healthy and self.rng.random() < 0.6:
            return httpx.Response(503)
        self.delivered += 1
        return httpx.Response(200)

    def post(self, kind, body, key=None):
        try:
            res = self.pipeline.handle(kind, body, key)
            return _Resp(200 if res["status"] == "done" else 202, res)
        except ValidationError:
            self.pipeline.store.audit("webhook", "rejected", "", {"kind": kind})
            return _Resp(422, {"status": "rejected"})

    def seed(self, n=200, seed=7):
        from tools.simulate import drive
        return drive(self.post, n, seed)

    def recover(self):
        self.healthy = True
        return self.pipeline.replay()
