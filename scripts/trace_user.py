"""Rebuild one user's journey from the JSON logs alone.
Usage: python scripts/trace_user.py <user_id> [logs/app.jsonl]"""
import json
import sys

# Windows consoles may default to a legacy code page that cannot print ₹ or other
# characters present in the JSONL log. Keep the trace readable instead of crashing.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

user = sys.argv[1]
path = sys.argv[2] if len(sys.argv) > 2 else "logs/app.jsonl"
lines = []
with open(path, encoding="utf-8") as f:
    for raw in f:
        try:
            lines.append(json.loads(raw))
        except ValueError:
            pass
rids = {l["request_id"] for l in lines if l.get("user_id") == user and l.get("request_id")}
keep = [l for l in lines if l.get("user_id") == user or (l.get("request_id") in rids and not l.get("user_id"))]
SKIP = {"ts", "level", "event", "request_id", "user_id"}
print(f"=== journey for {user}: {len(keep)} events ===")
for l in keep:
    extra = " ".join(f"{k}={v}" for k, v in l.items() if k not in SKIP and v is not None)
    print(f"{l['ts']}  {l.get('request_id') or '-':<16} {l['event']:<30} {extra}")
