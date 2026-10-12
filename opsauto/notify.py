"""Slack/Discord-compatible webhook notifier with exponential backoff + jitter."""
import random
import time

import httpx

from .models import Retry


class NotifyError(Exception):
    def __init__(self, msg, attempts):
        super().__init__(msg)
        self.attempts = attempts


class Notifier:
    def __init__(self, url: str, retry: Retry, fmt="slack", transport=None, sleep=time.sleep):
        self.url, self.retry, self.fmt, self.sleep = url, retry, fmt, sleep
        self.client = httpx.Client(transport=transport, timeout=5)

    def send(self, text: str) -> int:
        """Returns attempts used. Retries network errors, 429 and 5xx; other 4xx fail fast."""
        if not self.url:
            return 0
        body = {"content": text} if self.fmt == "discord" else {"text": text}
        r, err = self.retry, "unknown"
        for attempt in range(1, r.max_attempts + 1):
            try:
                resp = self.client.post(self.url, json=body)
                if resp.status_code < 300:
                    return attempt
                err = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    raise NotifyError(err, attempt)
            except httpx.HTTPError as e:
                err = f"{type(e).__name__}: {e}"
            if attempt < r.max_attempts:
                delay = min(r.max_delay, r.base_delay * r.factor ** (attempt - 1))
                self.sleep(delay * random.uniform(0.8, 1.2))
        raise NotifyError(err, r.max_attempts)
