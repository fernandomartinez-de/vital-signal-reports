"""
build_dashboards.py

Pulls WHOOP biometric data from Supabase, computes 12 derived metrics,
and renders two Spanish HTML dashboards (Javier Nutriologo, Doctora Escobar Oncologa).

Usage:
    python build_dashboards.py
    python build_dashboards.py --days 365
    python build_dashboards.py --end-date 2026-04-25

Environment variables:
    SUPABASE_DB_URL    Required. Full postgres connection string.
                       Example: postgresql://postgres.xxxxx:PASSWORD@aws-1-us-east-2.pooler.supabase.com:6543/postgres

Inputs (in same directory):
    nutritionist_template.html    Template with __DATA_PLACEHOLDER__ token
    oncologist_template.html      Template with __DATA_PLACEHOLDER__ token

Outputs (in same directory):
    martinez_nutritionist_dashboard.html
    martinez_oncologist_dashboard.html
"""

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, stdev

import psycopg2
import psycopg2.extras


# =============================================================================
# CONFIGURATION
# =============================================================================

PATIENT_NAME = "Fernando Martinez"
HEIGHT_M = 1.66   # updated from InBody 25 may 2026
WEIGHT_KG = 65.2  # updated from InBody 25 may 2026
MAX_HR = 196
DEFAULT_LOOKBACK_DAYS = 365

# InBody measurements pulled from Supabase at runtime (see fetch_inbody_data)
INBODY_DATA = []  # populated at runtime

# HR zone anchors (% of max HR). Standard 5-zone model.
HR_ZONES = {
    "Z1": (0.50, 0.60),
    "Z2": (0.60, 0.70),
    "Z3": (0.70, 0.80),
    "Z4": (0.80, 0.90),
    "Z5": (0.90, 1.00),
}

SCRIPT_DIR = Path(__file__).parent.resolve()


# =============================================================================
# DATABASE
# =============================================================================

def get_connection():
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        sys.exit("ERROR: SUPABASE_DB_URL environment variable not set")
    return psycopg2.connect(db_url)


def fetch_inbody_data(conn):
    """Pull all InBody measurements from Supabase, ordered by date."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT fecha, peso, mme, masa_grasa, pgc, mlg, agua, tmb, score,
               angulo_fase, grasa_visceral, rel_cintura_cadera, imc,
               peso_ideal, control_peso, control_grasa, control_musculo,
               dispositivo, proveedor
        FROM inbody_results
        ORDER BY fecha ASC
    """)
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        out.append({
            "fecha": r["fecha"].isoformat(),
            "peso": float(r["peso"]) if r["peso"] else None,
            "mme": float(r["mme"]) if r["mme"] else None,
            "masa_grasa": float(r["masa_grasa"]) if r["masa_grasa"] else None,
            "pgc": float(r["pgc"]) if r["pgc"] else None,
            "mlg": float(r["mlg"]) if r["mlg"] else None,
            "agua": float(r["agua"]) if r["agua"] else None,
            "tmb": int(r["tmb"]) if r["tmb"] else None,
            "score": int(r["score"]) if r["score"] else None,
            "angulo_fase": float(r["angulo_fase"]) if r["angulo_fase"] else None,
            "grasa_visceral": int(r["grasa_visceral"]) if r["grasa_visceral"] else None,
            "rel_cintura_cadera": float(r["rel_cintura_cadera"]) if r["rel_cintura_cadera"] else None,
            "imc": float(r["imc"]) if r["imc"] else None,
            "peso_ideal": float(r["peso_ideal"]) if r["peso_ideal"] else None,
            "control_peso": float(r["control_peso"]) if r["control_peso"] else None,
            "control_grasa": float(r["control_grasa"]) if r["control_grasa"] else None,
            "control_musculo": float(r["control_musculo"]) if r["control_musculo"] else None,
            "dispositivo": r["dispositivo"] or "InBody270S",
        })
    return out


def fetch_data(conn, start_date, end_date):
    """Pull all WHOOP tables filtered to the date window."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Cycles: daily strain and energy
    cur.execute("""
        SELECT
            start_time::date AS d,
            strain,
            kilojoules,
            average_heart_rate,
            max_heart_rate
        FROM whoop_cycles
        WHERE start_time::date BETWEEN %s AND %s
        ORDER BY d
    """, (start_date, end_date))
    cycles = cur.fetchall()

    # Recovery: HRV, RHR, SpO2, skin temp
    cur.execute("""
        SELECT
            recovery_date AS d,
            recovery_score,
            hrv_rmssd_milli AS hrv,
            resting_heart_rate AS rhr,
            spo2_percentage AS spo2,
            skin_temp_celsius AS skin_temp
        FROM whoop_recovery
        WHERE recovery_date BETWEEN %s AND %s
        ORDER BY d
    """, (start_date, end_date))
    recovery = cur.fetchall()

    # Sleep: stages, performance, efficiency (columns are in minutes not millis)
    cur.execute("""
        SELECT
            start_time::date AS d,
            (light_sleep_minutes + slow_wave_sleep_minutes + rem_sleep_minutes + awake_minutes) AS in_bed_min,
            awake_minutes,
            light_sleep_minutes AS light_min,
            slow_wave_sleep_minutes AS deep_min,
            rem_sleep_minutes AS rem_min,
            performance_percentage AS performance,
            sleep_efficiency_percentage AS efficiency,
            end_time AS end_ts
        FROM whoop_sleep
        WHERE start_time::date BETWEEN %s AND %s
        ORDER BY d
    """, (start_date, end_date))
    sleep = cur.fetchall()

    # Workouts: sport, strain, HR, duration
    cur.execute("""
        SELECT
            start_time::date AS d,
            sport_name,
            strain,
            kilojoules,
            average_heart_rate,
            max_heart_rate,
            start_time AS start_ts,
            end_time AS end_ts
        FROM whoop_workouts
        WHERE start_time::date BETWEEN %s AND %s
        ORDER BY start_time
    """, (start_date, end_date))
    workouts = cur.fetchall()

    cur.close()
    return {
        "cycles": cycles,
        "recovery": recovery,
        "sleep": sleep,
        "workouts": workouts,
    }


# =============================================================================
# HELPERS
# =============================================================================

def kj_to_kcal(kj):
    """WHOOP reports energy in kilojoules. Convert to kilocalories."""
    if kj is None:
        return None
    return round(float(kj) / 4.184, 1)


def ms_to_min(ms):
    if ms is None:
        return None
    return round(ms / 60000, 1)


def safe_round(x, digits=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), digits)


def rolling_mean(values, window):
    """Rolling mean with center alignment. Returns list same length as input."""
    out = []
    for i in range(len(values)):
        start = max(0, i - window + 1)
        sub = [v for v in values[start:i+1] if v is not None]
        out.append(sum(sub) / len(sub) if sub else None)
    return out


def rolling_baseline_30d(values):
    """30-day rolling mean, used as baseline for skin temp deviation."""
    return rolling_mean(values, 30)


# =============================================================================
# 12 DERIVED METRICS
# =============================================================================

def compute_rmr(weight_kg, height_m, age=40, sex="male"):
    """
    Mifflin-St Jeor RMR estimate. Single value applied as constant baseline.
    Conservative default age 40, can be parameterized.
    """
    h_cm = height_m * 100
    if sex == "male":
        return 10 * weight_kg + 6.25 * h_cm - 5 * age + 5
    return 10 * weight_kg + 6.25 * h_cm - 5 * age - 161


def compute_neat_kcal_per_day():
    """
    NEAT (non-exercise activity thermogenesis) approximation.
    For a moderately active office worker, ~400-600 kcal/day above RMR.
    Using 500 as a midrange constant.
    """
    return 500


def compute_workout_kcal_per_day(workouts_by_day, all_dates):
    """Metric 4 component: total workout kcal per day."""
    out = []
    for d in all_dates:
        day_total = sum(
            kj_to_kcal(w["kilojoules"]) or 0
            for w in workouts_by_day.get(d, [])
            if w["kilojoules"] is not None
        )
        out.append(round(day_total, 1) if day_total > 0 else 0)
    return out


def compute_skin_temp_deviation(skin_temps):
    """Metric 1: skin temp minus 30-day rolling baseline."""
    baseline = rolling_baseline_30d(skin_temps)
    deviations = []
    for v, b in zip(skin_temps, baseline):
        if v is None or b is None:
            deviations.append(None)
        else:
            deviations.append(round(v - b, 2))
    return deviations


def compute_autonomic_stress_index(rhr_list, hrv_list):
    """Metric 2: RHR divided by HRV. Higher = more autonomic stress."""
    out = []
    for rhr, hrv in zip(rhr_list, hrv_list):
        if rhr is None or hrv is None or hrv == 0:
            out.append(None)
        else:
            out.append(round(rhr / hrv, 2))
    return out


def compute_acwr(strain_list):
    """
    Metric 3: Acute:Chronic Workload Ratio.
    Acute = 7-day rolling mean strain.
    Chronic = 28-day rolling mean strain.
    Sweet spot: 0.8-1.3. Over: >1.5. Under: <0.8.
    """
    acute = rolling_mean(strain_list, 7)
    chronic = rolling_mean(strain_list, 28)
    out = []
    for a, c in zip(acute, chronic):
        if a is None or c is None or c == 0:
            out.append(None)
        else:
            out.append(round(a / c, 2))
    return out


def compute_rmr_neat_baseline():
    """Metric 4 component: RMR + NEAT, daily baseline burn."""
    rmr = compute_rmr(WEIGHT_KG, HEIGHT_M)
    neat = compute_neat_kcal_per_day()
    return round(rmr + neat, 0)


def compute_restorative_sleep_pct(sleep_rows):
    """
    Metric 5: (deep + REM) / total sleep.
    Returns pct (0-100).
    """
    out = []
    for s in sleep_rows:
        deep = s["deep_min"] or 0
        rem = s["rem_min"] or 0
        light = s.get("light_min") or 0
        total = deep + rem + light
        if total == 0:
            out.append(None)
        else:
            out.append(round(100 * (deep + rem) / total, 1))
    return out


def compute_sleep_regularity_weekly(sleep_rows, all_dates):
    """
    Metric 6: weekly SD of wake hour (hour of day, fractional).
    Returns one value per ISO week aligned to all_dates, but for
    rendering simplicity we return a per-day list where each day
    carries its week's SD.
    """
    # Map date string -> wake hour (fractional)
    wake_by_date = {}
    for s in sleep_rows:
        if s["end_ts"] is None:
            continue
        dt = s["end_ts"]
        wake_hour = dt.hour + dt.minute / 60 + dt.second / 3600
        wake_by_date[s["d"].isoformat() if hasattr(s["d"], "isoformat") else str(s["d"])] = wake_hour

    # Group all_dates by ISO week, compute SD per week
    week_sds = {}
    week_buckets = {}
    for d_str in all_dates:
        d = date.fromisoformat(d_str)
        iso_year, iso_week, _ = d.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        if d_str in wake_by_date:
            week_buckets.setdefault(key, []).append(wake_by_date[d_str])

    for key, vals in week_buckets.items():
        if len(vals) >= 2:
            week_sds[key] = round(stdev(vals), 2)
        else:
            week_sds[key] = None

    # Map back to per-day
    out = []
    for d_str in all_dates:
        d = date.fromisoformat(d_str)
        iso_year, iso_week, _ = d.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        out.append(week_sds.get(key))
    return out


def compute_wake_hour(sleep_rows, all_dates):
    """Metric 11 helper: wake hour per day (for circadian charts)."""
    wake_by_date = {}
    for s in sleep_rows:
        if s["end_ts"] is None:
            continue
        dt = s["end_ts"]
        wake_hour = dt.hour + dt.minute / 60
        d_str = s["d"].isoformat() if hasattr(s["d"], "isoformat") else str(s["d"])
        wake_by_date[d_str] = round(wake_hour, 2)
    return [wake_by_date.get(d_str) for d_str in all_dates]


def compute_workout_zone_distribution(workout):
    """
    Metric 7: zone distribution. Zone columns no longer exist in schema.
    Returns None gracefully.
    """
    return None


def compute_workout_duration_min(workout):
    """Metric 12: workout duration in minutes from start/end timestamps."""
    if workout["start_ts"] is None or workout["end_ts"] is None:
        return None
    delta = workout["end_ts"] - workout["start_ts"]
    return round(delta.total_seconds() / 60, 1)


# =============================================================================
# DATA ASSEMBLY
# =============================================================================

def build_daily_series(raw, start_date, end_date):
    """Assemble per-day arrays aligned to a continuous date axis."""
    # Generate continuous date axis
    days = []
    d = start_date
    while d <= end_date:
        days.append(d.isoformat())
        d += timedelta(days=1)

    # Index raw data by date string
    cycles_by_date = {
        (c["d"].isoformat() if hasattr(c["d"], "isoformat") else str(c["d"])): c
        for c in raw["cycles"]
    }
    recovery_by_date = {
        (r["d"].isoformat() if hasattr(r["d"], "isoformat") else str(r["d"])): r
        for r in raw["recovery"]
    }
    sleep_by_date = {
        (s["d"].isoformat() if hasattr(s["d"], "isoformat") else str(s["d"])): s
        for s in raw["sleep"]
    }
    workouts_by_date = {}
    for w in raw["workouts"]:
        key = w["d"].isoformat() if hasattr(w["d"], "isoformat") else str(w["d"])
        workouts_by_date.setdefault(key, []).append(w)

    # Build aligned series
    strain = [cycles_by_date[d]["strain"] if d in cycles_by_date else None for d in days]
    kcal_total = [
        kj_to_kcal(cycles_by_date[d]["kilojoules"]) if d in cycles_by_date else None
        for d in days
    ]
    workout_kcal = compute_workout_kcal_per_day(workouts_by_date, days)
    rmr_neat = [compute_rmr_neat_baseline()] * len(days)

    rec_score = [recovery_by_date[d]["recovery_score"] if d in recovery_by_date else None for d in days]
    hrv = [recovery_by_date[d]["hrv"] if d in recovery_by_date else None for d in days]
    rhr = [recovery_by_date[d]["rhr"] if d in recovery_by_date else None for d in days]
    spo2 = [recovery_by_date[d]["spo2"] if d in recovery_by_date else None for d in days]
    skin_temp = [recovery_by_date[d]["skin_temp"] if d in recovery_by_date else None for d in days]

    skin_temp_dev = compute_skin_temp_deviation(skin_temp)
    autonomic = compute_autonomic_stress_index(rhr, hrv)
    acwr = compute_acwr(strain)

    sleep_min = []
    sleep_perf = []
    sleep_eff = []
    deep_min = []
    rem_min = []
    sleep_rows_aligned = []
    for d in days:
        s = sleep_by_date.get(d)
        sleep_rows_aligned.append(s)
        if s is None:
            sleep_min.append(None)
            sleep_perf.append(None)
            sleep_eff.append(None)
            deep_min.append(None)
            rem_min.append(None)
        else:
            in_bed = s["in_bed_min"] or 0
            awake = s["awake_minutes"] or 0
            sleep_min.append(round(in_bed - awake, 1))
            sleep_perf.append(s["performance"])
            sleep_eff.append(s["efficiency"])
            deep_min.append(s["deep_min"])
            rem_min.append(s["rem_min"])

    restorative_pct = compute_restorative_sleep_pct(
        [s for s in sleep_rows_aligned if s is not None]
    )
    # Re-align to all days
    restorative_aligned = []
    rp_idx = 0
    for s in sleep_rows_aligned:
        if s is None:
            restorative_aligned.append(None)
        else:
            restorative_aligned.append(restorative_pct[rp_idx])
            rp_idx += 1

    wake_hour = compute_wake_hour(raw["sleep"], days)

    return {
        "d": days,
        "st": strain,
        "kc": kcal_total,
        "wk": workout_kcal,
        "rm": rmr_neat,
        "ac": acwr,
        "rs": rec_score,
        "hv": hrv,
        "hr": rhr,
        "so": spo2,
        "sk": skin_temp,
        "sd": skin_temp_dev,
        "as": autonomic,
        "sl": sleep_min,
        "sp": sleep_perf,
        "se": sleep_eff,
        "sw": deep_min,
        "sr": rem_min,
        "rp": restorative_aligned,
        "wh": wake_hour,
    }


def build_workout_series(raw):
    """Assemble flat workout arrays for the workouts chart."""
    out = {"d": [], "sp": [], "st": [], "mn": [], "hr": [], "kc": [], "z": []}
    for w in raw["workouts"]:
        d_str = w["d"].isoformat() if hasattr(w["d"], "isoformat") else str(w["d"])
        out["d"].append(d_str)
        out["sp"].append(w.get("sport_name") or "Unknown")
        out["st"].append(safe_round(w.get("strain"), 1))
        out["mn"].append(compute_workout_duration_min(w))
        out["hr"].append(w.get("average_heart_rate"))
        out["kc"].append(kj_to_kcal(w.get("kilojoules")))
        out["z"].append(compute_workout_zone_distribution(w))
    return out


def build_payload(raw, start_date, end_date, inbody=None):
    """Compose final payload matching the dashboard template schema."""
    daily = build_daily_series(raw, start_date, end_date)
    workouts = build_workout_series(raw)
    return {
        "meta": {
            "patient": PATIENT_NAME,
            "gen": date.today().isoformat(),
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "days": len(daily["d"]),
            "h": HEIGHT_M,
            "w": WEIGHT_KG,
            "mhr": MAX_HR,
            "bmi": round(WEIGHT_KG / (HEIGHT_M ** 2), 1),
        },
        "daily": daily,
        "workouts": workouts,
        "inbody": inbody or [],
    }


# =============================================================================
# RENDER
# =============================================================================

def render_template(template_path, payload, output_path):
    """Inject JSON payload into dashboard's vital-data script tag."""
    if not template_path.exists():
        sys.exit(f"ERROR: template not found: {template_path}")
    template = template_path.read_text(encoding='utf-8')
    payload_json = json.dumps(payload, separators=(",", ":"), default=str)

    # Strategy 1: replace __DATA_PLACEHOLDER__ token (template mode)
    if "__DATA_PLACEHOLDER__" in template:
        rendered = template.replace("__DATA_PLACEHOLDER__", payload_json)

    # Strategy 2: replace contents of <script id="vital-data"> tag (live dashboard mode)
    elif '<script id="vital-data"' in template:
        import re
        rendered = re.sub(
            r'(<script id="vital-data"[^>]*>)(.*?)(</script>)',
            lambda m: m.group(1) + payload_json + m.group(3),
            template,
            flags=re.DOTALL
        )
        if rendered == template:
            sys.exit(f"ERROR: could not replace vital-data content in {template_path.name}")
    else:
        sys.exit(f"ERROR: no injection point found in {template_path.name}")

    output_path.write_text(rendered, encoding='utf-8')
    print(f"  wrote {output_path.name} ({len(rendered):,} bytes)")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Build WHOOP dashboards from Supabase")
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help=f"Lookback window in days (default: {DEFAULT_LOOKBACK_DAYS})")
    parser.add_argument("--end-date", type=str, default=None,
                        help="End date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    end_date = (
        date.fromisoformat(args.end_date) if args.end_date else date.today()
    )
    start_date = end_date - timedelta(days=args.days - 1)

    print(f"Building dashboards for {start_date} to {end_date} ({args.days} days)")

    # 1. Pull from Supabase
    print("Connecting to Supabase...")
    conn = get_connection()
    print("Fetching WHOOP tables...")
    raw = fetch_data(conn, start_date, end_date)
    print(f"  cycles: {len(raw['cycles'])}, recovery: {len(raw['recovery'])}, "
          f"sleep: {len(raw['sleep'])}, workouts: {len(raw['workouts'])}")

    print("Fetching InBody data...")
    inbody = fetch_inbody_data(conn)
    print(f"  {len(inbody)} InBody measurements")
    conn.close()

    # 2. Compute metrics and assemble payload
    print("Computing derived metrics...")
    payload = build_payload(raw, start_date, end_date, inbody=inbody)

    # 3. Render both dashboards
    print("Rendering dashboards...")
    render_template(
        SCRIPT_DIR / "martinez_nutritionist_dashboard.html",
        payload,
        SCRIPT_DIR / "martinez_nutritionist_dashboard.html",
    )
    render_template(
        SCRIPT_DIR / "martinez_oncologist_dashboard.html",
        payload,
        SCRIPT_DIR / "martinez_oncologist_dashboard.html",
    )

    print("Done.")


if __name__ == "__main__":
    main()
