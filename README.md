# Tira Nova API (read-only)

JSON API over the team's output files. No database, no auth, CORS open (`*`). Interactive docs at `/docs`.

## Data files (put in `data/`, or set `TIRA_DATA`)
| File | Required | From |
|---|---|---|
| `terrain_emit_joined.csv` | yes | EMIT notebook, Cell 16 (new schema: `emit_status`, `final_rank`, `emit_n_minerals`) |
| `tira_nova_scan_catalog.csv` | yes | Terrain notebook |
| `crism_mars_targets.csv` | optional | CRISM notebook (evidence endpoint) |
| `crism_features.csv` | optional | CRISM notebook |
| `tira_nova_analysis_examples_scan.json` | optional | Terrain notebook (engine detail for the top-10) |
| `tira_nova_known_analogs.csv` | optional | Terrain notebook (otherwise derived from the joined file) |
| `themis_features.csv`, `earth_thermal_features.csv` | optional | Phase 2 thermal notebook (exploratory; team schema + `status`). A file with missing columns stops the start / reload and says which |

If `terrain_emit_joined.csv` is the old schema the API refuses to start and says which cells to re-run.

## Run
```
pip install -r requirements.txt
uvicorn app:app --port 8000
pip install -r requirements-dev.txt   # once, only for the smoke test (adds httpx)
python test_api.py                    # smoke test
```
Docker: `docker build -t tira-api . && docker run -p 8000:8000 tira-api` (the files in `data/` are copied into the image; hosts that set `PORT` are supported). To use other files without rebuilding: `docker run -p 8000:8000 -v $PWD/data:/srv/data tira-api`.
Deploying (Render / Railway): point the service at this folder (Dockerfile build); no start command or env var is needed. After changing a data file, rebuild/redeploy, or call `POST /api/reload` if the files are on a mounted volume.
After replacing a data file: `POST /api/reload` (no restart).

## Endpoints
| Endpoint | Returns |
|---|---|
| `GET /api/health` | status, row count |
| `GET /api/meta` | limits text (show it in the UI), counts, `field_notes` (meaning of each field) |
| `GET /api/sites` | Mars sites: id, lat/lon, missions, `terrain_only`, ranked matches, CRISM target minerals |
| `GET /api/matches?mars=<mars_region_id>&mission=&only_ranked=&limit=&offset=` | Earth windows for a site. `mission` is optional (all missions). Per mission: EMIT-ranked first, then terrain order. Each row has the map box (`bbox_w/s/e/n`) and `flags` |
| `GET /api/matches/{earth_region_id}?mars=&mission=` | One match: `match`, `all_missions`, `engine_detail` (only for the engine's top-10), `engine_warnings` |
| `GET /api/evidence/{mars_region_id}` | CRISM targets, top detections, coverage |
| `GET /api/thermal?planet=mars\|earth` | Phase 2 thermal summary per region (day / night / diurnal range, coverage, `range_rank_in_planet`, `flags`). Exploratory, separate from the match ranking |
| `GET /api/thermal/{region_id}` | All thermal features of one `mars_region_id` or `earth_region_id` (dataset, resolution, source dates, local time, season) |
| `GET /api/analogs` | Known analog sites |
| `POST /api/reload` | Re-read the files |

Missions: `landing_site`, `rover_mobility`, `terrain_overall`. Zhurong and InSight have terrain rows only (`emit_status = terrain_only`).

## Things the front-end should show
- `limits` from `/api/meta` on every results page.
- `flags` next to each row: `non_discriminating_terrain` (terrain rank separates candidates weakly), `minerals_no_emit_data` / `minerals_low_coverage` / `minerals_no_target_mineral`, `single_mineral_signal` (mineral part is yes/no only), `provisional_targets`, `unverified_source_pixels`.
- `final_rank` is a rank among EMIT-covered windows only (not global); `rank` is the terrain rank. `emit_hits` counts independent CRISM signals, not minerals.
- Thermal (Phase 2): show `thermal.limits` from `/api/meta` (or `limits` in each thermal response) next to every thermal number. It is exploratory: do not use it to sort or filter matches, do not call it thermal inertia, and do not put Mars and Earth values on one axis (absolute values are not comparable). `range_rank_in_planet` ranks inside one planet only. To show a window's thermal data, call `/api/thermal/{earth_region_id}` or `/api/thermal/{mars_region_id}`; `/api/sites` has `thermal_available`.
- Thermal `status` other than `ok` (`low_coverage`, `ls_gap`, `no_coverage`, `window_outside_tile`) comes with `flags`; values can be null.
- Rows with `final_rank = null` are valid candidates without a mineral ranking; do not hide them.
