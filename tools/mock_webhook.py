"""Mock Slack/Discord webhook receiver. FAIL_RATE=0.3 makes it flaky (503) to exercise retries.
Run: python -m tools.mock_webhook  (port 9000)"""
import os
import random

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
received: list[dict] = []


@app.post("/hook")
async def hook(req: Request):
    if random.random() < float(os.getenv("FAIL_RATE", "0")):
        return JSONResponse({"error": "flaky"}, 503)
    received.append(await req.json())
    return {"ok": True}


@app.get("/received")
def get_received():
    return {"count": len(received), "last": received[-5:]}


if __name__ == "__main__":
    uvicorn.run(app, port=9000, log_level="warning")
