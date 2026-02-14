import time
from datetime import datetime, timezone

from sqlalchemy import text

from app.db import SessionLocal
from app.automations import run_scheduler_tick
from app.worker import queue, execute_run_job, enqueue_run_job


def loop(interval_s: int = 30):
    while True:
        db = SessionLocal()
        try:
            events = run_scheduler_tick(db, datetime.now(timezone.utc))
            for ev in events:
                if ev.get("status") == "enqueued" and ev.get("workflow_run_id"):
                    run_row = db.execute(
                        text("SELECT linked_run_id, tenant_id, created_by FROM workflow_runs WHERE id=:id"),
                        {"id": ev["workflow_run_id"]},
                    ).fetchone()
                    if run_row and run_row.linked_run_id:
                        enqueue_run_job(str(run_row.linked_run_id), str(run_row.tenant_id), str(run_row.created_by), job_id=f"tenant:{run_row.tenant_id}:run:{run_row.linked_run_id}")
            db.commit()
        except Exception as exc:
            db.rollback()
            try:
                db.execute(text("INSERT INTO audit_logs (tenant_id, event_type, payload) VALUES (:tenant_id, :event_type, CAST(:payload AS jsonb))"), {"tenant_id": "10000000-0000-0000-0000-000000000001", "event_type": "scheduler_tick_failed", "payload": __import__("json").dumps({"error": str(exc)[:200]})})
                db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()
        time.sleep(interval_s)


if __name__ == "__main__":
    loop()
