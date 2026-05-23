from pathlib import Path

src = Path(r"C:\Users\mohideen.ashraf\Downloads\spillover_planner_extracted.py").read_text(encoding="utf-8")
start = src.find("import pandas")
end = src.find("# ---------------------------------------------------------\n# 6. EXECUTION")
core = src[start:end]
for old, new in [
    ('!pip install ortools pandas numpy plotly openrouteservice xlsxwriter "protobuf<6.0.0dev" pytz -q\n', ""),
    ("import plotly.express as px\n", ""),
    ("from google.colab import files\n", ""),
    ("import gspread\n", ""),
    ("from google.colab import auth as colab_auth\n", ""),
    ("from google.auth import default as google_default\n", ""),
]:
    core = core.replace(old, new)

old_block = """    _t5 = time.perf_counter()
    print(f"  ⏱️  [Stage 4 — ORS inter-hop calls] {_t5-_t4:.1f}s | {len(candidates) - len([c for c in candidates if c['type']=='Direct'])} multi-hop candidates added")
    _stage_times['4_ors_interhop_calls'] = _t5 - _t4"""

new_block = """    _pair_before = len(candidates)
    promising_pairs.sort(key=lambda x: x[0])
    for _d, i, j in promising_pairs[:MAX_API_CALLS_FOR_PAIRS]:
        o1, o2 = base_orders[i], base_orders[j]
        leg = get_real_route(o1['lat'], o1['lon'], o2['lat'], o2['lon'])
        inter_t, inter_dist = leg['duration_hrs'], leg['dist_km']
        vol = o1['vol'] + o2['vol']
        req_totes = vol / 20
        max_ft = min(o1['max_ft'], o2['max_ft'])
        opts = df_trucks[(df_trucks['cap'] >= req_totes) & (df_trucks['size_int'] <= max_ft)].sort_values('cap')
        if opts.empty:
            opts = df_trucks[df_trucks['size_int'] <= max_ft].sort_values('cap', ascending=False)
            if opts.empty or req_totes - opts.iloc[0]['cap'] > 15:
                continue
        truck = opts.iloc[0]['type']
        if truck not in fleet_available:
            continue
        unload_1 = round_up_025((o1['vol']/20) / 100)
        unload_2 = round_up_025((o2['vol']/20) / 100)
        load_hrs = round_up_025(req_totes / 100)
        tot_dur = load_hrs + o1['transit_out'] + unload_1 + inter_t + unload_2 + o2['transit_ret']
        if tot_dur > 24.0:
            continue
        candidates.append({
            'type': 'Pair', 'ids': [i, j], 'n_hops': 2,
            'hop_windows': [(o1['w_start'], o1['w_end']), (o2['w_start'], o2['w_end'])],
            'inter_times': [inter_t], 'unloads': [unload_1, unload_2],
            'transit_1': o1['transit_out'], 'transit_ret': o2['transit_ret'],
            'truck': truck, 'load_hrs': load_hrs, 'vol': vol,
            'dist': o1['dist_out'] + inter_dist + o2['dist_ret'],
            'route_desc': f"{o1['id']} + {o2['id']}",
        })
    _t5 = time.perf_counter()
    _pair_added = len(candidates) - _pair_before
    print(f"  ⏱️  [Stage 4 — ORS inter-hop calls] {_t5-_t4:.1f}s | {_pair_added} pair/milk-run candidates added")
    _stage_times['4_ors_interhop_calls'] = _t5 - _t4"""

if old_block not in core:
    raise SystemExit("patch anchor not found")
core = core.replace(old_block, new_block)

out = Path(__file__).resolve().parents[1] / "app" / "spillover" / "solver_core.py"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(core, encoding="utf-8")
print("wrote", out, len(core))
