import pandas as pd
import numpy as np
import openrouteservice
from ortools.sat.python import cp_model
from datetime import datetime, timedelta
import time
import warnings
import re
import math
import os
import pickle
import pytz
from pathlib import Path
warnings.filterwarnings('ignore')
# IST timezone constant — used for all "now" comparisons
IST = pytz.timezone('Asia/Kolkata')
# ---------------------------------------------------------
# STAGE PROFILER
# ---------------------------------------------------------
import functools
from concurrent.futures import ThreadPoolExecutor, as_completed
_stage_times = {}
class StageTimer:
    def __init__(self, name):
        self.name = name
    def __enter__(self):
        self._start = time.perf_counter()
        print(f"  ⏱️  [{self.name}] starting...")
        return self
    def __exit__(self, *args):
        elapsed = time.perf_counter() - self._start
        _stage_times[self.name] = elapsed
        print(f"  ✅  [{self.name}] done in {elapsed:.1f}s")
def print_profile_summary():
    print("\n" + "="*52)
    print("📊 PROFILING SUMMARY")
    print("="*52)
    total = sum(_stage_times.values())
    for stage, t in sorted(_stage_times.items(), key=lambda x: -x[1]):
        pct = (t/total*100) if total > 0 else 0
        bar = "█" * int(pct / 5)
        print(f"  {stage:<30} {t:>6.1f}s  {pct:>4.0f}%  {bar}")
    print(f"  {'TOTAL':<30} {total:>6.1f}s")
    print("="*52)
# ==========================================
# 🔑 CONFIGURATION
# ==========================================
ORS_API_KEY = os.environ.get("ORS_API_KEY", "")
MAX_DOCKS = 9
MAX_TOTAL_HOLD_HOURS = 1.0
BUFFER_MINS = 60
DAYS_IN_MONTH = 30
MAX_API_CALLS_FOR_PAIRS = int(os.environ.get("SPILLOVER_MAX_PAIR_API_CALLS", "300"))
AVG_SPEED_KMPH = 25
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = os.environ.get("ORS_CACHE_FILE", str(_DATA_DIR / "ors_route_cache.pkl"))
# --- SPATIAL & VOLUME CONSTRAINTS ---
MAX_INTER_HOP_KM = 10.0
MAX_DETOUR_FACTOR = 0.7
SOURCE_LEG_BUFFER_KM = 15.0
# AVAILABLE_FLEET is populated at runtime via prompt_fleet_input() in Section 6.
# ---------------------------------------------------------
# 1. CACHING & API SETUP
# ---------------------------------------------------------
def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'rb') as f:
                data = pickle.load(f)
                print(f"📂 Loaded Cache with {len(data)} routes.")
                return data
        except:
            return {}
    return {}
def save_cache(cache_data):
    try:
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(cache_data, f)
        print(f"💾 Cache Saved ({len(cache_data)} routes).")
    except:
        pass
route_cache = load_cache()
client = None
if ORS_API_KEY:
    try:
        client = openrouteservice.Client(key=ORS_API_KEY)
        print("✅ ORS Client Initialized")
    except Exception:
        print("⚠️ ORS Client Failed. Check Key.")
else:
    print("ℹ️ ORS_API_KEY not set — Haversine fallback for all routes.")
# ---------------------------------------------------------
# 2. HELPER FUNCTIONS & DATA ENGINEERING
# ---------------------------------------------------------
def parse_truck_cap(val, field_name=''):
    if pd.isna(val): return 99  # NaN = no restriction, allow any truck
    s = str(val).upper()
    match = re.search(r'(\d+)', s)
    if match: return int(match.group(1))
    print(f"⚠️  parse_truck_cap: could not parse '{val}'"
          + (f" in field '{field_name}'" if field_name else "")
          + " — defaulting to no restriction (99FT). Check your data.")
    return 99
def round_up_025(val):
    return math.ceil(val * 4) / 4
def floor_to_15_min(dt):
    minute = dt.minute
    new_minute = (minute // 15) * 15
    return dt.replace(minute=new_minute, second=0, microsecond=0)
def round_to_nearest_15(dt):
    minute = dt.minute
    if minute < 8:    new_min = 0
    elif minute < 23: new_min = 15
    elif minute < 38: new_min = 30
    elif minute < 53: new_min = 45
    else:
        dt = dt + timedelta(hours=1)
        new_min = 0
    return dt.replace(minute=new_min, second=0, microsecond=0)
def parse_time_window(val, base_date):
    if pd.isna(val): return base_date.replace(hour=23, minute=59)
    if isinstance(val, str):
        t = pd.to_datetime(val.strip(), format='%H:%M:%S').time()
    elif hasattr(val, 'hour'):
        t = val
    else:
        t = pd.to_datetime(str(val)).time()
    dt = base_date.replace(hour=t.hour, minute=t.minute)
    if t.hour == 0 and t.minute == 0: dt += timedelta(days=1)
    return dt
ORS_PROFILE = 'driving-car'
# ==========================================
# 📋 GOOGLE SHEET CONFIGURATION
# ==========================================
# Paste your Google Sheet URL below.
# The sheet must have a tab named exactly: Raw_1
SHEET_URL  = "https://docs.google.com/spreadsheets/d/1EQ377yHq2C-KP5CBqtRy_IIewnYViiPsX0yFpjvZH8Y/edit"
RAW_SHEET   = "Raw_1"
# Static reference sheet — sources, destinations lat/long, and time windows
STATIC_SHEET_URL = "https://docs.google.com/spreadsheets/d/11nQJuyaxgIbyXosw9MswdDfcfNalMATDeHvWhceTHrw/edit"
# Tab names in the static reference sheet
TAB_SOURCES  = "Sources"
TAB_DESTS    = "Destinations"
TAB_WINDOWS  = "Landing Window"
# ==========================================
# ── DBD WINDOW (hours from now in IST) ────────────────────────────────────────
DBD_PAST_CUTOFF_HRS   = 4    # ignore if DBD is more than this many hours in the past
DBD_FUTURE_CUTOFF_HRS = 12   # ignore if DBD is more than this many hours in the future
# ──────────────────────────────────────────────────────────────────────────────
# ── MINIMUM TOTE THRESHOLD ────────────────────────────────────────────────────
MIN_TOTES = 40   # destinations with totes equal to or less than this are excluded
# ──────────────────────────────────────────────────────────────────────────────
# ── SOURCE DISTANCE FILTER ────────────────────────────────────────────────────
MAX_SOURCE_KM = 80.0   # destinations further than this from the source are excluded
# ──────────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────────
# ── MULTI-HOP CONFIG ───────────────────────────────────────────────────────────
MAX_HOPS = 3   # maximum stops per trip (1 = direct only, 2 = pairs, 3 = up to 3 stops)
# ──────────────────────────────────────────────────────────────────────────────
def get_real_route(lat1, lon1, lat2, lon2):
    key = (ORS_PROFILE, round(lat1,4), round(lon1,4), round(lat2,4), round(lon2,4))
    if key in route_cache: return route_cache[key]
    if client is None:
        R = 6371
        l1, ln1, l2, ln2 = map(np.radians, [lat1, lon1, lat2, lon2])
        a = np.sin((l2-l1)/2)**2 + np.cos(l1)*np.cos(l2)*np.sin((ln2-ln1)/2)**2
        d = 2 * R * np.arctan2(np.sqrt(a), np.sqrt(1-a))
        res = {'dist_km': d, 'duration_hrs': round_up_025(d / AVG_SPEED_KMPH)}
        route_cache[key] = res
        return res
    try:
        coords = [[lon1, lat1], [lon2, lat2]]
        routes = client.directions(coordinates=coords, profile=ORS_PROFILE, format='geojson', validate=False)
        summary = routes['features'][0]['properties']['segments'][0]
        pure_dist = summary['distance'] / 1000
        res = {'dist_km': pure_dist, 'duration_hrs': round_up_025(pure_dist / AVG_SPEED_KMPH)}
        route_cache[key] = res
        return res
    except openrouteservice.exceptions.ApiError as e:
        print(f"⚠️  ORS API error ({lat1:.4f},{lon1:.4f})→({lat2:.4f},{lon2:.4f}): {e} — using Haversine fallback.")
        R = 6371
        l1, ln1, l2, ln2 = map(np.radians, [lat1, lon1, lat2, lon2])
        a = np.sin((l2-l1)/2)**2 + np.cos(l1)*np.cos(l2)*np.sin((ln2-ln1)/2)**2
        d = 2 * R * np.arctan2(np.sqrt(a), np.sqrt(1-a))
        return {'dist_km': d, 'duration_hrs': round_up_025(d / AVG_SPEED_KMPH)}
    except Exception as e:
        print(f"⚠️  ORS unexpected error ({lat1:.4f},{lon1:.4f})→({lat2:.4f},{lon2:.4f}): {type(e).__name__}: {e} — using Haversine fallback.")
        R = 6371
        l1, ln1, l2, ln2 = map(np.radians, [lat1, lon1, lat2, lon2])
        a = np.sin((l2-l1)/2)**2 + np.cos(l1)*np.cos(l2)*np.sin((ln2-ln1)/2)**2
        d = 2 * R * np.arctan2(np.sqrt(a), np.sqrt(1-a))
        return {'dist_km': d, 'duration_hrs': round_up_025(d / AVG_SPEED_KMPH)}
# ---------------------------------------------------------
# 2b. INTERACTIVE FLEET INPUT
# ---------------------------------------------------------
# Fixed fleet sizes available for input
FLEET_SIZES = ['07FT', '08FT', '10FT', '14FT', '17FT', '20FT', '22FT']
def prompt_fleet_input():
    """
    Asks user for count of each standard fleet size.
    Fixed list: 7, 8, 10, 14, 17, 20, 22 FT.
    Returns dict like {'10FT': 8, '20FT': 3}.
    """
    print("\n" + "="*52)
    print("🚛  AVAILABLE FLEET INPUT")
    print("   Enter how many trucks of each size are")
    print("   physically on ground today.")
    print("   (Press Enter or type 0 to skip a size.)")
    print("="*52)
    fleet = {}
    for t in FLEET_SIZES:
        while True:
            try:
                raw   = input(f"   {t}: ").strip()
                count = int(raw) if raw else 0
                if count < 0:
                    print("   ⚠️  Please enter 0 or a positive number.")
                    continue
                fleet[t] = count
                break
            except ValueError:
                print("   ⚠️  Invalid input — enter a whole number.")
    active = {t: c for t, c in fleet.items() if c > 0}
    if not active:
        raise ValueError("❌ No trucks entered. Please re-run and enter at least one truck.")
    print("\n   ✅ Fleet confirmed:")
    for t, c in active.items():
        print(f"      {t}: {c} truck{'s' if c > 1 else ''}")
    print("="*52 + "\n")
    return fleet
# ---------------------------------------------------------
# 2c. LIVE GOOGLE SHEETS LOADER
# ---------------------------------------------------------
def authenticate_gsheets():
    """Authenticate with Google using Colab credentials. Call once per session."""
    print("\n🔐 Authenticating with Google Sheets...")
    colab_auth.authenticate_user()
    creds, _ = google_default()
    gc = gspread.authorize(creds)
    print("✅ Google Sheets authenticated.")
    return gc
def load_live_demand(gc, source_warehouse, df_sources, df_dests, windows):
    """
    Pulls Raw_1 from the live Google Sheet, filters to:
      - box_final_status == 'Source Grid'
      - source_warehouse == user-selected warehouse
      - dbd within [now_IST - DBD_PAST_CUTOFF_HRS, now_IST + DBD_FUTURE_CUTOFF_HRS]
      - aggregated totes > MIN_TOTES (destinations with <= MIN_TOTES totes are excluded)
    Aggregates by destination_warehouse:
      - volume  = sum of Quantity (units)
      - totes   = volume / 20 (boxes)
      - window_end = earliest dbd within the valid window
      - window_start = same as window_end
    Then merges with Lat/Long from the static reference sheet.
    Returns a df compatible with solve_with_ortools().
    """
    print(f"\n📡 Fetching live data from Google Sheets (sheet: {RAW_SHEET})...")
    sh  = gc.open_by_url(SHEET_URL)
    ws  = sh.worksheet(RAW_SHEET)
    # Use get_values() to avoid duplicate header error (e.g. two 'Flag' columns)
    all_vals = ws.get_values()
    headers  = all_vals[0]
    # Deduplicate headers by appending _1, _2 etc for repeats
    seen = {}
    deduped = []
    for h in headers:
        if h in seen:
            seen[h] += 1
            deduped.append(f'{h}_{seen[h]}')
        else:
            seen[h] = 0
            deduped.append(h)
    df_raw = pd.DataFrame(all_vals[1:], columns=deduped)
    print(f"   Fetched {len(df_raw)} rows.")
    df_raw.columns = [str(c).strip() for c in df_raw.columns]
    # Filter to Source Grid only
    df_raw = df_raw[df_raw['box_final_status'] == 'Source Grid'].copy()
    print(f"   Source Grid rows: {len(df_raw)}")
    if df_raw.empty:
        raise ValueError("❌ No 'Source Grid' rows found in Raw_1.")
    # Filter to selected source warehouse only
    df_src = df_raw[df_raw['source_warehouse'] == source_warehouse].copy()
    print(f"   Rows for '{source_warehouse}': {len(df_src)}")
    if df_src.empty:
        raise ValueError(f"❌ No Source Grid rows for warehouse '{source_warehouse}'.")
    # Parse types
    df_src['Quantity'] = pd.to_numeric(df_src['Quantity'], errors='coerce').fillna(0)
    df_src['dbd_dt']   = pd.to_datetime(df_src['dbd'], errors='coerce')
    # ── DBD TIME FILTER (IST-aware) ────────────────────────────────────────────
    # Localize dbd_dt to IST (sheet stores times in IST without tz suffix).
    # If your sheet already has tz-aware timestamps with +05:30, replace
    # tz_localize(...) with .dt.tz_convert(IST) instead.
    df_src['dbd_dt'] = df_src['dbd_dt'].dt.tz_localize(
        IST, ambiguous='infer', nonexistent='shift_forward'
    )
    # now_ts is IST-aware — matches the localized dbd_dt for correct comparison
    now_ts   = datetime.now(IST)
    dbd_low  = now_ts - timedelta(hours=DBD_PAST_CUTOFF_HRS)
    dbd_high = now_ts + timedelta(hours=DBD_FUTURE_CUTOFF_HRS)
    before_filter = len(df_src)
    df_src = df_src[
        (df_src['dbd_dt'] >= dbd_low) &
        (df_src['dbd_dt'] <= dbd_high)
    ].copy()
    dropped_dbd = before_filter - len(df_src)
    if dropped_dbd > 0:
        print(f"   ⏱️  Dropped {dropped_dbd} rows outside DBD window "
              f"[{dbd_low.strftime('%H:%M')} – {dbd_high.strftime('%H:%M')} IST].")
    if df_src.empty:
        raise ValueError("❌ No rows remain after DBD filter. All shipments are outside the routing window.")
    # ──────────────────────────────────────────────────────────────────────────
    # Aggregate per destination
    agg = df_src.groupby('destination_warehouse').agg(
        volume     = ('Quantity', 'sum'),
        window_end = ('dbd_dt',   'min'),   # earliest valid deadline
    ).reset_index()
    agg.rename(columns={'destination_warehouse': 'id'}, inplace=True)
    agg['totes']        = agg['volume'] / 20   # Qty/20 = box count
    agg['window_start'] = agg['window_end']
    agg['id']           = agg['id'].astype(str).str.strip()
    print(f"   Aggregated to {len(agg)} unique destinations "
          f"| Total boxes: {agg['totes'].sum():.0f}")
    # ── TOTE FILTER — drop destinations with totes <= MIN_TOTES ───────────────
    below_min = agg[agg['totes'] <= MIN_TOTES]
    if not below_min.empty:
        print(f"   🚫 Dropping {len(below_min)} destination(s) with <= {MIN_TOTES} totes: "
              f"{below_min['id'].tolist()}")
    agg = agg[agg['totes'] > MIN_TOTES].copy()
    if agg.empty:
        raise ValueError(f"❌ No destinations remain after tote filter (minimum {MIN_TOTES} totes).")
    print(f"   ✅ After tote filter: {len(agg)} destinations remain "
          f"| Total boxes: {agg['totes'].sum():.0f}")
    # ──────────────────────────────────────────────────────────────────────────
    # ── Merge lat/long from static reference sheet ────────────────────────────
    df_lat = df_dests[['id', 'lat', 'lon']].drop_duplicates(subset=['id'])
    df = agg.merge(df_lat, on='id', how='left')
    # ── Strip timezone before passing to downstream (pipeline uses naive datetimes)
    df['window_end']   = df['window_end'].dt.tz_localize(None)
    df['window_start'] = df['window_start'].dt.tz_localize(None)
    # max_ft — no longer from sheet; default to largest truck (no restriction)
    df['max_ft'] = np.nan
    df['max_ft_int'] = 99  # no size restriction — fleet prompt controls truck types
    # ── HAVERSINE SOURCE-DISTANCE FILTER — drop destinations > MAX_SOURCE_KM ──
    # Pure math, no API calls — runs instantly regardless of destination count.
    # Applied here so every downstream step (ORS legs, solver, ad-hoc) only
    # ever sees destinations within range.
    def _haversine_km(lat1, lon1, lat2, lon2):
        R = 6371
        l1, ln1, l2, ln2 = map(np.radians, [lat1, lon1, lat2, lon2])
        a = (np.sin((l2 - l1) / 2) ** 2
             + np.cos(l1) * np.cos(l2) * np.sin((ln2 - ln1) / 2) ** 2)
        return 2 * R * np.arcsin(np.sqrt(a))
    src_row_ref = df_sources[df_sources['id'] == source_warehouse]
    if not src_row_ref.empty:
        src_lat = float(src_row_ref.iloc[0]['lat'])
        src_lon = float(src_row_ref.iloc[0]['lon'])
        df = df.copy()
        df['_dist_from_src_km'] = df.apply(
            lambda r: _haversine_km(src_lat, src_lon, r['lat'], r['lon'])
            if pd.notna(r['lat']) else np.nan,
            axis=1
        )
        too_far = df[df['_dist_from_src_km'] > MAX_SOURCE_KM]
        if not too_far.empty:
            print(f"   📍 Dropping {len(too_far)} destination(s) beyond {MAX_SOURCE_KM}km from source: "
                  f"{too_far['id'].tolist()}")
        df = df[df['_dist_from_src_km'] <= MAX_SOURCE_KM].drop(columns=['_dist_from_src_km']).copy()
        if df.empty:
            raise ValueError(f"❌ No destinations within {MAX_SOURCE_KM}km of source '{source_warehouse}'.")
        print(f"   ✅ After distance filter: {len(df)} destinations within {MAX_SOURCE_KM}km.")
    else:
        print(f"   ⚠️  Could not apply distance filter — source '{source_warehouse}' not in df_sources.")
    # ──────────────────────────────────────────────────────────────────────────
    missing = df['lat'].isna().sum()
    if missing > 0:
        print(f"   ⚠️  {missing} destinations have no lat/long in reference sheet — will be dropped.")
    print(f"   ✅ Live demand ready: {len(df)} destinations, total volume {df['volume'].sum():.0f} units.")
    return df
def prompt_source_warehouse(gc):
    """
    Reads Raw_1 fully once to:
      1. Show all unique source warehouses with Source Grid rows as a numbered menu.
      2. After selection, print how many unique destinations are mapped to that source.
    Returns the chosen source_warehouse string.
    """
    print("\n" + "="*52)
    print("🏭  SOURCE WAREHOUSE SELECTION")
    print("   Reading sheet... (this may take a moment)")
    print("="*52)
    sh  = gc.open_by_url(SHEET_URL)
    ws  = sh.worksheet(RAW_SHEET)
    all_vals = ws.get_values()
    headers  = all_vals[0]
    seen = {}
    deduped = []
    for h in headers:
        if h in seen:
            seen[h] += 1
            deduped.append(f'{h}_{seen[h]}')
        else:
            seen[h] = 0
            deduped.append(h)
    df_all = pd.DataFrame(all_vals[1:], columns=deduped)
    df_all.columns = [str(c).strip() for c in df_all.columns]
    # Only look at Source Grid rows
    df_sg = df_all[df_all['box_final_status'] == 'Source Grid'].copy()
    # Build source → destination count mapping
    src_dest = (df_sg.groupby('source_warehouse')['destination_warehouse']
                     .nunique()
                     .reset_index()
                     .rename(columns={'destination_warehouse': 'dest_count'})
                     .sort_values('source_warehouse'))
    sources = src_dest['source_warehouse'].tolist()
    counts  = src_dest['dest_count'].tolist()
    print("   Available source warehouses (Source Grid rows only):")
    for i, (s, c) in enumerate(zip(sources, counts), 1):
        print(f"   {i:>2}. {s}  ({c} destinations)")
    print("="*52)
    while True:
        raw_inp = input("   Enter number or exact warehouse name: ").strip()
        if raw_inp.isdigit() and 1 <= int(raw_inp) <= len(sources):
            chosen = sources[int(raw_inp) - 1]
            break
        elif raw_inp in sources:
            chosen = raw_inp
            break
        else:
            print(f"   ⚠️  Invalid — enter a number (1–{len(sources)}) or exact name.")
    dest_count = counts[sources.index(chosen)]
    print(f"\n   ✅ Selected: {chosen} → {dest_count} destinations to route\n")
    return chosen
# ---------------------------------------------------------
# 2d. STATIC REFERENCE SHEET LOADER
# ---------------------------------------------------------
def load_static_reference(gc):
    """
    Reads the static reference sheet — 3 separate tabs:
      - "Sources"        → columns: Sources, Lat, Long
      - "Destinations"   → columns: Destinations, Lat, Long
      - "Landing Window" → columns: Part, Window Start, Window End
    Returns:
      df_sources — DataFrame: id, lat, lon
      df_dests   — DataFrame: id, lat, lon
      windows    — dict e.g. {'Day': ('10:00', '18:00'), 'Night': ('22:00', '04:00')}
    """
    print("\n📍 Loading static reference data (lat/long + windows)...")
    sh = gc.open_by_url(STATIC_SHEET_URL)
    # ── Sources tab ───────────────────────────────────────────────────────────
    ws_src  = sh.worksheet(TAB_SOURCES)
    rows    = ws_src.get_values()[1:]   # skip header row
    df_sources = pd.DataFrame(rows, columns=['id', 'lat', 'lon'])
    df_sources['lat'] = pd.to_numeric(df_sources['lat'], errors='coerce')
    df_sources['lon'] = pd.to_numeric(df_sources['lon'], errors='coerce')
    df_sources['id']  = df_sources['id'].astype(str).str.strip()
    # ── Destinations tab ──────────────────────────────────────────────────────
    ws_dst  = sh.worksheet(TAB_DESTS)
    rows    = ws_dst.get_values()[1:]
    df_dests = pd.DataFrame(rows, columns=['id', 'lat', 'lon'])
    df_dests['lat'] = pd.to_numeric(df_dests['lat'], errors='coerce')
    df_dests['lon'] = pd.to_numeric(df_dests['lon'], errors='coerce')
    df_dests['id']  = df_dests['id'].astype(str).str.strip()
    # ── Landing Window tab ────────────────────────────────────────────────────
    ws_win  = sh.worksheet(TAB_WINDOWS)
    rows    = ws_win.get_values()[1:]   # Part | Window Start | Window End
    windows = {}
    for row in rows:
        if len(row) >= 3 and row[0].strip():
            windows[row[0].strip()] = (row[1].strip(), row[2].strip())
    print(f"   ✅ {len(df_sources)} sources | {len(df_dests)} destinations | "
          f"windows: {list(windows.keys())}")
    return df_sources, df_dests, windows
# ---------------------------------------------------------
# 3. OR-TOOLS ENGINE  (fleet-cap aware)
# ---------------------------------------------------------
def solve_with_ortools(df, df_trucks, src, available_fleet):
    print("🚀 Generating Options (Smart Filter + Fleet Count Constraints)...")
    # Build fleet availability counter (mutable, used both in solver & ad-hoc)
    fleet_available = {t: cnt for t, cnt in available_fleet.items() if cnt > 0}
    _t1 = time.perf_counter()
    base_orders = []
    try:
        first_date = pd.to_datetime(df['window_start'].iloc[0])
        base_d = first_date.replace(hour=0, minute=0, second=0, microsecond=0)
    except:
        base_d = datetime(2024, 1, 2)
    missing_locs = []
    # Pre-parse time windows (no I/O, fast)
    valid_rows = []
    for idx, r in df.iterrows():
        if pd.isna(r['lat']):
            missing_locs.append(r['id'])
            continue
        w_start = parse_time_window(r['window_start'], base_d)
        w_end   = parse_time_window(r['window_end'],   base_d)
        if w_start > w_end: w_end += timedelta(days=1)
        w_start = round_to_nearest_15(w_start)
        w_end   = round_to_nearest_15(w_end)
        vol = pd.to_numeric(r['volume'], errors='coerce') or 0
        valid_rows.append({
            'id': r['id'], 'vol': vol,
            'lat': float(r['lat']), 'lon': float(r['lon']),
            'w_start': w_start, 'w_end': w_end,
            'max_ft': r['max_ft_int']
        })
    # Parallel ORS calls — fetch all source legs concurrently
    def _fetch_legs(row):
        """Fetch outbound and return ORS distances for one store."""
        route = get_real_route(src[0], src[1], row['lat'], row['lon'])
        ret   = get_real_route(row['lat'], row['lon'], src[0], src[1])
        return row['id'], route['dist_km'], ret['dist_km']
    MAX_WORKERS = min(16, len(valid_rows))
    leg_results = {}
    if valid_rows:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_fetch_legs, row): row['id'] for row in valid_rows}
            for future in as_completed(futures):
                store_id, dist_out, dist_ret = future.result()
                leg_results[store_id] = (dist_out, dist_ret)
    # Build base_orders in original row order
    for row in valid_rows:
        dist_out_km = leg_results[row['id']][0] + SOURCE_LEG_BUFFER_KM
        dist_ret_km = leg_results[row['id']][1] + SOURCE_LEG_BUFFER_KM
        transit_out = round_up_025(dist_out_km / AVG_SPEED_KMPH)
        transit_ret = round_up_025(dist_ret_km / AVG_SPEED_KMPH)
        base_orders.append({
            'ix': len(base_orders), 'id': row['id'], 'vol': row['vol'],
            'lat': row['lat'], 'lon': row['lon'],
            'w_start': row['w_start'], 'w_end': row['w_end'], 'max_ft': row['max_ft'],
            'transit_out': transit_out, 'dist_out': dist_out_km,
            'transit_ret': transit_ret, 'dist_ret': dist_ret_km
        })
    _t2 = time.perf_counter()
    print(f"  ⏱️  [Stage 1 — ORS source legs] {_t2-_t1:.1f}s | {len(base_orders)} orders built")
    _stage_times['1_ors_source_legs'] = _t2 - _t1
    if missing_locs:
        print(f"⚠️ WARNING: Dropped {len(missing_locs)} orders due to missing Lat/Lon: {missing_locs}")
    candidates = []
    # --- DIRECT TRIPS ---
    for ord in base_orders:
        req_totes = ord['vol'] / 20
        # Primary: smallest truck that fits volume within max_ft
        opts = df_trucks[
            (df_trucks['cap'] >= req_totes) &
            (df_trucks['size_int'] <= ord['max_ft'])
        ].sort_values('cap')
        overflow_flag = False
        if opts.empty:
            # Closest-match fallback: largest available truck within max_ft.
            opts = df_trucks[df_trucks['size_int'] <= ord['max_ft']].sort_values('cap', ascending=False)
            if opts.empty:
                continue
            best = opts.iloc[0]
            overflow = req_totes - best['cap']
            if overflow > 15:
                print(f"   ⚠️  {ord['id']}: {req_totes:.1f} totes exceeds largest permissible truck "
                      f"({best['type']} cap={best['cap']}) by {overflow:.1f} totes — skipping candidate.")
                continue
            truck = best['type']
            overflow_flag = True
            if overflow > 0:
                print(f"   ℹ️  {ord['id']}: Closest-match used ({truck}, cap={best['cap']}) "
                      f"for {req_totes:.1f} totes (+{overflow:.1f} tote overflow).")
        else:
            truck = opts.iloc[0]['type']
        # Skip if this truck type isn't in our available fleet at all
        if truck not in fleet_available:
            continue
        load_hrs  = round_up_025((ord['vol']/20) / 100)
        tot_dur   = load_hrs + ord['transit_out'] + load_hrs + ord['transit_ret']
        if tot_dur > 24.0: continue
        earliest_dispatch = floor_to_15_min(ord['w_start'] - timedelta(hours=ord['transit_out']))
        latest_dispatch   = floor_to_15_min(ord['w_end']   - timedelta(hours=ord['transit_out']))
        dist = ord['dist_out'] + ord['dist_ret']
        candidates.append({
            'type': 'Direct', 'ids': [ord['ix']], 'truck': truck,
            'load_hrs': load_hrs,
            'vol': ord['vol'],
            'earliest_dispatch': earliest_dispatch,
            'latest_dispatch':   latest_dispatch,
            'transit_1':   ord['transit_out'],
            'transit_ret': ord['transit_ret'],
            'dist': dist,
            'landing':    ord['w_end'],
            'hop2_arrival': None,
            'route_desc': ord['id']
        })
    _t3 = time.perf_counter()
    print(f"  ⏱️  [Stage 2 — Direct candidates] {_t3-_t2:.1f}s | {len(candidates)} direct candidates")
    _stage_times['2_direct_candidates'] = _t3 - _t2
    # --- MILK RUNS ---
    print(f"   Scanning Pairs (Filtering > 24h & Fleet Cap)...")
    promising_pairs = []
    MAX_FLEET_CAP = df_trucks['cap'].max()
    for i in range(len(base_orders)):
        for j in range(len(base_orders)):
            if i == j: continue
            o1, o2 = base_orders[i], base_orders[j]
            if (o1['vol'] + o2['vol'])/20 > MAX_FLEET_CAP: continue
            R = 6371
            l1,ln1,l2,ln2 = map(np.radians,[o1['lat'],o1['lon'],o2['lat'],o2['lon']])
            a = np.sin((l2-l1)/2)**2 + np.cos(l1)*np.cos(l2)*np.sin((ln2-ln1)/2)**2
            d = 2 * R * np.arctan2(np.sqrt(a), np.sqrt(1-a))
            if d > MAX_INTER_HOP_KM: continue
            if d > (o1['dist_out'] + o2['dist_out']) * MAX_DETOUR_FACTOR: continue
            t_est    = d / AVG_SPEED_KMPH
            unload_1 = round_up_025((o1['vol']/20) / 100)
            earliest_arr_2 = o1['w_start'] + timedelta(hours=unload_1 + t_est)
            if earliest_arr_2 > o2['w_end']: continue
            latest_arr_2 = o1['w_end'] + timedelta(hours=unload_1 + t_est)
            gap = 0
            if latest_arr_2 < o2['w_start']:
                gap = (o2['w_start'] - latest_arr_2).total_seconds() / 3600
            if gap > MAX_TOTAL_HOLD_HOURS + 1.0: continue
            promising_pairs.append((d, i, j))
    _t4 = time.perf_counter()
    print(f"  ⏱️  [Stage 3 — Haversine pre-filter] {_t4-_t3:.1f}s | {len(promising_pairs)} pairs")
    _stage_times['3_haversine_prefilter'] = _t4 - _t3
    _pair_before = len(candidates)
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
    _stage_times['4_ors_interhop_calls'] = _t5 - _t4
    # --- CP-SAT SOLVER ---
    print(f"   Solving with {len(candidates)} options across fleet: {fleet_available}...")
    model  = cp_model.CpModel()
    x      = {}
    slacks = {}
    for c_idx, c in enumerate(candidates):
        x[c_idx] = model.NewBoolVar(f'cand_{c_idx}')
    # Each order covered exactly once (or slack)
    for o_idx in range(len(base_orders)):
        slacks[o_idx] = model.NewBoolVar(f'slack_{o_idx}')
        model.Add(
            sum(x[c_idx] for c_idx, c in enumerate(candidates) if o_idx in c['ids'])
            + slacks[o_idx] == 1
        )
    # ── FLEET COUNT CONSTRAINTS ────────────────────────────────────────────────
    for truck_type, max_count in fleet_available.items():
        type_vars = [x[c_idx] for c_idx, c in enumerate(candidates) if c['truck'] == truck_type]
        if type_vars:
            model.Add(sum(type_vars) <= max_count)
    # ──────────────────────────────────────────────────────────────────────────
    intervals = []
    demands   = []
    all_vars  = {}
    MAX_HORIZON_MINS = 3600
    for c_idx, c in enumerate(candidates):
        load_mins = int(c['load_hrs'] * 60)
        if c['type'] == 'Direct':
            min_disp = int((c['earliest_dispatch'] - base_d).total_seconds() / 60)
            max_disp = int((c['latest_dispatch']   - base_d).total_seconds() / 60)
            dispatch_var    = model.NewIntVar(min_disp, max_disp, f'disp_{c_idx}')
            vpt_var         = model.NewIntVar(0, MAX_HORIZON_MINS, f'vpt_{c_idx}')
            source_hold_var = model.NewIntVar(0, int(MAX_TOTAL_HOLD_HOURS*60), f'sh_{c_idx}')
            model.Add(vpt_var + load_mins + source_hold_var == dispatch_var)
            fixed_mins = int((c['load_hrs'] + c['transit_1'] + c['load_hrs'] + c['transit_ret']) * 60)
            model.Add(source_hold_var <= max(0, (24*60) - fixed_mins))
            all_vars[c_idx] = {'vpt': vpt_var, 'disp': dispatch_var, 'hold': source_hold_var}
        else:
            n          = c['n_hops']
            hop_wins   = c['hop_windows']
            inter_t    = c['inter_times']
            transit1_mins = int(c['transit_1'] * 60)
            arr_vars   = []
            hold_vars  = []
            total_hold = None
            for k in range(n):
                w_s, w_e = hop_wins[k]
                mn = int((w_s - base_d).total_seconds() / 60)
                mx = int((w_e - base_d).total_seconds() / 60)
                mn = max(0, mn); mx = max(mn, mx)
                arr_vars.append(model.NewIntVar(mn, mx, f'arr{k}_{c_idx}'))
            for k in range(n - 1):
                ih = model.NewIntVar(0, int(MAX_TOTAL_HOLD_HOURS*60), f'ih{k}_{c_idx}')
                hold_vars.append(ih)
                inter_mins_k = int(inter_t[k] * 60)
                model.Add(arr_vars[k+1] == arr_vars[k] + inter_mins_k + ih)
            dispatch_var    = model.NewIntVar(0, MAX_HORIZON_MINS, f'disp_{c_idx}')
            model.Add(dispatch_var == arr_vars[0] - transit1_mins)
            vpt_var         = model.NewIntVar(0, MAX_HORIZON_MINS, f'vpt_{c_idx}')
            source_hold_var = model.NewIntVar(0, int(MAX_TOTAL_HOLD_HOURS*60), f'sh_{c_idx}')
            model.Add(vpt_var + load_mins + source_hold_var == dispatch_var)
            all_hold_vars = [source_hold_var] + hold_vars
            total_hold_expr = sum(all_hold_vars)
            fixed_mins = int((c['load_hrs'] + c['transit_1']
                              + sum(inter_t)
                              + sum(c['unloads'][1:])
                              + c['transit_ret']) * 60)
            model.Add(total_hold_expr <= max(0, (24*60) - fixed_mins))
            arr1_var = arr_vars[0]
            arr2_var = arr_vars[1] if n >= 2 else None
            all_vars[c_idx] = {
                'vpt': vpt_var, 'disp': dispatch_var,
                'arr1': arr1_var,
                'arr2': arr2_var,
                'arr_vars': arr_vars,
                'hold': total_hold_expr
            }
        interval = model.NewOptionalFixedSizeIntervalVar(vpt_var, load_mins, x[c_idx], f'dock_{c_idx}')
        intervals.append(interval)
        demands.append(1)
    model.AddCumulative(intervals, demands, MAX_DOCKS)
    hold_terms = []
    for c_idx in all_vars:
        raw_hold = all_vars[c_idx]['hold']
        ub = int(MAX_TOTAL_HOLD_HOURS * 60 * 2)
        ah = model.NewIntVar(0, ub, f'ah_{c_idx}')
        model.Add(ah <= raw_hold)
        model.Add(ah <= ub * x[c_idx])
        model.Add(ah >= raw_hold - ub * (1 - x[c_idx]))
        hold_terms.append(ah)
    hold_term  = sum(hold_terms)
    slack_term = sum(slacks[o] * 100000000000 for o in slacks)
    model.Minimize(hold_term + slack_term)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30
    status = solver.Solve(model)
    results       = []
    dropped_ixs   = set()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("✅ Primary Solution Found!")
        dropped_ixs = {o for o in slacks if solver.Value(slacks[o]) > 0}
        if dropped_ixs:
            names = [base_orders[o]['id'] for o in dropped_ixs]
            print(f"⚠️ Solver dropped {len(dropped_ixs)} orders (fleet/time infeasible): {names}")
        for c_idx, c in enumerate(candidates):
            if solver.BooleanValue(x[c_idx]):
                vals    = all_vars[c_idx]
                vpt     = base_d + timedelta(minutes=solver.Value(vals['vpt']))
                disp    = base_d + timedelta(minutes=solver.Value(vals['disp']))
                load_end = vpt + timedelta(hours=c['load_hrs'])
                source_hold = (disp - load_end).total_seconds() / 3600
                ih_hold     = 0
                arr1 = arr2 = None
                trip_fixed_time = 0
                if c['type'] == 'Direct':
                    arr1 = disp + timedelta(hours=c['transit_1'])
                    arr_all = [arr1]
                    trip_fixed_time = c['load_hrs'] + c['transit_1'] + c['load_hrs'] + c['transit_ret']
                    ih_hold = 0
                else:
                    arr_all = [
                        base_d + timedelta(minutes=solver.Value(v))
                        for v in vals['arr_vars']
                    ]
                    arr1 = arr_all[0]
                    arr2 = arr_all[1] if len(arr_all) > 1 else None
                    ih_hold = 0
                    for k in range(len(arr_all) - 1):
                        actual_gap  = (arr_all[k+1] - arr_all[k]).total_seconds() / 3600
                        fixed_gap   = c['inter_times'][k]
                        ih_hold    += max(0, actual_gap - fixed_gap)
                    trip_fixed_time = (c['load_hrs'] + c['transit_1']
                                       + sum(c['inter_times'])
                                       + sum(c['unloads'][1:])
                                       + c['transit_ret'])
                total_duration = trip_fixed_time + source_hold + ih_hold
                ret_time       = vpt + timedelta(hours=total_duration)
                hop_cols = {}
                for k, at in enumerate(arr_all, 1):
                    hop_cols[f'hop{k}'] = round_to_nearest_15(at)
                for k in range(len(arr_all) + 1, MAX_HOPS + 1):
                    hop_cols[f'hop{k}'] = None
                results.append({
                    'type':  c['type'],
                    'route': c['route_desc'],
                    'truck': c['truck'],
                    'trip_tag': 'Primary',
                    'dist':   c['dist'],
                    'volume': round(c['vol'], 2),
                    'vpt':               round_to_nearest_15(vpt),
                    'dispatch_from_dock': round_to_nearest_15(load_end),
                    'dispatch_from_source': round_to_nearest_15(disp),
                    'load_hrs': c['load_hrs'],
                    'Source_Hold_Hrs':    round(source_hold, 2),
                    'Inter_Hop_Hold_Hrs': round(ih_hold, 2),
                    'Total_Hold_Hrs':     round(source_hold + ih_hold, 2),
                    **hop_cols,
                    'return_time': round_to_nearest_15(ret_time)
                })
    else:
        print("❌ No Primary Solution Found.")
    _t6 = time.perf_counter()
    print(f"  ⏱️  [Stage 5 — CP-SAT solver] {_t6-_t5:.1f}s")
    _stage_times['5_cpsat_solver'] = _t6 - _t5
    save_cache(route_cache)
    return results, base_orders, dropped_ixs, df_trucks
# ---------------------------------------------------------
# 4. AD-HOC TRIP GENERATOR
# ---------------------------------------------------------
def generate_adhoc_trips(dropped_ixs, base_orders, df_trucks, src, base_d):
    """
    For each dropped order, attempt a 2nd / ad-hoc trip using the
    smallest permissible truck. Returns (adhoc_results, unserviceable_list).
    """
    adhoc_results    = []
    unserviceable    = []
    if not dropped_ixs:
        return adhoc_results, unserviceable
    print(f"\n🔁 Attempting Ad-Hoc trips for {len(dropped_ixs)} dropped order(s)...")
    for o_idx in dropped_ixs:
        ord = base_orders[o_idx]
        store_id = ord['id']
        req_totes = ord['vol'] / 20
        opts = df_trucks[
            (df_trucks['cap'] >= req_totes) &
            (df_trucks['size_int'] <= ord['max_ft'])
        ].sort_values('cap')
        if opts.empty:
            print(f"   ❌ {store_id}: No truck can carry {req_totes:.1f} totes within {ord['max_ft']}FT limit → UNSERVICEABLE")
            unserviceable.append({
                'Store_ID':  store_id,
                'Volume':    ord['vol'],
                'Totes':     round(req_totes, 2),
                'Max_FT':    ord['max_ft'],
                'Reason':    f"No truck fits volume ({req_totes:.1f} totes) within {ord['max_ft']}FT",
                'Window_Start': ord['w_start'],
                'Window_End':   ord['w_end']
            })
            continue
        truck = opts.iloc[0]['type']
        load_hrs = round_up_025(req_totes / 100)
        tot_dur  = load_hrs + ord['transit_out'] + load_hrs + ord['transit_ret']
        if tot_dur > 24.0:
            print(f"   ❌ {store_id}: Ad-hoc trip exceeds 24h ({tot_dur:.2f}h) → UNSERVICEABLE")
            unserviceable.append({
                'Store_ID':  store_id,
                'Volume':    ord['vol'],
                'Totes':     round(req_totes, 2),
                'Max_FT':    ord['max_ft'],
                'Reason':    f"Trip duration {tot_dur:.2f}h exceeds 24h limit",
                'Window_Start': ord['w_start'],
                'Window_End':   ord['w_end']
            })
            continue
        earliest_dispatch = floor_to_15_min(ord['w_start'] - timedelta(hours=ord['transit_out']))
        latest_dispatch   = floor_to_15_min(ord['w_end']   - timedelta(hours=ord['transit_out']))
        if earliest_dispatch > latest_dispatch:
            print(f"   ❌ {store_id}: No valid dispatch window for ad-hoc → UNSERVICEABLE")
            unserviceable.append({
                'Store_ID':  store_id,
                'Volume':    ord['vol'],
                'Totes':     round(req_totes, 2),
                'Max_FT':    ord['max_ft'],
                'Reason':    "No valid dispatch window within time constraints",
                'Window_Start': ord['w_start'],
                'Window_End':   ord['w_end']
            })
            continue
        disp     = earliest_dispatch
        vpt      = round_to_nearest_15(disp - timedelta(hours=load_hrs))
        arr1     = round_to_nearest_15(disp + timedelta(hours=ord['transit_out']))
        ret_time = round_to_nearest_15(arr1  + timedelta(hours=load_hrs + ord['transit_ret']))
        dist = ord['dist_out'] + ord['dist_ret']
        print(f"   ✅ {store_id}: Ad-hoc trip → {truck} | VPT {vpt.strftime('%H:%M')} | Dispatch {disp.strftime('%H:%M')} | Return {ret_time.strftime('%H:%M')}")
        adhoc_results.append({
            'type':  'Direct',
            'route': store_id,
            'truck': truck,
            'trip_tag': 'Ad-Hoc',
            'dist':   dist,
            'volume': round(ord['vol'], 2),
            'vpt':               vpt,
            'dispatch_from_dock': round_to_nearest_15(vpt + timedelta(hours=load_hrs)),
            'dispatch_from_source': disp,
            'load_hrs': load_hrs,
            'Source_Hold_Hrs':    0.0,
            'Inter_Hop_Hold_Hrs': 0.0,
            'Total_Hold_Hrs':     0.0,
            'hop1':        arr1,
            'hop2':        None,
            'return_time': ret_time
        })
    return adhoc_results, unserviceable
# ---------------------------------------------------------
# 5. FLEET ROTATION
# ---------------------------------------------------------
def run_fleet_rotation(schedule, initial_fleet=None):
    if initial_fleet is None: initial_fleet = []
    print("🔄 Calculating Fleet Rotation...")
    schedule.sort(key=lambda x: x['vpt'])
    fleet  = initial_fleet.copy()
    max_id = max([t['id'] for t in fleet]) if fleet else 0
    for trip in schedule:
        needed = trip['truck']
        start  = trip['vpt']
        ret    = trip['return_time']
        assigned = None
        for truck in fleet:
            if truck['type'] == needed:
                if truck['available_at'] + timedelta(minutes=BUFFER_MINS) <= start:
                    assigned = truck
                    break
        if assigned:
            assigned['trips'].append(trip)
            assigned['available_at'] = ret
        else:
            max_id += 1
            fleet.append({'id': max_id, 'type': needed, 'available_at': ret, 'trips': [trip]})
    return fleet
