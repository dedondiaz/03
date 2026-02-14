import os
from redis import Redis
from rq import Queue, Worker, Retry
from .db import SessionLocal
from .runtime import execute_run

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_conn = Redis.from_url(redis_url)
queue = Queue("runs", connection=redis_conn, default_timeout=600)
dlq = Queue("runs_dlq", connection=redis_conn)


def enqueue_run_job(run_id: str, tenant_id: str, user_id: str, job_id: str):
    return queue.enqueue(execute_run_job, run_id, tenant_id, user_id, job_id=job_id, retry=Retry(max=3, interval=[5, 15, 30]), failure_ttl=7*24*3600)


def execute_run_job(run_id: str, tenant_id: str, user_id: str):
    db = SessionLocal()
    try:
        execute_run(db, run_id, tenant_id, user_id)
    finally:
        db.close()


def start_worker():
    worker = Worker([queue, dlq], connection=redis_conn)
    worker.work()


if __name__ == "__main__":
    start_worker()
