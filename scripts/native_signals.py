"""DAQ analog signals for fullnet scenarios: physical values -> device codes, and the 2026-11-16 test-fire input
patterns (DAQ-Board tests/fixtures/input_faults_fire_20261116.json, recorded by the station).

Device codes follow the DAQ's own conversions (current dev, load cells in volts):
  MCP3564R (spi2): pin V = code * 2.4 V / 2^23; CH9 = P6 differential at x16; CH6/7 = connector / 11.
                   Codes are 24-bit two's complement, or 0x1000000 + c for a positive overrange code c
                   (the DAQ decodes 25-bit: the KG1000 amplifier clipped at +0xB40000 = 3.375 V).
  ADC1 (14-bit, 3.3 V): VMON (channel 17) = VMAIN / 11.
Natively the codes are applied at their virtual times through diagnostics/native-signals.txt; on Renode the same
schedule is replayed into the stimulus files at the controller's polling resolution.
"""
from __future__ import annotations
import json
from pathlib import Path

MCP_VREF = 2.4
P6_GAIN = 16.0
ADC1_VREF = 3.3
ADC1_FULL = 16383
VMON_CHANNEL = 17
KG1000, KG50_AMP2, P6_SPLUS, P6_SMINUS, CHAMBER, P6_DIFF = 0, 1, 2, 3, 6, 9


def mcp_code(volts: float) -> int:
    c = round(volts * (1 << 23) / MCP_VREF)
    if c > 0x7FFFFF:
        return 0x1000000 + min(c, 0xFFFFFF)  # positive overrange (25-bit)
    return c & 0xFFFFFF


def adc1_vmon_code(vmain_mv: float) -> int:
    return max(0, min(ADC1_FULL, round(vmain_mv / 11.0 / 1000.0 / ADC1_VREF * ADC1_FULL)))


def nominal_mcp(base: list[int]) -> list[int]:
    """Healthy front end on top of the load-cell fixture codes: P6 legs at ~1.137 V each, chamber at 0.5 V."""
    codes = list(base)
    codes[P6_SPLUS] = mcp_code(1.1370); codes[P6_SMINUS] = mcp_code(1.1355)
    codes[P6_DIFF] = mcp_code((1.1370 - 1.1355) * P6_GAIN)
    codes[CHAMBER] = mcp_code(0.5 / 11.0)
    codes[KG1000] = mcp_code(0.209)
    return codes


def fixture(board_src: Path) -> dict:
    return json.loads((board_src / 'tests/fixtures/input_faults_fire_20261116.json').read_text())


def patterns(fx: dict):
    """name -> (expected station fault key, [(t_ms, {'mcp': {ch: code}, 'adc1': {ch: code}})])"""
    out = {}
    out['vmain_boot_low'] = ('supply_low', [(t, {'adc1': {VMON_CHANNEL: adc1_vmon_code(mv)}}) for t, mv in fx['vmain_mv_0_14s']])
    out['vmain_dips'] = ('supply_low', [(t, {'adc1': {VMON_CHANNEL: adc1_vmon_code(mv)}}) for t, mv in fx['vmain_mv_320_460s'] if t <= 30000])
    out['kg1000_clip'] = ('kg1000_high_rail', [(t, {'mcp': {KG1000: mcp_code(uv * 1e-6)}}) for t, uv in fx['kg1000_pin_uv_325_336s']])
    out['kg1000_rail_code'] = ('kg1000_high_rail', [(0, {'mcp': {KG1000: 0x1000000 + 0xB40000}}), (3000, {'mcp': {KG1000: mcp_code(0.209)}})])
    out['kg1000_low_rail'] = ('kg1000_low_rail', [(t, {'mcp': {KG1000: mcp_code(uv * 1e-6)}}) for t, uv in fx['kg1000_pin_uv_1177_1180s']])
    out['p6_splus_dropout'] = ('p6_leg_imbalance', [(t, {'mcp': {P6_SPLUS: mcp_code(a * 1e-6), P6_SMINUS: mcp_code(b * 1e-6),
                                                                 P6_DIFF: mcp_code((a - b) * 1e-6 * P6_GAIN)}})
                                                    for t, a, b in fx['p6_legs_uv_449_459s']])
    out['dead_chamber'] = ('chamber_no_signal', [(t, {'mcp': {CHAMBER: mcp_code(uv * 1e-6 / 11.0)}}) for t, uv in fx['ch6_connector_uv_0_20s']])
    return out
