"""Acceptance criterion 5, proven over real HTTP (not the test client).

Boots the service twice:
  pass A — FREEZE_ENABLED=true : reads stay 200, every write method answers 503
  pass B — FREEZE_ENABLED=false: a write answers 201 again
Everything is appended to evidence/freeze_run.log.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "evidence" / "freeze_run.log"
PORT = 18080
BASE = f"http://127.0.0.1:{PORT}"
NOTICE = "Cutover freeze in progress: write operations resume once the migration window closes."


def log(msg: str) -> None:
    print(msg, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def wait_ready(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("service did not start")


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


def boot(freeze: bool) -> subprocess.Popen:
    env = dict(os.environ)
    env["FREEZE_ENABLED"] = "true" if freeze else "false"
    env["FREEZE_MESSAGE"] = NOTICE
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wait_ready()
    return proc


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    log(f"started_at: {datetime.now(timezone.utc).isoformat()}")

    failures: list[str] = []

    log("\n=== PASS A — freeze ON (FREEZE_ENABLED=true) ===")
    proc = boot(True)
    try:
        code, body = call("GET", "/health")
        log(f"GET  /health                -> {code} {body}")
        if code != 200 or body.get("freeze_enabled") is not True:
            failures.append("health did not report freeze state")

        for path in ("/api/v1/companies", "/api/v1/contacts", "/api/v1/freeze/status"):
            code, body = call("GET", path)
            sample = {k: body[k] for k in list(body)[:2]}
            log(f"GET  {path:<23} -> {code} {sample}")
            if code != 200:
                failures.append(f"{path} should stay readable during freeze")

        payload = {
            "name": "Fictional Cutover Contact Co",
            "org_id": "11111111-1111-1111-1111-111111111111",
            "site_id": "22222222-2222-2222-2222-222222222222",
        }
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            code, body = call(method, "/api/v1/companies", payload if method != "DELETE" else None)
            log(f"{method:<4} /api/v1/companies        -> {code} {body.get('detail')} | {body.get('message')}")
            if code != 503:
                failures.append(f"{method} not blocked during freeze")
            if body.get("detail") != "service_temporarily_frozen":
                failures.append(f"{method} missing freeze detail code")
            if body.get("message") != NOTICE:
                failures.append(f"{method} missing maintenance notice text")
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        time.sleep(0.5)

    log("\n=== PASS B — freeze OFF (FREEZE_ENABLED=false) ===")
    proc = boot(False)
    try:
        code, body = call("GET", "/health")
        log(f"GET  /health                -> {code} {body}")
        code, body = call("POST", "/api/v1/companies", {
            "name": "Fictional Post Freeze Co",
            "org_id": "33333333-3333-3333-3333-333333333333",
            "site_id": "44444444-4444-4444-4444-444444444444",
        })
        log(f"POST /api/v1/companies        -> {code} {body}")
        if code != 201:
            failures.append("writes did not resume after the freeze was lifted")
    finally:
        proc.terminate()
        proc.wait(timeout=15)

    log(f"\nfinished_at: {datetime.now(timezone.utc).isoformat()}")
    if failures:
        log("RESULT: FAIL — " + "; ".join(sorted(set(failures))))
        return 1
    log("RESULT: PASS — reads served 200 throughout, all four write methods answered "
        "503 with the maintenance notice, writes resumed after the flag was cleared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
