"""Smoke test: python test_api.py   (needs the data files in ./data)"""
import os
os.environ.setdefault("TIRA_DATA", "data")
from fastapi.testclient import TestClient
import app

c = TestClient(app.app)
def ok(u, code=200):
    r = c.get(u); assert r.status_code == code, (u, r.status_code, r.text[:200]); return r.json()

print("health", ok("/api/health"))
m = ok("/api/meta"); assert m["limits"] and m["field_notes"]
sites = ok("/api/sites"); assert len(sites) >= 5
s = next(x for x in sites if not x["terrain_only"])
mm = ok(f"/api/matches?mars={s['mars_region_id']}&only_ranked=true&limit=3")      # mission is optional
assert mm["results"] and "flags" in mm["results"][0]
one = ok(f"/api/matches?mars={s['mars_region_id']}&mission={s['missions'][0]}&limit=2")
d = ok(f"/api/matches/{one['results'][0]['earth_region_id']}?mars={s['mars_region_id']}")
assert d["match"] and d["all_missions"]
ok(f"/api/evidence/{s['mars_region_id']}")
ok("/api/analogs")
ok("/api/matches?mars=nope", 404)

# --- Phase 2 thermal (optional files; exploratory, never part of the ranking) ---
th = ok("/api/thermal"); assert th["limits"] and ok("/api/meta")["thermal"]["limits"]
if th["available"]:
    assert th["count"] > 0 and all("exploratory_only" in r["flags"] for r in th["results"])
    rid = th["results"][0]["region_id"]
    d = ok(f"/api/thermal/{rid}"); assert d["features"] and d["limits"]
    ok("/api/thermal?planet=mars"); ok("/api/thermal?planet=moon", 422)
    print("thermal", th["count"], "regions")
else:
    ok("/api/thermal/nope", 404); print("thermal files not loaded (optional)")
print("ALL OK")
