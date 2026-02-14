import os
from sqlalchemy import create_engine, text

DB = os.getenv("DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/saas_ai")
engine = create_engine(DB, future=True)

QUERIES = {
    "ops_metrics_runs_24h": "SELECT status, count(*) FROM task_runs WHERE tenant_id=:tenant_id AND created_at >= now()-interval '24 hours' GROUP BY status",
    "ops_recent_runs": "SELECT id, status, created_at FROM task_runs WHERE tenant_id=:tenant_id ORDER BY created_at DESC LIMIT 30",
    "audit_list": "SELECT id, tool_name, status, started_at FROM tool_invocations WHERE tenant_id=:tenant_id ORDER BY started_at DESC LIMIT 200",
    "usage_summary": "SELECT day, api_requests_count FROM tenant_usage_daily WHERE tenant_id=:tenant_id ORDER BY day DESC LIMIT 7",
}

def main():
    tenant_id = "10000000-0000-0000-0000-000000000001"
    with engine.begin() as conn:
        for name, q in QUERIES.items():
            rows = conn.execute(text(f"EXPLAIN {q}"), {"tenant_id": tenant_id}).fetchall()
            print(f"\n--- {name} ---")
            for r in rows:
                print(r[0])

if __name__ == "__main__":
    main()
