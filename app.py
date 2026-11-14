"""Tira Nova backend: serves the team's output files through a small read-only JSON API.

Data (put these files in TIRA_DATA, default ./data):
  required: terrain_emit_joined.csv, tira_nova_scan_catalog.csv
  optional: tira_nova_analysis_examples_scan.json, crism_mars_targets.csv, crism_features.csv, tira_nova_known_analogs.csv,
            themis_features.csv, earth_thermal_features.csv   (Phase 2 thermal, exploratory)

Endpoints: /api/health /api/meta /api/sites /api/matches /api/matches/{earth_region_id} /api/evidence/{mars_region_id} /api/analogs
           /api/thermal /api/thermal/{region_id}  POST /api/reload
Run:  pip install -r requirements.txt  &&  uvicorn app:app --port 8000
Docs: http://localhost:8000/docs
"""
import json, os, re
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

DATA = Path(os.environ.get("TIRA_DATA", "data"))
REQUIRED = ["terrain_emit_joined.csv", "tira_nova_scan_catalog.csv"]
THERMAL_FILES = ["themis_features.csv", "earth_thermal_features.csv"]
THERMAL_COLS = ["region_id", "planet", "dataset", "resolution_m", "feature_name", "value", "coverage_pct",
                "source_date", "status"]
OPTIONAL = ["tira_nova_analysis_examples_scan.json", "crism_mars_targets.csv", "crism_features.csv",
            "tira_nova_known_analogs.csv"] + THERMAL_FILES

# Shown with every result: the limits of the method (keep in sync with the team document).
LIMITS = [
    "Preliminary exploratory comparison. Similarity is not equivalence.",
    "Terrain comparison is regional (~450 m, 50.9 km windows), not rover-scale.",
    "EMIT mineral data covers only the Earth windows that were processed; the others are 'no_emit_data'.",
    "CRISM observations cover about 5-13% of each Mars site window and come from targeted (mineral-rich) areas.",
    "CRISM detection rule is a heuristic chosen from our own statistics, not a published threshold.",
    "Earth cannot reproduce Martian gravity, atmosphere, radiation or temperature.",
]

# Phase 2 thermal (exploratory). Shown with every thermal result; NEVER merged into the match ranking.
THERMAL_LIMITS = [
    "Exploratory only: no similarity claim, and not part of the final ranking.",
    "Diurnal range (day minus night surface temperature) is a simple proxy. It is NOT thermal inertia.",
    "Absolute values are not comparable between Mars and Earth (the thin Martian atmosphere gives a much larger range). "
    "Compare ranks or normalized values within one planet only.",
    "Mars = THEMIS brightness temperature (Band 9, no emissivity or atmospheric correction, 100 m). "
    "Earth = MODIS Aqua land surface temperature (1 km). Different quantities and resolutions, each averaged over a 21 km window.",
    "Each Mars site uses one day scene and one night scene from different dates (2002), with no dispersion estimate. "
    "Season (Ls) and local time are only approximately matched and are recorded in the features.",
    "Window size, coverage threshold (50%) and the Ls-gap threshold (10 deg) are provisional choices.",
]

THERMAL_FIELD_NOTES = {
    "diurnal_range_K": "Mean day temperature minus mean night temperature over the window. Proxy only, NOT thermal inertia.",
    "range_rank_in_planet": "Rank by diurnal_range_K inside the same planet among status 'ok' regions (1 = largest range). "
                            "Not comparable across planets. Exploratory.",
    "coverage_day_pct / coverage_night_pct": "Share of valid pixels in the window for the day and the night observation.",
    "status": "ok | low_coverage | ls_gap | no_coverage | window_outside_tile.",
    "local_solar_time_day_h / local_solar_time_night_h": "Mars local solar time of the two scenes (hours).",
    "local_view_time_day_h / local_view_time_night_h": "Earth MODIS Aqua mean view time (hours).",
    "ls_day_deg / ls_night_deg / ls_gap_deg": "Mars season (solar longitude) of the two scenes and their gap.",
    "earth_solar_longitude_deg_approx": "Approximate Earth solar longitude of the MODIS period (simple approximation).",
    "diurnal_range_strictQC_K": "Earth sensitivity check using only the strictest MODIS quality flag.",
    "source_date": "Observation dates (Mars) or the composite period (Earth).",
}

STATE = {}


def _clean(df: pd.DataFrame):
    """DataFrame -> list of JSON-safe dicts (NaN -> None)."""
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _site_name(rid: str) -> str:
    return rid.split("_lat")[0]


def _latlon(rid: str):
    m = re.search(r"_lat(-?[\d.]+)_lon(-?[\d.]+)$", rid)
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


REQUIRED_COLS = ["mars_region_id", "mission", "earth_region_id", "rank", "distance", "discrimination",
                 "emit_status", "final_rank", "emit_hits", "emit_n_targets", "combined_score"]

FIELD_NOTES = {
    "rank": "Terrain rank among all Earth windows for this Mars site and mission (1 = closest terrain).",
    "distance": "Terrain distance (smaller = more similar); distance_vs_good = distance / the 'good' distance.",
    "discrimination": "non_discriminating = a large share of Earth windows is inside the good distance, so the terrain rank separates candidates weakly.",
    "emit_hits": "Number of independent CRISM mineral signals (not minerals) found in the Earth window by EMIT.",
    "emit_n_targets": "Number of independent CRISM signals for the Mars site. Minerals sharing CRISM parameters count once.",
    "emit_n_minerals": "Number of target minerals behind those signals.",
    "emit_status": "ok | low_coverage | no_target_mineral | no_emit_data | terrain_only. Only 'ok' rows get a final_rank.",
    "final_rank": "Rank among EMIT-covered windows only (combined terrain + mineral score). Not a global rank.",
    "combined_score": "0.5*(1-distance_vs_good) + 0.5*(emit_hits/emit_n_targets). A coarse score when emit_n_targets is 1 or 2.",
}


def _flags(r: dict):
    """Short caution flags for the front-end to show next to a result."""
    f = []
    if r.get("discrimination") == "non_discriminating":
        f.append("non_discriminating_terrain")
    st = r.get("emit_status")
    if st in ("no_emit_data", "low_coverage", "no_target_mineral", "terrain_only"):
        f.append(f"minerals_{st}")
    if st == "ok" and (r.get("emit_n_targets") or 0) <= 1:
        f.append("single_mineral_signal")
    if r.get("emit_target_source") == "provisional":
        f.append("provisional_targets")
    if r.get("pct_unverified_source") not in (None, 0, 0.0):
        f.append("unverified_source_pixels")
    return f


def _rows(df):
    out = _clean(df)
    for r in out:
        r["flags"] = _flags(r)
    return out


def _num(x):
    """NaN/None -> None, numpy scalars -> plain Python (JSON-safe)."""
    if x is None or (not isinstance(x, str) and pd.isna(x)):
        return None
    return x.item() if hasattr(x, "item") else x


def _thermal_flags(rec: dict):
    f = ["exploratory_only"]
    if rec["status"] != "ok":
        f.append(f"thermal_{rec['status']}")
    if rec["planet"] == "mars":
        f.append("single_scene_pair")
    return f


def load_thermal(joined: pd.DataFrame):
    """Read the optional Phase 2 thermal files -> {region_id: record}. Returns {} when neither file exists."""
    frames = []
    for name in THERMAL_FILES:
        p = DATA / name
        if not p.exists():
            continue
        d = pd.read_csv(p)
        bad = [c for c in THERMAL_COLS if c not in d.columns]
        if bad:
            raise RuntimeError(f"{name} is missing columns {bad}; expected {THERMAL_COLS}")
        frames.append(d)
    if not frames:
        return {}
    t = pd.concat(frames, ignore_index=True)
    analog = {}
    if "known_analog" in joined:
        k = joined[joined.known_analog.notna()]
        analog = dict(zip(k.earth_region_id, k.known_analog))
    recs = {}
    for rid, g in t.groupby("region_id", sort=False):
        first = g.iloc[0]
        lat, lon = _latlon(rid)
        recs[rid] = {"region_id": rid, "planet": str(first.planet), "name": _site_name(rid), "lat": lat, "lon": lon,
                     "known_analog": analog.get(rid), "dataset": first.dataset, "resolution_m": _num(first.resolution_m),
                     "source_date": first.source_date, "status": str(first.status),
                     "features": {r.feature_name: {"value": _num(r.value), "coverage_pct": _num(r.coverage_pct)}
                                  for r in g.itertuples()}}
    # rank by diurnal range inside each planet (status ok only); ranks never cross planets
    rows = [(r["planet"], rid, r["features"].get("diurnal_range_K", {}).get("value")) for rid, r in recs.items()
            if r["status"] == "ok"]
    rk = pd.DataFrame(rows, columns=["planet", "rid", "v"]).dropna()
    if len(rk):
        rk["rank"] = rk.groupby("planet").v.rank(ascending=False, method="min").astype(int)
        n = rk.groupby("planet").size().to_dict()
        for _, r in rk.iterrows():
            recs[r.rid]["range_rank_in_planet"] = int(r["rank"]); recs[r.rid]["n_ranked_in_planet"] = int(n[r.planet])
    for rec in recs.values():
        rec.setdefault("range_rank_in_planet", None); rec.setdefault("n_ranked_in_planet", None)
        rec["flags"] = _thermal_flags(rec)
    return recs


def load():
    missing = [f for f in REQUIRED if not (DATA / f).exists()]
    if missing:
        raise RuntimeError(f"missing required files in {DATA.resolve()}: {missing}")
    joined = pd.read_csv(DATA / "terrain_emit_joined.csv", low_memory=False)
    bad = [c for c in REQUIRED_COLS if c not in joined.columns]
    if bad:
        raise RuntimeError(f"terrain_emit_joined.csv is the old schema, missing columns: {bad}. Re-run EMIT cells 14c, 15, 16.")
    cat = pd.read_csv(DATA / "tira_nova_scan_catalog.csv")
    cat = cat[cat.planet == "Earth"].drop_duplicates("region_id").set_index("region_id")
    # attach the map box of each Earth window
    joined = joined.join(cat[["bbox_w", "bbox_s", "bbox_e", "bbox_n"]], on="earth_region_id")
    engine = {}
    p = DATA / "tira_nova_analysis_examples_scan.json"
    if p.exists():
        for r in json.load(open(p)):
            engine[(r["mars_region"]["region_id"], r["mission"]["id"])] = r
    def opt_csv(name):
        q = DATA / name
        return pd.read_csv(q, low_memory=False) if q.exists() else pd.DataFrame()
    thermal = load_thermal(joined)          # read before STATE.update so a bad file keeps the old data
    STATE.update(joined=joined, engine=engine, targets=opt_csv("crism_mars_targets.csv"),
                 crism=opt_csv("crism_features.csv"), analogs=opt_csv("tira_nova_known_analogs.csv"),
                 thermal=thermal, missing_optional=[f for f in OPTIONAL if not (DATA / f).exists()])


app = FastAPI(title="Tira Nova API", version="0.3")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])
load()


@app.get("/api/health")
def health():
    return {"status": "ok", "rows": int(len(STATE["joined"]))}


@app.post("/api/reload")
def reload_data():
    """Re-read the files after you replace them (no restart needed)."""
    load()
    return {"status": "reloaded", "rows": int(len(STATE["joined"]))}


@app.get("/api/meta")
def meta():
    j = STATE["joined"]
    return {"limits": LIMITS,
            "missing_optional_files": STATE["missing_optional"],
            "rows": int(len(j)),
            "emit_status_counts": j.emit_status.value_counts().to_dict() if "emit_status" in j else {},
            "target_source_counts": j.emit_target_source.value_counts().to_dict() if "emit_target_source" in j else {},
            "engine_responses": len(STATE["engine"]),
            "field_notes": FIELD_NOTES,
            "thermal": {"available": bool(STATE["thermal"]), "regions": len(STATE["thermal"]),
                        "by_planet": pd.Series([r["planet"] for r in STATE["thermal"].values()], dtype=object)
                                       .value_counts().to_dict(),
                        "limits": THERMAL_LIMITS, "field_notes": THERMAL_FIELD_NOTES}}


@app.get("/api/sites")
def sites():
    j = STATE["joined"]; t = STATE["targets"]
    out = []
    for rid, g in j.groupby("mars_region_id"):
        lat, lon = _latlon(rid)
        mins = sorted(t[t.mars_region_id == rid].emit_mineral.unique()) if len(t) else []
        out.append({"mars_region_id": rid, "name": _site_name(rid), "lat": lat, "lon": lon,
                    "missions": sorted(g.mission.unique()),
                    "terrain_only": bool((g.emit_status == "terrain_only").all()),
                    "ranked_matches": int(g.final_rank.notna().sum()),
                    "crism_target_minerals": mins,
                    "thermal_available": rid in STATE["thermal"]})
    return out


@app.get("/api/matches")
def matches(mars: str, mission: Optional[str] = None, only_ranked: bool = False,
            limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    j = STATE["joined"]
    g = j[j.mars_region_id == mars]
    if mission:
        g = g[g.mission == mission]
    if g.empty:
        raise HTTPException(404, f"no rows for mars={mars!r}, mission={mission!r}")
    if only_ranked:
        g = g[g.final_rank.notna()]
    g = g.sort_values(["mission", "final_rank", "rank"], na_position="last")   # per mission: EMIT-ranked first, then terrain order
    total = int(len(g))
    return {"mars_region_id": mars, "mission": mission, "total": total, "limit": limit, "offset": offset,
            "limits": LIMITS, "results": _rows(g.iloc[offset:offset + limit])}


@app.get("/api/matches/{earth_region_id}")
def match_detail(earth_region_id: str, mars: str, mission: Optional[str] = None):
    j = STATE["joined"]
    g = j[(j.mars_region_id == mars) & (j.earth_region_id == earth_region_id)]
    if mission:
        g = g[g.mission == mission]
    if g.empty:
        raise HTTPException(404, "match not found")
    g = g.sort_values("rank")
    allm = _rows(g)
    row = allm[0]                                    # best (lowest terrain rank) when no mission is given
    mission = row["mission"]
    detail = None
    resp = STATE["engine"].get((mars, mission))
    if resp:                                          # factors / confidence exist only for the engine's top-10
        detail = next((r for r in resp["results"] if r["region_id"] == earth_region_id), None)
    return {"match": row, "all_missions": allm, "engine_detail": detail,
            "engine_warnings": resp["global_warnings"] if resp else [], "limits": LIMITS}


@app.get("/api/evidence/{mars_region_id}")
def evidence(mars_region_id: str):
    t, c = STATE["targets"], STATE["crism"]
    tg = _clean(t[t.mars_region_id == mars_region_id]) if len(t) else []
    feats, cov, dates = [], None, None
    if len(c):
        s = c[c.region_id == mars_region_id]
        if len(s):
            cov, dates = float(s.coverage_pct.max()), s.source_date.iloc[0]
            d = s[s.feature_name.str.endswith("_detect_pct")].sort_values("value", ascending=False).head(10)
            feats = _clean(d[["feature_name", "value"]])
    return {"mars_region_id": mars_region_id, "crism_targets": tg, "top_detect_features": feats,
            "crism_coverage_pct": cov, "crism_dates": dates,
            "note": "CRISM covers only part of the site window; detection rule is a heuristic." }


_THERMAL_SUMMARY = ["mean_day_temp_K", "mean_night_temp_K", "diurnal_range_K"]


@app.get("/api/thermal")
def thermal_list(planet: Optional[str] = None):
    """Phase 2 thermal summary per region (exploratory; independent of the match ranking)."""
    if planet is not None and planet.lower() not in ("mars", "earth"):
        raise HTTPException(422, "planet must be 'mars' or 'earth'")
    recs = [r for r in STATE["thermal"].values() if planet is None or r["planet"] == planet.lower()]
    results = []
    for r in recs:
        f = r["features"]
        cov = [f[k]["coverage_pct"] for k in ("coverage_day_pct", "coverage_night_pct")
               if k in f and f[k]["value"] is not None]
        results.append({"region_id": r["region_id"], "planet": r["planet"], "name": r["name"],
                        "known_analog": r["known_analog"], "dataset": r["dataset"], "status": r["status"],
                        **{k: f.get(k, {}).get("value") for k in _THERMAL_SUMMARY},
                        "min_coverage_pct": min(cov) if cov else None,
                        "range_rank_in_planet": r["range_rank_in_planet"],
                        "n_ranked_in_planet": r["n_ranked_in_planet"], "flags": r["flags"]})
    return {"available": bool(STATE["thermal"]), "count": len(results), "limits": THERMAL_LIMITS, "results": results}


@app.get("/api/thermal/{region_id}")
def thermal_detail(region_id: str):
    """All thermal features of one Mars (mars_region_id) or Earth (earth_region_id) region."""
    r = STATE["thermal"].get(region_id)
    if r is None:
        raise HTTPException(404, f"no thermal data for {region_id!r}" +
                            ("" if STATE["thermal"] else " (thermal files are not loaded)"))
    return {**r, "limits": THERMAL_LIMITS}


@app.get("/api/analogs")
def analogs():
    """Known analog sites. Uses tira_nova_known_analogs.csv if present, else derives them from the joined file."""
    if len(STATE["analogs"]):
        return _clean(STATE["analogs"])
    j = STATE["joined"]
    if "known_analog" not in j:
        return []
    k = j[j.known_analog.notna()]
    out = []
    for name, g in k.groupby("known_analog"):
        out.append({"known_analog": name, "windows": int(g.earth_region_id.nunique()),
                    "mars_sites": sorted(g.mars_region_id.unique()),
                    "earth_region_ids": sorted(g.earth_region_id.unique())})
    return out
