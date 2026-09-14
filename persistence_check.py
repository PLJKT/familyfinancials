"""Wait for the Render free instance to spin down, then test whether data survived.

Render free web services stop after ~15 minutes with no incoming requests.
If the service is backed by SQLite on the default ephemeral filesystem, all
data written after the last deploy/restart is lost. If it is backed by
Postgres (render.yaml blueprint), the data survives.
"""
import json
import time
import urllib.request
import urllib.error

B = "https://familyfinancials.onrender.com"
MARK = open(r"C:\Users\HONOR\familyfinancials\.persist_marker.txt").read().strip()

IDLE_SECONDS = 17 * 60  # stay completely silent so the free instance spins down


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(B + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return -1, repr(e)


print(f"marker to look for: {MARK}", flush=True)
print(f"sleeping {IDLE_SECONDS}s with zero requests ...", flush=True)
time.sleep(IDLE_SECONDS)

t0 = time.time()
s, d = call("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
cold = time.time() - t0
print(f"first-request-after-idle: HTTP {s} in {cold:.1f}s", flush=True)
if s != 200:
    print("login failed:", d[:300], flush=True)
    raise SystemExit(1)
tok = json.loads(d)["access_token"]

# Was the marker transaction still there?
s, d = call("GET", "/api/transactions?limit=5000", token=tok)
tx = json.loads(d)
found = [t for t in tx if t.get("description") == MARK]
print(f"transactions now: {len(tx)}  marker found: {len(found)}", flush=True)

# Was the family account still there?
s, d = call("GET", "/api/users", token=tok)
users = json.loads(d)
print("users:", [(u["id"], u["username"], u["created_at"]) for u in users], flush=True)

if found:
    s, _ = call("DELETE", f"/api/transactions/{found[0]['id']}", token=tok)
    print(f"cleanup: deleted marker transaction id={found[0]['id']} -> HTTP {s}", flush=True)
else:
    print("cleanup: marker already gone (data was wiped)", flush=True)

print("VERDICT:", "DATA SURVIVES RESTARTS (external database)" if found
      else "DATA IS LOST ON EVERY SPIN-DOWN / RESTART (ephemeral SQLite)", flush=True)
