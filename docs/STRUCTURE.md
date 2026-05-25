# Code structure

## Demand pipeline (no DBD)

1. **Upload / sample CSV** — Raw_1 format (`box_final_status`, `source_warehouse`, `NextStop`, `destination_warehouse`, `Count_Box_ID`).
2. **Source Grid** — Filter `box_final_status == 'Source Grid'` for selected source; pivot `source_warehouse × NextStop`.
3. **XD Grid** — Filter `XD Grid`; pivot `NextStop × destination_warehouse` for next stops linked to that source.
4. **Combine** — Direct SG demand + chained SG×XD + XD-only legs; aggregate by destination.
5. **Landing window** — All stops get the same Day or Night window from UI (not DBD).
6. **Solver** — `solve_with_ortools` → Direct + Pair trips; fleet rotation.

## Key modules

| Module | Role |
|--------|------|
| `app/main.py` | HTTP API, file upload, optimize endpoint |
| `app/spillover/data_loader.py` | Grid pivots & demand build |
| `app/spillover/landing_windows.py` | Day/Night window config |
| `app/spillover/pipeline.py` | Run solver, format JSON response |
| `app/spillover/solver_core.py` | OR-Tools CP-SAT + ORS routes |
| `app/spillover/static_ref.py` | Lat/lon cache |

## Do not edit by hand

- `app/spillover/solver_core.py` — regenerate with `scripts/build_solver_core.py`
