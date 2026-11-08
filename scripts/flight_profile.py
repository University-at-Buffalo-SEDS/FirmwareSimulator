#!/usr/bin/env python3
"""Deterministic vertical flight profile for the FC sensor stimulus.

The profile is time-indexed from ignition (T0) and is consumed by the Renode
models in renode/peripherals/SedsSpiSensors.cs (BMP390 pressure/temperature,
BMI088 accelerometer/gyro on the FC, NEO-M9N NMEA on the RF board). It is an
open-loop trajectory: parachute openings happen at nominal times/altitudes and
do not depend on the firmware's own deployment pulses.

Phases: pad -> boost (constant thrust) -> coast with drag -> apogee -> short
free fall -> drogue -> main at the FC's REEF_TARGET_ALT -> touchdown -> rest.

Units: metres, seconds, m/s, m/s^2, Pa, degrees C, degrees.
Body z is up (the vehicle flies vertically without rotation), so the
accelerometer reads the specific force f = a + g along +z.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

G = 9.80665
PAD_MSL = 890.016           # fcconfig.h LAUNCH_SITE_SEA
SITE = (31.758, -102.046)   # fcconfig.h LAUNCH_SITE_LAT/LON
REEF_TARGET_ALT = 457.2     # fcconfig.h
FC_ALT_CONST = 44330.0      # barometer.h ALT_PRES_CONST
FC_HYPS_EXP = 0.1903        # barometer.h BARO_HYPSOMETRIC_EXPONENT

DEFAULTS = dict(
    dt=0.001, row_ms=20, burn_s=3.6, thrust_g=9.5, target_apogee_m=2900.0,
    freefall_s=1.5, drogue_vt=6.0, drogue_inflate_s=1.0, main_alt_m=445.0,
    main_vt=4.0, main_inflate_s=1.5, wind_east=1.5, wind_north=0.4,
    tail_s=420.0,
)


def std_pressure(h_msl: float) -> float:
    return 101325.0 * (1 - 2.25577e-5 * h_msl) ** 5.25588


def fc_pressure(h_agl: float, p0: float) -> float:
    """Pressure that the FC's own hypsometric formula maps back to h_agl."""
    return p0 * (1 - h_agl / FC_ALT_CONST) ** (1 / FC_HYPS_EXP)


def density(h_msl: float) -> float:
    t = 288.15 - 0.0065 * h_msl
    return std_pressure(h_msl) / (287.05 * t)


def simulate(cfg: dict, k_body: float):
    """Return per-step rows (t, h, v, a, f, phase) for a given body drag constant.

    k is drag/(m * rho) per unit v^2: drag acceleration = k * rho * v^2.
    """
    dt = cfg['dt']
    t = h = v = 0.0
    phase = 'pad'
    rows = []
    t_apogee = t_drogue = t_main = t_land = None
    k = k_body
    k_drogue = G / (density(PAD_MSL + 1500) * cfg['drogue_vt'] ** 2)
    k_main = G / (density(PAD_MSL + 200) * cfg['main_vt'] ** 2)
    while True:
        rho = density(PAD_MSL + max(h, 0))
        if phase in ('pad', 'boost') and t < cfg['burn_s']:
            phase = 'boost'
            thrust = cfg['thrust_g'] * G
        else:
            thrust = 0.0
            if phase == 'boost':
                phase = 'coast'
        if phase == 'coast' and v <= 0:
            phase, t_apogee = 'freefall', t
        if phase == 'freefall' and t - t_apogee >= cfg['freefall_s']:
            phase, t_drogue = 'drogue', t
        if phase == 'drogue' and h <= cfg['main_alt_m']:
            phase, t_main = 'main', t
        if phase == 'drogue':
            blend = min(1.0, (t - t_drogue) / cfg['drogue_inflate_s'])
            k = k_body + blend * (k_drogue - k_body)
        elif phase == 'main':
            blend = min(1.0, (t - t_main) / cfg['main_inflate_s'])
            k = k_drogue + blend * (k_main - k_drogue)
        drag = -math.copysign(k * rho * v * v, v)
        a = thrust + drag - G
        if phase == 'landed':
            a = 0.0
        f = a + G
        rows.append((t, h, v, a, f, phase))
        if phase == 'landed' and t - t_land >= cfg['tail_s']:
            break
        if phase != 'landed':
            v += a * dt
            h += v * dt
            if h <= 0 and phase in ('main', 'drogue', 'freefall'):
                h = v = 0.0
                phase, t_land = 'landed', t
        t = round(t + dt, 9)
        if t > 3600:
            raise RuntimeError('profile did not land')
    return rows, dict(apogee_s=t_apogee, drogue_s=t_drogue, main_s=t_main, landing_s=t_land)


def tune(cfg: dict) -> float:
    lo, hi = 1e-6, 1e-2
    for _ in range(60):
        mid = math.sqrt(lo * hi)
        apogee = coast_only(cfg, mid)
        if apogee > cfg['target_apogee_m']:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


def coast_only(cfg: dict, k: float) -> float:
    dt = cfg['dt']
    t = h = v = 0.0
    while True:
        rho = density(PAD_MSL + h)
        thrust = cfg['thrust_g'] * G if t < cfg['burn_s'] else 0.0
        a = thrust - math.copysign(k * rho * v * v, v) - G
        v += a * dt
        h += v * dt
        t += dt
        if t > cfg['burn_s'] and v <= 0:
            return h


def build(cfg: dict) -> tuple[list[dict], dict]:
    k_body = tune(cfg)
    steps, events = simulate(cfg, k_body)
    p0 = std_pressure(PAD_MSL)
    every = int(round(cfg['row_ms'] / 1000 / cfg['dt']))
    rows = []
    east = north = 0.0
    last_t = 0.0
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(SITE[0]))
    for index, (t, h, v, a, f, phase) in enumerate(steps):
        if phase not in ('pad', 'landed'):
            # Horizontal drift with the wind once airborne; none on the ground.
            east += cfg['wind_east'] * (t - last_t)
            north += cfg['wind_north'] * (t - last_t)
        last_t = t
        if index % every:
            continue
        h_msl = PAD_MSL + h
        rows.append(dict(
            t_ms=int(round(t * 1000)), alt_agl_m=h, vel_mps=v, acc_mps2=a, specific_force_z_mps2=f,
            pressure_pa=fc_pressure(h, p0), temp_c=15.0 - 0.0065 * h_msl,
            gyro_x_dps=0.0, gyro_y_dps=0.0, gyro_z_dps=0.0,
            lat=SITE[0] + north / m_per_deg_lat, lon=SITE[1] + east / m_per_deg_lon, alt_msl_m=h_msl, phase=phase))
    # Expected maxima from the full-resolution trajectory.
    peak_h = max(steps, key=lambda r: r[1])
    peak_v = max(steps, key=lambda r: abs(r[2]))
    peak_a = max(steps, key=lambda r: abs(r[3]))
    peak_f = max(steps, key=lambda r: abs(r[4]))
    burnout = max(r for r in steps if r[5] == 'boost')
    summary = dict(
        generator='scripts/flight_profile.py', config=cfg, body_drag_k=k_body, pad_pressure_pa=p0, pad_msl_m=PAD_MSL,
        launch_site=SITE, reef_target_alt_m=REEF_TARGET_ALT,
        events_s=dict(ignition=0.0, burnout=burnout[0], **events),
        expected_maxima=dict(
            max_altitude_agl_m=peak_h[1], max_altitude_t_plus_s=peak_h[0],
            max_altitude_msl_m=PAD_MSL + peak_h[1],
            max_speed_mps=abs(peak_v[2]), max_speed_t_plus_s=peak_v[0],
            max_acceleration_mps2=abs(peak_a[3]), max_acceleration_g=abs(peak_a[3]) / G, max_acceleration_t_plus_s=peak_a[0],
            max_specific_force_mps2=abs(peak_f[4]), max_specific_force_g=abs(peak_f[4]) / G, max_specific_force_t_plus_s=peak_f[0],
            burnout_velocity_mps=burnout[2], burnout_altitude_m=burnout[1]),
        descent=dict(drogue_terminal_mps=cfg['drogue_vt'], main_terminal_mps=cfg['main_vt'], main_open_alt_m=cfg['main_alt_m']),
        rows=len(rows), row_ms=cfg['row_ms'],
    )
    return rows, summary


COLUMNS = ['t_ms', 'alt_agl_m', 'vel_mps', 'acc_mps2', 'specific_force_z_mps2', 'pressure_pa', 'temp_c',
           'gyro_x_dps', 'gyro_y_dps', 'gyro_z_dps', 'lat', 'lon', 'alt_msl_m', 'phase']


def write(rows: list[dict], summary: dict, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'flight-profile.csv').open('w') as handle:
        handle.write('# SEDS flight profile v1; t_ms from ignition (T0); see flight-profile.json\n')
        handle.write(','.join(COLUMNS) + '\n')
        for row in rows:
            handle.write(','.join(row['phase'] if c == 'phase' else (str(row[c]) if c == 't_ms' else f'{row[c]:.6f}' if c not in ('lat', 'lon') else f'{row[c]:.7f}') for c in COLUMNS) + '\n')
    (out / 'flight-profile.json').write_text(json.dumps(summary, indent=2) + '\n')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out', type=Path, required=True)
    for key, value in DEFAULTS.items():
        p.add_argument('--' + key.replace('_', '-'), type=type(value), default=value)
    args = p.parse_args()
    cfg = {key: getattr(args, key) for key in DEFAULTS}
    rows, summary = build(cfg)
    write(rows, summary, args.out)
    print(json.dumps({'events_s': summary['events_s'], 'expected_maxima': summary['expected_maxima']}, indent=2))


if __name__ == '__main__':
    main()
