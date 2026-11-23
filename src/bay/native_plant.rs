//! Physical plant for a station-managed native bay (`native.json` "plant"): what the boards' sensors would read
//! given what their actuators do. It closes the loop around the real firmware without touching firmware state:
//!
//! * inputs are the boards' own output pins (MSG_GPIO edges: the AB fill solenoids and igniter, the VB pilot
//!   solenoid, the FC pyro channels) and the VB servo compare registers (TIM2 CCR1/CCR2, read from the node's
//!   mapped peripheral page, as the VB itself reads them back), plus operator inputs from the station's control file
//!   (QD connected, supply bottles open, calibration masses on the load cells, ambient and wind) and injected faults;
//! * outputs are sensor codes the shim delivers to the firmware (VB tank PT on ADC3, DAQ MCP3564R channels for the
//!   KG1000/KG50 load cells and the chamber PT, FC barometer/accelerometer through the "flightsens" device), input
//!   pins (VB umbilical continuity) and CAN link changes (the umbilical separating at liftoff).
//!
//! Fluid, motor, load-cell and atmosphere physics: native_physics.rs (sources and assumptions there). Supply path:
//! nitrous/nitrogen only reach the tank while the AB valve is open AND the QD is connected AND that bottle is open
//! (and not empty) AND the supply is above the tank.
//!
//! Ignition: the igniter must have fired (unless `require_igniter` is off: HITL fires no igniter) and the pilot must
//! open with liquid in the tank. The pilot actuates the main oxidiser valve, assumed to stay open to burnout once the
//! motor lights. On a test stand (`test_stand`) the rocket stays put: thrust unloads the KG50 first and the KG1000
//! sees the rest once the gap closes. In flight, liftoff separates the umbilical; 1-D vertical dynamics (thrust, drag
//! with the ambient-adjusted density, mass flow) and wind drift; the descent follows the FC's own pyro outputs
//! (CO2 = drogue, REEF = main), without them the vehicle falls ballistically. The FC barometer reads true ISA pressure
//! at the geometric altitude (FlightComputer26 baro_agl since 659440e).
use super::native_physics::{self as phys, Fluids, LoadCell, Paths};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;

const G: f64 = phys::G0;

/// One output pin of a board: node, GPIO port index (A = 0) and pin number.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct PinRef {
    pub node: String,
    pub port: u8,
    pub pin: u8,
    /// The level that means "energized / open" (default high).
    #[serde(default = "one")]
    pub active: u8,
}
fn one() -> u8 {
    1
}
fn pin(node: &str, port: u8, pin: u8) -> PinRef {
    PinRef { node: node.into(), port, pin, active: 1 }
}

/// A hobby servo on a timer channel: open while the pulse is below `open_below_us`.
#[derive(Clone, Debug, Deserialize)]
pub struct ServoRef {
    pub node: String,
    pub timer_base: u32,
    pub channel: u8,
    pub open_below_us: f64,
}

/// Which valves pass flow this instant (after faults).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Valves {
    pub nitrogen: bool,
    pub nitrous: bool,
    pub vent: bool,
    pub dump: bool,
    pub pilot: bool,
}

/// Operator inputs outside the boards' control.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Supply {
    pub qd_connected: bool,
    pub nitrogen_bottle_open: bool,
    pub nitrous_bottle_open: bool,
}
impl Default for Supply {
    fn default() -> Self {
        Self { qd_connected: true, nitrogen_bottle_open: true, nitrous_bottle_open: true }
    }
}
impl Supply {
    /// The flow paths the valves and the supply allow.
    pub fn paths(&self, v: Valves) -> Paths {
        Paths {
            nitrous_in: v.nitrous && self.qd_connected && self.nitrous_bottle_open,
            nitrogen_in: v.nitrogen && self.qd_connected && self.nitrogen_bottle_open,
            vent: v.vent,
            dump: v.dump,
            pilot: v.pilot,
        }
    }
}

/// Ambient and weather (control "conditions").
#[derive(Clone, Copy, Debug, PartialEq, serde::Serialize)]
pub struct Conditions {
    pub ambient_c: f64,
    pub wind_mps: f64,
    pub wind_from_deg: f64,
    pub gust_mps: f64,
}
impl Default for Conditions {
    fn default() -> Self {
        // tools/flight_profile nominal: 4 m/s from 300 deg, 1 m/s gusts
        Self { ambient_c: 20.0, wind_mps: 4.0, wind_from_deg: 300.0, gust_mps: 1.0 }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct VehicleParams {
    pub pad_msl_m: f64,
    pub launch_lat: f64,
    pub launch_lon: f64,
    /// GroundStation26 tools/flight_profile/vehicle.json (15 ft x 6 in, 31.6 kg dry, Cd about 0.5).
    pub dry_mass_kg: f64,
    pub fuel_kg: f64,
    pub diameter_m: f64,
    pub cd: f64,
    pub drogue_vt: f64,
    pub main_vt: f64,
    pub inflate_s: f64,
    /// Fill load-cell tare: the hanging mass it was zeroed with (the empty rocket with its fuel).
    pub tare_kg: f64,
}
impl Default for VehicleParams {
    fn default() -> Self {
        Self {
            pad_msl_m: 890.016,
            launch_lat: 31.758,
            launch_lon: -102.046,
            dry_mass_kg: 31.6,
            fuel_kg: 1.1,
            diameter_m: 0.1524,
            cd: 0.5,
            drogue_vt: 25.0,
            main_vt: 5.8,
            inflate_s: 1.0,
            tare_kg: 32.7,
        }
    }
}

pub fn std_pressure(h_msl: f64) -> f64 {
    101325.0 * (1.0 - 2.25577e-5 * h_msl).powf(5.25588)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Pad,
    Boost,
    Coast,
    Descent,
    Landed,
}

/// 1-D vertical flight plus horizontal wind drift.
#[derive(Clone, Debug)]
pub struct Flight {
    pub phase: Phase,
    pub t_launch: f64,
    pub h: f64,
    pub v: f64,
    pub f: f64,
    pub east: f64,
    pub north: f64,
    pub apogee: f64,
    pub drogue_at: Option<f64>,
    pub main_at: Option<f64>,
}
impl Default for Flight {
    fn default() -> Self {
        Self { phase: Phase::Pad, t_launch: 0.0, h: 0.0, v: 0.0, f: G, east: 0.0, north: 0.0, apogee: 0.0, drogue_at: None, main_at: None }
    }
}
impl Flight {
    /// Advance by `dt` with `thrust` (N), vehicle mass `mass` (kg), the deployments seen and the conditions.
    #[allow(clippy::too_many_arguments)]
    pub fn step(&mut self, t: f64, dt: f64, thrust: f64, mass: f64, drogue: bool, main: bool, veh: &VehicleParams, c: &Conditions) {
        if matches!(self.phase, Phase::Pad | Phase::Landed) {
            self.f = G;
            return;
        }
        if drogue && self.drogue_at.is_none() {
            self.drogue_at = Some(t);
        }
        if main && self.main_at.is_none() {
            self.main_at = Some(t);
        }
        if thrust <= 0.0 && self.phase == Phase::Boost && t - self.t_launch > 0.5 {
            self.phase = Phase::Coast;
        }
        let rho = phys::air_density(veh.pad_msl_m + self.h.max(0.0), c.ambient_c, veh.pad_msl_m);
        let area = std::f64::consts::PI * veh.diameter_m * veh.diameter_m / 4.0;
        let mut drag = -0.5 * rho * veh.cd * area * self.v * self.v.abs() / mass;
        let target_vt = if self.main_at.is_some() {
            Some(veh.main_vt)
        } else if self.drogue_at.is_some() {
            Some(veh.drogue_vt)
        } else {
            None
        };
        if let (Some(vt), true) = (target_vt, self.v < 0.0) {
            drag = drag.max(G + (-vt - self.v) / veh.inflate_s.max(0.05));
        }
        let a = thrust / mass + drag - G;
        self.f = thrust / mass + drag;
        self.v += a * dt;
        self.h += self.v * dt;
        if self.h < 0.0 && self.phase == Phase::Boost {
            // thrust below weight right at ignition: still on the rail
            self.h = 0.0;
            self.v = self.v.max(0.0);
        }
        // horizontal: the vehicle drifts with the wind once off the pad (weathercocking not modelled)
        let gust = c.gust_mps * (0.7 * (0.37 * t).sin() + 0.3 * (1.3 * t).sin());
        let w = (c.wind_mps + gust).max(0.0);
        let to = (c.wind_from_deg + 180.0).to_radians();
        self.east += w * to.sin() * dt;
        self.north += w * to.cos() * dt;
        self.apogee = self.apogee.max(self.h);
        if self.phase == Phase::Coast && self.v < 0.0 {
            self.phase = Phase::Descent;
        }
        if self.h <= 0.0 && matches!(self.phase, Phase::Coast | Phase::Descent) {
            self.h = 0.0;
            self.v = 0.0;
            self.f = G;
            self.phase = Phase::Landed;
        }
    }
}

/// VB tank PT (PRESSURE_TRANSDUCER 0..2000 psi over ADC 0..3103).
pub fn vb_pressure_raw(psi: f64) -> i64 {
    (psi * 3103.0 / 2000.0).round().clamp(0.0, 4095.0) as i64
}
/// MCP3564R code for a pin voltage (DAQ: 2.4 V reference, 25-bit positive overrange as the DAQ decodes it).
pub fn mcp_code(volts: f64) -> i64 {
    let c = (volts * f64::from(1u32 << 23) / 2.4).round() as i64;
    if c > 0x7F_FFFF { 0x100_0000 + c.min(0xFF_FFFF) } else { c & 0xFF_FFFF }
}
/// KG1000 amplifier top clamp (the supply-dip signature, DAQ input_faults fixture).
pub const KG1000_CLAMP_CODE: i64 = 0x100_0000 + 0xB4_0000;
/// DAQ MCP3564R channels: KG1000 on CH0 and KG50 on CH1 (ADC-pin volts), healthy P6 legs, the chamber PT on CH6.
pub fn daq_codes(kg1000_v: Option<f64>, kg50_v: f64, chamber_psi: f64) -> Vec<i64> {
    let mut v: Vec<i64> = (0..16).map(|i| ((i + 1) * 0x10_0000) & 0xFF_FFFF).collect();
    for i in 8..12 {
        v[i] = (v[2 * (i - 8)] - v[2 * (i - 8) + 1]) & 0xFF_FFFF;
    }
    v[12] = 305652;
    v[0] = kg1000_v.map(mcp_code).unwrap_or(KG1000_CLAMP_CODE);
    v[1] = mcp_code(kg50_v);
    v[2] = mcp_code(1.1370);
    v[3] = mcp_code(1.1355);
    v[9] = mcp_code((1.1370 - 1.1355) * 16.0);
    v[6] = mcp_code(chamber_psi.max(0.0) / 200.0 / 11.0);
    v
}

/// The DAQ's DAC1 as wired on the board (DAQ-Board daq_dac.c): channel c plays a table from SRAM through GPDMA1
/// channel 10+c on a TIM7/TIM15 trigger, or holds a software value; the P10 buffer doubles it to the connector.
#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct DacParams {
    pub node: String,
    pub dac_base: u32,
    pub dma_channel_base: [u32; 2],
    pub timer_base: [u32; 2],
    /// Timer kernel clock (Hz); `clock_addr` (SystemCoreClock in the DAQ, resolved by the launcher) wins when set.
    pub timer_clock_hz: f64,
    pub clock_addr: Option<u32>,
    pub vref_v: f64,
    pub buffer_gain: f64,
    /// Connector rail of the P10 buffer (the swapped-inputs fault latches to it or to 0 V).
    pub buffer_rail_v: f64,
}
impl Default for DacParams {
    fn default() -> Self {
        Self {
            node: "daq".into(),
            dac_base: 0x4602_1800,
            dma_channel_base: [0x4002_0550, 0x4002_05D0],
            timer_base: [0x4000_1400, 0x4001_4000],
            timer_clock_hz: 160e6,
            clock_addr: None,
            vref_v: 3.3,
            buffer_gain: 2.0,
            buffer_rail_v: 6.4,
        }
    }
}

/// DAC output voltage at the P10 connector for channel `c` (0/1) at time `t`, read from the DAQ's own registers and
/// table (so any waveform the firmware plays, from any table, comes through). None while the channel is off.
pub fn dac_connector_v(o: &dyn Observe, p: &DacParams, c: usize, t: f64, starts: &mut [Option<f64>; 2], swapped: bool) -> Option<f64> {
    let cr = o.word(&p.node, p.dac_base)?;
    let shift = 16 * c as u32;
    if cr & (1 << shift) == 0 {
        starts[c] = None;
        return None;
    }
    let dma_on = cr & (1 << (12 + shift)) != 0;
    let tim = p.timer_base[c];
    let tim_on = o.word(&p.node, tim).unwrap_or(0) & 1 != 0;
    let code = if dma_on && tim_on {
        let ch = p.dma_channel_base[c];
        let bytes = o.word(&p.node, ch + 0x48).unwrap_or(0) & 0xFFFF;
        let table = o.word(&p.node, ch + 0x4C).unwrap_or(0);
        let n = (bytes / 4).max(1);
        let psc = o.word(&p.node, tim + 0x28).unwrap_or(0) as f64;
        let arr = o.word(&p.node, tim + 0x2C).unwrap_or(0) as f64;
        let clock = p.clock_addr.and_then(|a| o.word(&p.node, a)).map(f64::from).filter(|c| *c > 1e6).unwrap_or(p.timer_clock_hz);
        let rate = clock / ((psc + 1.0) * (arr + 1.0));
        let t0 = *starts[c].get_or_insert(t);
        let idx = (((t - t0) * rate).floor() as u64 % u64::from(n)) as u32;
        o.word(&p.node, table + 4 * idx)? & 0xFFF
    } else {
        starts[c] = None;
        o.word(&p.node, p.dac_base + if c == 0 { 0x08 } else { 0x14 })? & 0xFFF
    };
    let pin = f64::from(code) / 4095.0 * p.vref_v;
    Some(if swapped {
        // P10 op-amp inputs swapped (the unreworked board): positive feedback, the output latches to a rail
        if pin > p.vref_v / 2.0 { p.buffer_rail_v } else { 0.0 }
    } else {
        (pin * p.buffer_gain).min(p.buffer_rail_v)
    })
}

/// A DAQ input the DAC output is patched to, and the ADC-pin voltages it puts on the MCP3564R channels
/// (GroundStation26 backend/src/daq_dac/model.rs paths). `attenuation` is the inline attenuator on load-cell inputs.
pub fn patch_pins(target: &str, connector_v: f64, attenuation: f64) -> Option<Vec<(usize, f64)>> {
    let v = connector_v;
    Some(match target {
        "mcp_ch2" => vec![(2, v)],
        "mcp_ch3" => vec![(3, v)],
        "mcp_ch4" => vec![(4, v)],
        "mcp_ch5" => vec![(5, v)],
        "hvain1" => vec![(6, v / 11.0)],
        "hvain2" => vec![(7, v / 11.0)],
        // P6 pin 3 driven, pin 4 at ground: CH2 single-ended and CH9 the x16 differential
        "p6_diff" => vec![(2, v), (3, 0.0), (9, 16.0 * v)],
        "loadcell_kg1000" => vec![(0, 0.218 + 100.9 * v * attenuation)],
        "loadcell_kg50" => vec![(1, 0.600 + 100.9 * v * attenuation)],
        _ => return None,
    })
}
/// The DAC validation targets the plant can patch (SAR inputs and the internal readback are not modelled).
pub const PATCH_TARGETS: [&str; 9] = ["mcp_ch2", "mcp_ch3", "mcp_ch4", "mcp_ch5", "hvain1", "hvain2", "p6_diff", "loadcell_kg1000", "loadcell_kg50"];

#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct PlantConfig {
    pub fluids: phys::FluidParams,
    pub motor: phys::MotorParams,
    pub vehicle: VehicleParams,
    pub kg50: phys::LoadCellParams,
    pub kg1000: phys::LoadCellParams,
    pub nitrogen_valve: PinRef,
    pub nitrous_valve: PinRef,
    pub igniter: PinRef,
    pub pilot_valve: PinRef,
    pub drogue_pyro: PinRef,
    pub main_pyro: PinRef,
    pub vent_servo: ServoRef,
    pub dump_servo: ServoRef,
    pub tank_pt: (String, String),
    pub daq: String,
    pub flight_node: String,
    /// The board that leaves with the rocket on the fill CAN bus, and its umbilical continuity input pin.
    pub umbilical: (String, String),
    pub continuity_pin: Option<(String, u32)>,
    /// The motor lights only after the igniter fired (HITL fires none: the supervisor turns this off there).
    pub require_igniter: bool,
    /// DAQ DAC (signal generator) as the plant reads it back (STM32U585 register map).
    pub dac: DacParams,
    /// Test fire: the rocket stays on the stand (load cells see the thrust, no flight).
    pub test_stand: bool,
    pub qd_connected: bool,
    pub nitrogen_bottle_open: bool,
    pub nitrous_bottle_open: bool,
    pub seed: u64,
}
impl Default for PlantConfig {
    fn default() -> Self {
        Self {
            fluids: phys::FluidParams::default(),
            motor: phys::MotorParams::default(),
            vehicle: VehicleParams::default(),
            kg50: phys::kg50_default(),
            kg1000: phys::kg1000_default(),
            // ActuatorBoard26 main.h: N2_SIG PB13, N20_SIG PB14, IGNITER_SIG PB12
            nitrogen_valve: pin("actuator", 1, 13),
            nitrous_valve: pin("actuator", 1, 14),
            igniter: pin("actuator", 1, 12),
            // ValveBoard26 main.h: Solenoid PB11; servo_driver.h: NO servo TIM2 CH2, dump servo TIM2 CH1, open near
            // 0 degrees (~530 us), closed near 90 degrees (~1200 us) on the 500-2500 us / 270 degree mapping
            pilot_valve: pin("valve", 1, 11),
            vent_servo: ServoRef { node: "valve".into(), timer_base: 0x4000_0000, channel: 2, open_below_us: 860.0 },
            dump_servo: ServoRef { node: "valve".into(), timer_base: 0x4000_0000, channel: 1, open_below_us: 860.0 },
            // FlightComputer26 main.h: CO2 PB5 (drogue), REEF PB6 (main)
            drogue_pyro: pin("flight", 1, 5),
            main_pyro: pin("flight", 1, 6),
            tank_pt: ("valve".into(), "adc3".into()),
            daq: "daq".into(),
            flight_node: "flight".into(),
            umbilical: ("valve".into(), "fdcan2".into()),
            // ValveBoard26 Continuity PB15 (sim/board.json pin 31, high = loop closed)
            continuity_pin: Some(("valve".into(), 31)),
            require_igniter: true,
            dac: DacParams::default(),
            test_stand: false,
            qd_connected: true,
            nitrogen_bottle_open: true,
            nitrous_bottle_open: true,
            seed: 1,
        }
    }
}

/// What the plant asks the coordinator to do.
#[derive(Clone, Debug, PartialEq)]
pub enum Output {
    Adc { node: String, device: String, text: String },
    Pin { node: String, index: u32, value: u8 },
    CanLink { node: String, peripheral: String, connected: bool },
    RadioCut(bool),
}

/// What the coordinator lets the plant observe.
pub trait Observe {
    fn pin(&self, node: &str, port: u8, pin: u8) -> Option<u8>;
    fn word(&self, node: &str, address: u32) -> Option<u32>;
}

#[derive(Clone, Debug, Default)]
pub struct Faults {
    /// valve name -> stuck open (true) or closed (false)
    pub stuck: BTreeMap<String, bool>,
    pub dead_tank_pt: bool,
    pub dead_chamber_pt: bool,
    pub radio_drop: bool,
}

pub struct Plant {
    pub cfg: PlantConfig,
    pub fluids: Fluids,
    pub supply: Supply,
    pub valves: Valves,
    pub flight: Flight,
    pub faults: Faults,
    pub conditions: Conditions,
    pub kg50: LoadCell,
    pub kg1000: LoadCell,
    /// Calibration masses placed on each cell (kg): (KG50, KG1000).
    pub masses: (f64, f64),
    pub thrust: f64,
    chamber_pa: f64,
    burning: bool,
    burned_out: bool,
    gap_closed_at: Option<f64>,
    fuel_left: f64,
    servo_state: BTreeMap<u8, bool>,
    igniter_fired: bool,
    last_t: Option<f64>,
    sent: BTreeMap<String, String>,
    umbilical_attached: bool,
    radio_cut_sent: bool,
    /// DAC channel -> (patched target, attenuation); P10 swapped-inputs fault.
    pub patch: [Option<(String, f64)>; 2],
    pub p10_swapped: bool,
    dac_starts: [Option<f64>; 2],
    pub dac_out: [Option<f64>; 2],
    pub events: Vec<(f64, String)>,
}

impl Plant {
    pub fn new(cfg: PlantConfig) -> Self {
        let conditions = Conditions::default();
        let supply = Supply { qd_connected: cfg.qd_connected, nitrogen_bottle_open: cfg.nitrogen_bottle_open, nitrous_bottle_open: cfg.nitrous_bottle_open };
        Self {
            fluids: Fluids::new(cfg.fluids.clone(), conditions.ambient_c + 273.15),
            kg50: LoadCell::new(cfg.kg50.clone(), cfg.seed.wrapping_mul(7919) + 1),
            kg1000: LoadCell::new(cfg.kg1000.clone(), cfg.seed.wrapping_mul(104_729) + 3),
            fuel_left: cfg.vehicle.fuel_kg,
            cfg,
            supply,
            valves: Valves::default(),
            flight: Flight::default(),
            faults: Faults::default(),
            conditions,
            masses: (0.0, 0.0),
            thrust: 0.0,
            chamber_pa: 0.0,
            burning: false,
            burned_out: false,
            gap_closed_at: None,
            servo_state: BTreeMap::new(),
            igniter_fired: false,
            last_t: None,
            sent: BTreeMap::new(),
            umbilical_attached: true,
            radio_cut_sent: false,
            patch: [None, None],
            p10_swapped: false,
            dac_starts: [None, None],
            dac_out: [None, None],
            events: Vec::new(),
        }
    }

    fn event(&mut self, t: f64, what: impl Into<String>) {
        let what = what.into();
        eprintln!("[native-plant] {t:.3} s {what}");
        self.events.push((t, what));
        if self.events.len() > 64 {
            self.events.remove(0);
        }
    }
    fn active(o: &dyn Observe, p: &PinRef) -> bool {
        o.pin(&p.node, p.port, p.pin).map(|v| v == p.active).unwrap_or(false)
    }
    fn servo_open(&mut self, o: &dyn Observe, s: &ServoRef) -> bool {
        let ccr = o.word(&s.node, s.timer_base + 0x30 + 4 * u32::from(s.channel));
        let arr = o.word(&s.node, s.timer_base + 0x2C);
        if let (Some(ccr), Some(arr)) = (ccr, arr) {
            if ccr != 0 && arr != 0 {
                let us = f64::from(ccr) * 20000.0 / (f64::from(arr) + 1.0);
                self.servo_state.insert(s.channel, us < s.open_below_us);
            }
        }
        // an unpowered servo holds its last position; the plant starts with both open (vented, safe)
        *self.servo_state.get(&s.channel).unwrap_or(&true)
    }
    fn stuck(&self, name: &str, commanded: bool) -> bool {
        *self.faults.stuck.get(name).unwrap_or(&commanded)
    }
    /// Vehicle mass (kg): dry, fuel left, tank contents.
    pub fn vehicle_mass(&self) -> f64 {
        self.cfg.vehicle.dry_mass_kg + self.fuel_left + self.fluids.n2o_kg + self.fluids.n2_kg
    }

    /// Advance the plant to virtual time `t_ns`; returns what changed for the boards.
    pub fn step(&mut self, t_ns: u64, o: &dyn Observe) -> Vec<Output> {
        let t = t_ns as f64 / 1e9;
        let dt = match self.last_t {
            Some(prev) if t > prev => (t - prev).min(0.5),
            Some(_) => return Vec::new(),
            None => 0.0,
        };
        self.last_t = Some(t);
        let cfg = self.cfg.clone();
        let vent = self.servo_open(o, &cfg.vent_servo);
        let dump = self.servo_open(o, &cfg.dump_servo);
        let v = Valves {
            nitrogen: self.stuck("nitrogen", Self::active(o, &cfg.nitrogen_valve)),
            nitrous: self.stuck("nitrous", Self::active(o, &cfg.nitrous_valve)),
            vent: self.stuck("vent", vent),
            dump: self.stuck("dump", dump),
            pilot: self.stuck("pilot", Self::active(o, &cfg.pilot_valve)),
        };
        if v != self.valves {
            let names: Vec<&str> = [("nitrogen", v.nitrogen), ("nitrous", v.nitrous), ("vent", v.vent), ("dump", v.dump), ("pilot", v.pilot)]
                .iter()
                .filter(|x| x.1)
                .map(|x| x.0)
                .collect();
            self.event(t, format!("valves open: [{}]", names.join(",")));
            self.valves = v;
        }
        let mut out = Vec::new();
        if Self::active(o, &cfg.igniter) && !self.igniter_fired {
            self.igniter_fired = true;
            self.event(t, "igniter energized");
        }
        if !self.burning && !self.burned_out && (self.igniter_fired || !cfg.require_igniter) && v.pilot && self.fluids.liquid_kg() >= 0.5 {
            self.burning = true;
            self.flight.t_launch = t;
            self.event(t, format!("ignition with {:.2} kg of nitrous at {:.0} psi", self.fluids.liquid_kg(), self.fluids.pressure_psi()));
            if !cfg.test_stand {
                self.flight.phase = Phase::Boost;
                self.event(t, "liftoff");
            }
        }
        let ambient_k = self.conditions.ambient_c + 273.15;
        let mut paths = self.supply.paths(v);
        paths.pilot = v.pilot || self.burning;
        self.fluids.step(dt, paths, ambient_k, self.chamber_pa);
        self.thrust = if self.burning { cfg.motor.thrust(self.fluids.ox_flow) } else { 0.0 };
        self.chamber_pa = cfg.motor.chamber_pa(self.thrust);
        if self.burning {
            self.fuel_left = (self.fuel_left - self.fluids.ox_flow / cfg.motor.of_ratio * dt).max(0.0);
            if t - self.flight.t_launch > 1.0 && self.thrust < 20.0 {
                self.burning = false;
                self.burned_out = true;
                self.event(t, format!("burnout ({:.2} kg nitrous left)", self.fluids.n2o_kg));
            }
        }
        let drogue = Self::active(o, &cfg.drogue_pyro);
        let main = Self::active(o, &cfg.main_pyro);
        let before = (self.flight.phase, self.flight.drogue_at.is_some(), self.flight.main_at.is_some());
        if dt > 0.0 && !cfg.test_stand {
            let n = (dt / 0.001).ceil().max(1.0) as usize;
            let mass = self.vehicle_mass();
            let cond = self.conditions;
            for i in 0..n {
                self.flight.step(t - dt + dt * (i as f64 + 1.0) / n as f64, dt / n as f64, self.thrust, mass, drogue, main, &cfg.vehicle, &cond);
            }
        }
        let after = (self.flight.phase, self.flight.drogue_at.is_some(), self.flight.main_at.is_some());
        if after != before {
            if after.1 && !before.1 {
                self.event(t, format!("drogue pyro: deployed at {:.0} m AGL", self.flight.h));
            }
            if after.2 && !before.2 {
                self.event(t, format!("main pyro: deployed at {:.0} m AGL", self.flight.h));
            }
            if after.0 != before.0 {
                self.event(t, format!("flight phase {:?} at {:.0} m AGL, {:.1} m/s", after.0, self.flight.h, self.flight.v));
            }
        }
        if self.flight.phase != Phase::Pad && self.umbilical_attached {
            self.umbilical_attached = false;
            self.supply.qd_connected = false;
            out.push(Output::CanLink { node: cfg.umbilical.0.clone(), peripheral: cfg.umbilical.1.clone(), connected: false });
            if let Some((node, index)) = &cfg.continuity_pin {
                out.push(Output::Pin { node: node.clone(), index: *index, value: 0 });
            }
            self.event(t, "umbilical separated (fill CAN and continuity loop broken)");
        }
        // VB tank PT
        let tank_key = format!("{}/{}", cfg.tank_pt.0, cfg.tank_pt.1);
        if !self.faults.dead_tank_pt && self.sent.get(&tank_key).map(String::as_str) == Some("fault=1") {
            out.push(Output::Adc { node: cfg.tank_pt.0.clone(), device: cfg.tank_pt.1.clone(), text: "fault=0".into() });
        }
        let tank_raw = if self.faults.dead_tank_pt { "fault=1".to_string() } else { vb_pressure_raw(self.fluids.pressure_psi()).to_string() };
        self.send(&mut out, &cfg.tank_pt.0, &cfg.tank_pt.1, tank_raw);
        // Load cells: the hanging rocket on the KG50 until the thrust lifts it, then the KG1000 once the gap closes.
        let on_pad = self.flight.phase == Phase::Pad;
        let w_hanging = if on_pad { self.vehicle_mass() } else { 0.0 };
        let thrust_kg = if on_pad { self.thrust / G } else { 0.0 };
        let kg50_force = (w_hanging - thrust_kg).max(0.0) + self.masses.0;
        let mut kg1000_force = (thrust_kg - w_hanging).max(0.0) + self.masses.1;
        if (thrust_kg - w_hanging) > 0.5 && self.gap_closed_at.is_none() && self.burning {
            self.gap_closed_at = Some(t);
        }
        if let Some(at) = self.gap_closed_at {
            let s = t - at; // impact transient as the gap closes: a damped 25 Hz ring
            kg1000_force += 0.3 * thrust_kg * (-s / 0.08).exp() * (2.0 * std::f64::consts::PI * 25.0 * s).sin();
        }
        let ambient_c = self.conditions.ambient_c;
        let kg50_v = self.kg50.sample(t, dt, kg50_force - cfg.vehicle.tare_kg, ambient_c);
        let kg1000_v = self.kg1000.sample(t, dt, kg1000_force, ambient_c);
        let kg1000 = if self.kg1000.faults.supply_dip { None } else { Some(kg1000_v) };
        let chamber = if self.faults.dead_chamber_pt { 0.0 } else { self.chamber_pa / phys::PSI_PA };
        let mut codes = daq_codes(kg1000, kg50_v, chamber);
        for c in 0..2 {
            self.dac_out[c] = dac_connector_v(o, &cfg.dac, c, t, &mut self.dac_starts, self.p10_swapped);
            if let Some((target, att)) = self.patch[c].clone() {
                // an off channel leaves the patched input at 0 V (the cable replaces the sensor)
                for (ch, pin_v) in patch_pins(&target, self.dac_out[c].unwrap_or(0.0), att).unwrap_or_default() {
                    codes[ch] = mcp_code(pin_v);
                }
            }
        }
        self.send(&mut out, &cfg.daq, "mcp3564r", codes.iter().map(|c| c.to_string()).collect::<Vec<_>>().join(","));
        // FC barometer and accelerometer
        let pa = std_pressure(cfg.vehicle.pad_msl_m + self.flight.h);
        let f_body = if cfg.test_stand { G } else { self.flight.f };
        self.send(&mut out, &cfg.flight_node, "flightsens", format!("{:.1},0,0,{:.3}", pa, f_body));
        if self.faults.radio_drop != self.radio_cut_sent {
            self.radio_cut_sent = self.faults.radio_drop;
            out.push(Output::RadioCut(self.faults.radio_drop));
        }
        out
    }

    fn send(&mut self, out: &mut Vec<Output>, node: &str, device: &str, text: String) {
        let key = format!("{node}/{device}");
        if self.sent.get(&key) != Some(&text) {
            self.sent.insert(key, text.clone());
            out.push(Output::Adc { node: node.into(), device: device.into(), text });
        }
    }

    /// Operator inputs, conditions, calibration masses and faults from the station's control file.
    pub fn apply_control(&mut self, c: &Value) {
        let section = |name: &str| c.get(name).cloned().unwrap_or(Value::Null);
        let b = |v: &Value, k: &str| v.get(k).and_then(Value::as_bool);
        let f = |v: &Value, k: &str| v.get(k).and_then(Value::as_f64);
        let plant = section("plant");
        if let Some(x) = b(&plant, "qd_connected") {
            if self.umbilical_attached {
                self.supply.qd_connected = x;
            }
        }
        if let Some(x) = b(&plant, "nitrogen_bottle_open") {
            self.supply.nitrogen_bottle_open = x;
        }
        if let Some(x) = b(&plant, "nitrous_bottle_open") {
            self.supply.nitrous_bottle_open = x;
        }
        let cond = section("conditions");
        if let Some(x) = f(&cond, "ambient_c") {
            self.conditions.ambient_c = x.clamp(-30.0, 50.0);
        }
        if let Some(x) = f(&cond, "wind_mps") {
            self.conditions.wind_mps = x.clamp(0.0, 40.0);
        }
        if let Some(x) = f(&cond, "wind_from_deg") {
            self.conditions.wind_from_deg = x.rem_euclid(360.0);
        }
        if let Some(x) = f(&cond, "gust_mps") {
            self.conditions.gust_mps = x.clamp(0.0, 20.0);
        }
        let loads = section("loads");
        self.masses = (f(&loads, "kg50_kg").unwrap_or(0.0).clamp(0.0, 60.0), f(&loads, "kg1000_kg").unwrap_or(0.0).clamp(0.0, 1200.0));
        let faults = section("faults");
        let fb = |k: &str| faults.get(k).and_then(Value::as_bool).unwrap_or(false);
        let ff = |k: &str| faults.get(k).and_then(Value::as_f64);
        self.faults.dead_tank_pt = fb("dead_tank_pt");
        self.faults.dead_chamber_pt = fb("dead_chamber_pt");
        self.faults.radio_drop = fb("radio_drop");
        self.kg1000.faults.supply_dip = fb("kg1000_supply_dip");
        self.kg1000.faults.open_lead = fb("kg1000_open_lead");
        self.kg50.faults.open_lead = fb("kg50_open_lead");
        self.kg50.faults.bypass = ff("kg50_bypass").unwrap_or(0.0);
        self.kg1000.faults.bypass = ff("kg1000_bypass").unwrap_or(0.0);
        self.kg50.p.drift_kg_per_c = ff("kg50_drift_kg_per_c").unwrap_or(self.cfg.kg50.drift_kg_per_c);
        self.kg1000.p.drift_kg_per_c = ff("kg1000_drift_kg_per_c").unwrap_or(self.cfg.kg1000.drift_kg_per_c);
        let patch = section("patch");
        for (c, key) in ["dac1", "dac2"].iter().enumerate() {
            self.patch[c] = patch.get(*key).and_then(Value::as_str).filter(|t| PATCH_TARGETS.contains(t)).map(|t| {
                let att = patch.get(format!("{key}_attenuation")).and_then(Value::as_f64).unwrap_or(1.0);
                (t.to_string(), att)
            });
        }
        self.p10_swapped = fb("p10_inputs_swapped");
        self.faults.stuck.clear();
        if let Some(map) = faults.get("stuck_valves").and_then(Value::as_object) {
            for (k, v) in map {
                match v.as_str() {
                    Some("open") => {
                        self.faults.stuck.insert(k.clone(), true);
                    }
                    Some("closed") => {
                        self.faults.stuck.insert(k.clone(), false);
                    }
                    _ => {}
                }
            }
        }
    }

    pub fn status(&self) -> Value {
        let fl = &self.fluids;
        json!({
            "tank": {"pressure_psi": round1(fl.pressure_psi()), "liquid_kg": round3(fl.liquid_kg()), "n2o_kg": round3(fl.n2o_kg),
                     "n2_kg": round3(fl.n2_kg), "nitrogen_psi": round1(fl.n2_pa() / phys::PSI_PA), "temperature_c": round1(fl.t_k - 273.15)},
            "bottles": {"n2o_kg": round3(fl.bottle_n2o_kg), "n2_psi": round1(fl.bottle_n2_pa() / phys::PSI_PA),
                        "n2_regulator_psi": fl.p.n2_regulator_psi},
            "valves": {"nitrogen": self.valves.nitrogen, "nitrous": self.valves.nitrous, "vent": self.valves.vent,
                       "dump": self.valves.dump, "pilot": self.valves.pilot},
            "supply": {"qd_connected": self.supply.qd_connected, "nitrogen_bottle_open": self.supply.nitrogen_bottle_open,
                       "nitrous_bottle_open": self.supply.nitrous_bottle_open},
            "conditions": self.conditions,
            "loads": {"kg50_kg": self.masses.0, "kg1000_kg": self.masses.1},
            "motor": {"burning": self.burning, "burned_out": self.burned_out, "thrust_n": round1(self.thrust),
                      "chamber_psi": round1(self.chamber_pa / phys::PSI_PA)},
            "dac": {"connector_v": self.dac_out.map(|v| v.map(round3)),
                    "patch": {"dac1": self.patch[0].as_ref().map(|p| json!({"target": p.0, "attenuation": p.1})),
                              "dac2": self.patch[1].as_ref().map(|p| json!({"target": p.0, "attenuation": p.1}))},
                    "p10_inputs_swapped": self.p10_swapped, "targets": PATCH_TARGETS},
            "umbilical_attached": self.umbilical_attached,
            "igniter_fired": self.igniter_fired,
            "test_stand": self.cfg.test_stand,
            "require_igniter": self.cfg.require_igniter,
            "flight": {"phase": self.flight.phase, "altitude_m": round1(self.flight.h), "velocity_m_s": round1(self.flight.v),
                       "apogee_m": round1(self.flight.apogee), "accel_m_s2": round3(self.flight.f),
                       "drift_east_m": round1(self.flight.east), "drift_north_m": round1(self.flight.north),
                       "mass_kg": round3(self.vehicle_mass()),
                       "drogue_at_s": self.flight.drogue_at.map(|t| round3(t - self.flight.t_launch)),
                       "main_at_s": self.flight.main_at.map(|t| round3(t - self.flight.t_launch)),
                       "launch_site": [self.cfg.vehicle.launch_lat, self.cfg.vehicle.launch_lon, self.cfg.vehicle.pad_msl_m]},
            "faults": {"stuck_valves": self.faults.stuck.iter().map(|(k, v)| (k.clone(), if *v { "open" } else { "closed" })).collect::<BTreeMap<_, _>>(),
                       "dead_tank_pt": self.faults.dead_tank_pt, "dead_chamber_pt": self.faults.dead_chamber_pt,
                       "radio_drop": self.faults.radio_drop, "kg1000_supply_dip": self.kg1000.faults.supply_dip,
                       "kg1000_open_lead": self.kg1000.faults.open_lead, "kg50_open_lead": self.kg50.faults.open_lead,
                       "kg50_bypass": self.kg50.faults.bypass, "kg1000_bypass": self.kg1000.faults.bypass},
            "events": self.events.iter().map(|(t, e)| json!({"t_s": round3(*t), "event": e})).collect::<Vec<_>>(),
        })
    }
}
fn round1(v: f64) -> f64 {
    (v * 10.0).round() / 10.0
}
fn round3(v: f64) -> f64 {
    (v * 1000.0).round() / 1000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Io(BTreeMap<(String, u8, u8), u8>, BTreeMap<(String, u32), u32>);
    impl Observe for Io {
        fn pin(&self, node: &str, port: u8, pin: u8) -> Option<u8> {
            self.0.get(&(node.to_string(), port, pin)).copied()
        }
        fn word(&self, node: &str, address: u32) -> Option<u32> {
            self.1.get(&(node.to_string(), address)).copied()
        }
    }
    fn io() -> Io {
        // VB servos: TIM2 ARR 19999 (1 us counts); vent and dump closed (~1200 us)
        let mut io = Io(BTreeMap::new(), BTreeMap::new());
        io.1.insert(("valve".into(), 0x4000_002C), 19999);
        io.1.insert(("valve".into(), 0x4000_0034), 1196);
        io.1.insert(("valve".into(), 0x4000_0038), 1196);
        io
    }
    fn run(plant: &mut Plant, io: &Io, from_ms: u64, to_ms: u64) -> Vec<Output> {
        let mut all = Vec::new();
        for ms in (from_ms..=to_ms).step_by(10) {
            all.extend(plant.step(ms * 1_000_000, io));
        }
        all
    }

    #[test]
    fn pt_rises_only_with_the_valve_open_the_qd_connected_and_the_bottle_open() {
        let mut plant = Plant::new(PlantConfig::default());
        let mut io = io();
        run(&mut plant, &io, 0, 2000);
        assert_eq!(plant.fluids.pressure_psi(), 0.0, "valves closed");
        io.0.insert(("actuator".into(), 1, 13), 1); // AB N2_SIG
        let out = run(&mut plant, &io, 2010, 12000);
        let rose = plant.fluids.pressure_psi();
        assert!(rose > 30.0, "{rose}");
        assert!(out.iter().any(|o| matches!(o, Output::Adc { device, .. } if device == "adc3")));
        plant.apply_control(&json!({"plant": {"qd_connected": false}}));
        run(&mut plant, &io, 12010, 20000);
        assert!((plant.fluids.pressure_psi() - rose).abs() < 0.5, "QD off: {} vs {rose}", plant.fluids.pressure_psi());
        plant.apply_control(&json!({"plant": {"qd_connected": true, "nitrogen_bottle_open": false}}));
        run(&mut plant, &io, 20010, 25000);
        assert!((plant.fluids.pressure_psi() - rose).abs() < 0.5, "bottle closed");
        plant.apply_control(&json!({"faults": {"stuck_valves": {"nitrogen": "closed"}}}));
        run(&mut plant, &io, 25010, 30000);
        assert!((plant.fluids.pressure_psi() - rose).abs() < 0.5, "valve stuck closed");
        plant.apply_control(&json!({"plant": {"nitrogen_bottle_open": true}}));
        run(&mut plant, &io, 30010, 35000);
        assert!(plant.fluids.pressure_psi() > rose + 10.0, "all open again");
        io.1.insert(("valve".into(), 0x4000_0034), 530); // dump servo open
        io.0.insert(("actuator".into(), 1, 13), 0);
        let before = plant.fluids.pressure_psi();
        run(&mut plant, &io, 35010, 40000);
        assert!(plant.fluids.pressure_psi() < before - 20.0, "dump bleeds");
        plant.apply_control(&json!({"faults": {"dead_tank_pt": true}}));
        let out = plant.step(40_010_000_000, &io);
        assert!(out.contains(&Output::Adc { node: "valve".into(), device: "adc3".into(), text: "fault=1".into() }));
    }

    #[test]
    fn dac_patch_routes_the_daqs_own_dac_output_to_the_patched_input() {
        let mut plant = Plant::new(PlantConfig::default());
        let mut io = io();
        let d = DacParams::default();
        // DC: DAC CH1 enabled, software value 2048 (1.65 V pin, 3.3 V at the connector)
        io.1.insert(("daq".into(), d.dac_base), 1);
        io.1.insert(("daq".into(), d.dac_base + 0x08), 2048);
        let code_of = |out: &[Output], ch: usize| -> Option<i64> {
            out.iter().rev().find_map(|o| match o {
                Output::Adc { device, text, .. } if device == "mcp3564r" => text.split(',').nth(ch).and_then(|x| x.parse().ok()),
                _ => None,
            })
        };
        let out = run(&mut plant, &io, 0, 100);
        assert_ne!(code_of(&out, 6), Some(mcp_code(3.3 / 11.0)), "not patched: the chamber PT model");
        plant.apply_control(&json!({"patch": {"dac1": "hvain1"}}));
        let out = run(&mut plant, &io, 110, 200);
        let v = 2048.0 / 4095.0 * 3.3 * 2.0;
        assert_eq!(code_of(&out, 6), Some(mcp_code(v / 11.0)));
        // a played table: 4 samples at 1 kHz from the DAQ's SRAM through DMA, the trigger timer running
        io.1.insert(("daq".into(), d.dac_base), 1 | (1 << 12));
        io.1.insert(("daq".into(), d.timer_base[0]), 1);
        io.1.insert(("daq".into(), d.timer_base[0] + 0x28), 159);
        io.1.insert(("daq".into(), d.timer_base[0] + 0x2C), 999);
        io.1.insert(("daq".into(), d.dma_channel_base[0] + 0x48), 16);
        io.1.insert(("daq".into(), d.dma_channel_base[0] + 0x4C), 0x2000_1000);
        for (i, code) in [0u32, 1000, 2000, 3000].iter().enumerate() {
            io.1.insert(("daq".into(), 0x2000_1000 + 4 * i as u32), *code);
        }
        let mut seen = std::collections::BTreeSet::new();
        for ms in (210..=400).step_by(10) {
            plant.step(ms * 1_000_000, &io);
            seen.insert((plant.dac_out[0].unwrap() * 1000.0) as i64);
        }
        assert!(seen.len() >= 2, "the table plays: {seen:?}");
        // swapped P10 inputs latch to a rail; channel off reads 0 V on the patched input
        plant.apply_control(&json!({"patch": {"dac1": "loadcell_kg50", "dac1_attenuation": 0.01}, "faults": {"p10_inputs_swapped": true}}));
        plant.step(500_000_000, &io);
        assert!(matches!(plant.dac_out[0], Some(v) if v == 0.0 || v == 6.4));
        io.1.insert(("daq".into(), d.dac_base), 0);
        let out = plant.step(510_000_000, &io);
        assert_eq!(code_of(&out, 1), Some(mcp_code(0.600)));
        assert!(patch_pins("sar1", 1.0, 1.0).is_none());
    }

    #[test]
    fn stand_burn_unloads_the_kg50_then_loads_the_kg1000_consistently() {
        let mut plant = Plant::new(PlantConfig { test_stand: true, ..PlantConfig::default() });
        plant.fluids.n2o_kg = 5.6;
        let mut io = io();
        io.0.insert(("actuator".into(), 1, 12), 1); // igniter
        run(&mut plant, &io, 0, 2000);
        let w0 = plant.vehicle_mass();
        io.0.insert(("valve".into(), 1, 11), 1); // pilot
        let (mut impulse, mut max_err, mut min50) = (0.0, 0.0f64, f64::MAX);
        for ms in (2010..=12000).step_by(10) {
            plant.step(ms * 1_000_000, &io);
            let thrust_kg = plant.thrust / G;
            let w = plant.vehicle_mass();
            let f50 = (w - thrust_kg).max(0.0);
            let f1000 = (thrust_kg - w).max(0.0);
            // the apogee planner's extraction: F_KG1000 + (W_KG50_preburn - W_KG50(t)) g, with the changing mass
            max_err = max_err.max((f1000 + (w - f50) - thrust_kg).abs());
            min50 = min50.min(f50 - plant.cfg.vehicle.tare_kg);
            impulse += plant.thrust * 0.01;
        }
        assert!(max_err < 1e-9);
        assert!(min50 < -10.0, "the KG50's tared reading goes negative as the motor lifts the rocket: {min50}");
        assert!(impulse > 5000.0, "impulse {impulse}");
        assert!(w0 > plant.vehicle_mass() + 4.0, "propellant consumed");
        assert_eq!(plant.flight.phase, Phase::Pad, "stays on the stand");
        // calibration masses on the cells
        plant.apply_control(&json!({"loads": {"kg50_kg": 10.0, "kg1000_kg": 100.0}}));
        run(&mut plant, &io, 12010, 20000);
        assert_eq!(plant.masses, (10.0, 100.0));
    }

    #[test]
    fn hitl_flight_lights_on_the_pilot_flies_and_descends_on_the_fcs_deployments() {
        let mut plant = Plant::new(PlantConfig { require_igniter: false, ..PlantConfig::default() });
        plant.fluids.n2o_kg = 5.6;
        let mut io = io();
        run(&mut plant, &io, 0, 1000);
        io.0.insert(("valve".into(), 1, 11), 1); // VB pilot
        run(&mut plant, &io, 1010, 2500);
        io.0.insert(("valve".into(), 1, 11), 0); // HITL closes it after 1.5 s: the lit motor keeps burning
        let mut ms = 2510;
        while plant.flight.phase != Phase::Landed && ms < 900_000 {
            if plant.flight.phase == Phase::Descent {
                io.0.insert(("flight".into(), 1, 5), 1);
            }
            if plant.flight.drogue_at.is_some() && plant.flight.h < 450.0 {
                io.0.insert(("flight".into(), 1, 6), 1);
            }
            plant.step(ms * 1_000_000, &io);
            ms += 10;
        }
        assert!(plant.events.iter().any(|e| e.1.starts_with("liftoff")));
        // the measured motor on the assumed airframe: about 1.4 km (tools/flight_profile measured_motor)
        assert!(plant.flight.apogee > 900.0 && plant.flight.apogee < 2200.0, "apogee {}", plant.flight.apogee);
        assert_eq!(plant.flight.phase, Phase::Landed);
        assert!(plant.flight.east.abs() + plant.flight.north.abs() > 50.0, "wind drift");
    }
}
