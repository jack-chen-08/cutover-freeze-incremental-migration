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
PY = sys.executable
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


def real_org_and_site() -> tuple[str, str]:
    """Writes need foreign keys that actually exist in the drill database."""
    import psycopg

    from app.config import settings

    dsn = settings.source_dsn.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT o.id, s.id FROM orgs o CROSS JOIN sites s ORDER BY o.id, s.id LIMIT 1")
        row = cur.fetchone()
    if row is None:
        raise SystemExit("drill database has no org/site rows — run scripts/prepare_drill.py --phase seed --truncate")
    return str(row[0]), str(row[1])


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    log(f"started_at: {datetime.now(timezone.utc).isoformat()}")

    failures: list[str] = []

    org_id, site_id = real_org_and_site()
    freeze_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rec = subprocess.run([PY, "-m", "scripts.cutover_incremental", "--record-freeze-at", freeze_at],
                         cwd=ROOT, capture_output=True, text=True)
    log(f"\nfreeze point: {freeze_at}  (recorded by cutover_incremental: {rec.stdout.strip() or rec.stderr.strip()})")
    if rec.returncode != 0:
        failures.append("freeze_at could not be recorded")

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
            "org_id": org_id,
            "site_id": site_id,
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
            "org_id": org_id,
            "site_id": site_id,
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
