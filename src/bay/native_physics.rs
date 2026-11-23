//! Physics for the station-managed bay's plant (native_plant.rs): saturated nitrous, the nitrogen pressurant and
//! its regulator, the run tank, the hybrid motor in blowdown, the stand's load cells and the atmosphere.
//!
//! Sources and assumptions (all SI unless named):
//! * N2O saturation properties: ESDU 91022 correlations as used in the Aspire Space run-tank model (vapour pressure,
//!   saturated liquid and vapour density, liquid/vapour enthalpy, liquid heat capacity), valid about 183-309 K.
//! * Run tank 12 L Al 6061 (GroundStation26 tools/flight_profile/vehicle.json: "n2o_run_tank_al6061_12L", 7 kg),
//!   lumped liquid/vapour/wall temperature, equilibrium liquid/vapour split; evaporation to refill the ullage cools it.
//! * Supply bottle: 50 lb (22.7 kg) of N2O at ambient temperature (assumed); nitrogen K bottle 49 L at 2200 psi
//!   (assumed) behind a regulator (setpoint configurable); bottle blowdown polytropic (n = 1.3) with heat back from
//!   ambient; tank N2 partial pressure ideal gas at the tank temperature.
//! * Flows: liquid through an orifice, mdot = CdA sqrt(2 rho dP) (single-phase approximation of the two-phase fill
//!   line; no flashing correction), gas/vapour choked or subsonic through an orifice. Effective areas are assumed so
//!   an 8 kg fill takes a few minutes with venting, the vent bleeds slowly and the dump empties the tank in ~15 s.
//! * Motor (11-16 static fire, tools/flight_profile/motors/testfire_20261116.json: 9.19 kN s from 5.6 kg N2O, liquid
//!   phase ~2.7 s at ~2 kN then a vapour tail): oxidiser through the injector from tank conditions (liquid, then
//!   choked vapour), thrust = delivered Isp (~140 s, from the motor summary) x g0 x (1 + 1/OF) x mdot_ox, OF 5; chamber pressure from a
//!   fixed C_F A_t chosen so 2 kN reads ~350 psi.
//! * Load cells (GroundStation26 docs/loadcell-investigation-2026-11.md): KG1000 545.8 kg/V at the ADC pin, zero
//!   0.2126 V (1.83 mV/V, 9.6 V excitation, 100.9x); KG50 12 915 kg/V, zero 0.2112 V; noise 0.03-0.2 kg per sample,
//!   4 Hz and 10 Hz scheduling lines, first-order settling, optional thermal drift; faults: load bypass fraction
//!   (rod friction), KG1000 supply-dip top clamp, open lead (about half sensitivity, more noise).
//!   Stand load path: thrust first unloads the KG50 (rocket hanging), KG50 = W_hanging - thrust floored at zero
//!   cell force (its tared reading goes negative), then the gap closes and the KG1000 sees thrust - W_hanging.
//! * Atmosphere: ISA with a temperature offset from the ambient setting; wind speed/direction with gusts.
pub const G0: f64 = 9.80665;
pub const PSI_PA: f64 = 6894.757293;
const TC: f64 = 309.57;
const PC: f64 = 7_251_000.0;
const RHOC: f64 = 452.0;
pub const R_N2O: f64 = 188.91;
pub const R_N2: f64 = 296.8;

/// ESDU 91022 saturation vapour pressure (Pa).
pub fn n2o_psat(t: f64) -> f64 {
    let tr = (t / TC).clamp(0.55, 0.9999);
    let x = 1.0 - tr;
    PC * ((1.0 / tr) * (-6.71893 * x + 1.35966 * x.powf(1.5) - 1.3779 * x.powf(2.5) - 4.051 * x.powi(5))).exp()
}
pub fn n2o_rho_liquid(t: f64) -> f64 {
    let x = 1.0 - (t / TC).clamp(0.55, 0.9999);
    RHOC * (1.72328 * x.powf(1.0 / 3.0) - 0.83950 * x.powf(2.0 / 3.0) + 0.51060 * x - 0.10412 * x.powf(4.0 / 3.0)).exp()
}
pub fn n2o_rho_vapour(t: f64) -> f64 {
    let y = 1.0 / (t / TC).clamp(0.55, 0.9999) - 1.0;
    RHOC * (-1.00900 * y.powf(1.0 / 3.0) - 6.28792 * y.powf(2.0 / 3.0) + 7.50332 * y - 7.90463 * y.powf(4.0 / 3.0)
        + 0.629427 * y.powf(5.0 / 3.0))
    .exp()
}
/// Latent heat of vaporisation (J/kg).
pub fn n2o_latent(t: f64) -> f64 {
    let x = 1.0 - (t / TC).clamp(0.55, 0.9999);
    let hl = -200.0 + 116.043 * x.powf(1.0 / 3.0) - 917.225 * x.powf(2.0 / 3.0) + 794.779 * x - 589.587 * x.powf(4.0 / 3.0);
    let hv = -200.0 + 440.055 * x.powf(1.0 / 3.0) - 459.701 * x.powf(2.0 / 3.0) + 434.081 * x - 485.338 * x.powf(4.0 / 3.0);
    (hv - hl) * 1000.0
}
/// Saturated liquid heat capacity (J/kg/K).
pub fn n2o_cp_liquid(t: f64) -> f64 {
    let x = 1.0 - (t / TC).clamp(0.55, 0.995);
    2499.73 * (1.0 + 0.023454 / x - 3.80136 * x + 13.0945 * x * x - 14.5180 * x * x * x)
}

/// Orifice: incompressible liquid flow (kg/s).
pub fn liquid_flow(cda: f64, rho: f64, dp: f64) -> f64 {
    if dp <= 0.0 { 0.0 } else { cda * (2.0 * rho * dp).sqrt() }
}
/// Orifice: ideal-gas flow from p_up/t_up to p_down, choked or subsonic (kg/s).
pub fn gas_flow(cda: f64, p_up: f64, t_up: f64, p_down: f64, gamma: f64, r: f64) -> f64 {
    if p_up <= p_down || p_up <= 0.0 {
        return 0.0;
    }
    let crit = (2.0 / (gamma + 1.0)).powf(gamma / (gamma - 1.0));
    let ratio = (p_down / p_up).max(crit);
    let term = (2.0 * gamma / (gamma - 1.0)) * (ratio.powf(2.0 / gamma) - ratio.powf((gamma + 1.0) / gamma));
    cda * p_up * (term.max(0.0) / (r * t_up)).sqrt()
}

#[derive(Clone, Debug, serde::Deserialize)]
#[serde(default)]
pub struct FluidParams {
    pub tank_volume_m3: f64,
    pub tank_wall_kg: f64,
    pub wall_cp: f64,
    /// Heat transfer from ambient to the tank (W/K).
    pub tank_ua: f64,
    pub n2o_bottle_kg: f64,
    pub n2_bottle_volume_m3: f64,
    pub n2_bottle_psi: f64,
    pub n2_regulator_psi: f64,
    pub fill_cda_m2: f64,
    /// Extra source pressure over the bottle's vapour pressure: inverted-bottle liquid head and fill-line pumping
    /// effects (psi). Without it a same-temperature transfer stalls at saturation until venting cools the tank.
    pub fill_head_psi: f64,
    pub n2_cda_m2: f64,
    pub vent_cda_m2: f64,
    pub dump_cda_m2: f64,
    pub injector_cda_m2: f64,
}
impl Default for FluidParams {
    fn default() -> Self {
        Self {
            tank_volume_m3: 0.012,
            tank_wall_kg: 7.0,
            wall_cp: 900.0,
            tank_ua: 4.0,
            n2o_bottle_kg: 22.7,
            n2_bottle_volume_m3: 0.049,
            n2_bottle_psi: 2200.0,
            n2_regulator_psi: 1000.0,
            fill_cda_m2: 1.2e-5,
            fill_head_psi: 15.0,
            n2_cda_m2: 1.5e-6,
            vent_cda_m2: 1.0e-6,
            dump_cda_m2: 6.0e-6,
            injector_cda_m2: 2.9e-5,
        }
    }
}

/// What the plant's valves and supply path allow this instant.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Paths {
    /// AB nitrous valve open, QD mated, bottle open.
    pub nitrous_in: bool,
    /// AB nitrogen valve open, QD mated, bottle open.
    pub nitrogen_in: bool,
    pub vent: bool,
    pub dump: bool,
    /// Pilot (main oxidiser) valve open into the motor.
    pub pilot: bool,
}

#[derive(Clone, Debug)]
pub struct Fluids {
    pub p: FluidParams,
    /// Run tank: N2O mass, N2 mass, lumped temperature.
    pub n2o_kg: f64,
    pub n2_kg: f64,
    pub t_k: f64,
    /// Supply bottles.
    pub bottle_n2o_kg: f64,
    pub bottle_n2_kg: f64,
    pub bottle_n2_t: f64,
    /// Oxidiser into the motor this step (kg/s), for the motor model.
    pub ox_flow: f64,
    pub ox_liquid: bool,
    /// Chamber pressure per oxidiser flow (Pa per kg/s) of the motor fed by the pilot: the liquid injector flow and
    /// the chamber pressure are solved together (an explicit step oscillates). 0: no motor model (back pressure only).
    pub chamber_k: f64,
}
impl Fluids {
    pub fn new(p: FluidParams, ambient_k: f64) -> Self {
        let n2_kg = p.n2_bottle_psi * PSI_PA * p.n2_bottle_volume_m3 / (R_N2 * ambient_k);
        Self { bottle_n2o_kg: p.n2o_bottle_kg, bottle_n2_kg: n2_kg, bottle_n2_t: ambient_k, p, n2o_kg: 0.0, n2_kg: 0.0, t_k: ambient_k, ox_flow: 0.0, ox_liquid: false, chamber_k: 0.0 }
    }
    /// Liquid volume (m^3) at the current temperature, equilibrium split.
    pub fn liquid_volume(&self) -> f64 {
        let (rl, rv) = (n2o_rho_liquid(self.t_k), n2o_rho_vapour(self.t_k));
        ((self.n2o_kg - rv * self.p.tank_volume_m3) / (rl - rv)).clamp(0.0, self.p.tank_volume_m3 * 0.98)
    }
    pub fn liquid_kg(&self) -> f64 {
        self.liquid_volume() * n2o_rho_liquid(self.t_k)
    }
    fn ullage(&self) -> f64 {
        (self.p.tank_volume_m3 - self.liquid_volume()).max(self.p.tank_volume_m3 * 0.02)
    }
    /// N2O partial pressure: saturated while liquid remains, else ideal-gas vapour (capped at saturation).
    pub fn n2o_pa(&self) -> f64 {
        if self.n2o_kg <= 0.0 {
            return 0.0;
        }
        let (rl, rv) = (n2o_rho_liquid(self.t_k), n2o_rho_vapour(self.t_k));
        let unclamped = (self.n2o_kg - rv * self.p.tank_volume_m3) / (rl - rv);
        if unclamped > self.p.tank_volume_m3 * 0.98 {
            // liquid-full: hydraulic, the pressure climbs steeply with any more liquid
            let over = (unclamped - self.p.tank_volume_m3 * 0.98) / self.p.tank_volume_m3;
            n2o_psat(self.t_k) + over * 2.0e9
        } else if unclamped > 0.0 {
            n2o_psat(self.t_k)
        } else {
            // superheated vapour: the saturated vapour's compressibility scaled by density (ideal gas overstates
            // the pressure near saturation, Z ~ 0.6 at 20 C)
            n2o_psat(self.t_k) * (self.n2o_kg / self.p.tank_volume_m3 / n2o_rho_vapour(self.t_k)).min(1.0)
        }
    }
    pub fn n2_pa(&self) -> f64 {
        self.n2_kg * R_N2 * self.t_k / self.ullage()
    }
    pub fn pressure_pa(&self) -> f64 {
        self.n2o_pa() + self.n2_pa()
    }
    pub fn pressure_psi(&self) -> f64 {
        self.pressure_pa() / PSI_PA - 0.0
    }
    pub fn bottle_n2_pa(&self) -> f64 {
        self.bottle_n2_kg * R_N2 * self.bottle_n2_t / self.p.n2_bottle_volume_m3
    }
    fn heat_capacity(&self) -> f64 {
        self.liquid_kg() * n2o_cp_liquid(self.t_k) + self.p.tank_wall_kg * self.p.wall_cp + 50.0
    }
    /// Integrate `dt` s. `chamber_pa` is the motor's back pressure while the pilot is open.
    pub fn step(&mut self, dt: f64, paths: Paths, ambient_k: f64, chamber_pa: f64) {
        let mut left = dt;
        while left > 1e-9 {
            let h = left.min(0.005);
            self.integrate(h, paths, ambient_k, chamber_pa);
            left -= h;
        }
    }
    fn integrate(&mut self, dt: f64, paths: Paths, ambient_k: f64, chamber_pa: f64) {
        let atm = 101_325.0;
        let p_tank = self.pressure_pa();
        let vapour_before = self.n2o_kg - self.liquid_kg();
        let rl = n2o_rho_liquid(self.t_k);
        let mut q = self.p.tank_ua * (ambient_k - self.t_k) * dt; // J into the tank
        let mut vapour_out = 0.0; // N2O vapour leaving the ullage this step
        // Liquid in from the bottle (at ambient, saturated) while there is liquid in it and it is above the tank.
        if paths.nitrous_in && self.bottle_n2o_kg > 0.2 {
            let p_src = n2o_psat(ambient_k) + self.p.fill_head_psi * PSI_PA;
            let dm = (liquid_flow(self.p.fill_cda_m2, n2o_rho_liquid(ambient_k), p_src - p_tank) * dt).min(self.bottle_n2o_kg - 0.2);
            if dm > 0.0 {
                // sensible heat of the warmer/cooler incoming liquid
                q += dm * n2o_cp_liquid(ambient_k) * (ambient_k - self.t_k);
                self.n2o_kg += dm;
                self.bottle_n2o_kg -= dm;
            }
        }
        // Nitrogen in through the regulator (polytropic bottle blowdown).
        if paths.nitrogen_in && self.bottle_n2_kg > 0.0 {
            let p_reg = (self.p.n2_regulator_psi * PSI_PA).min(self.bottle_n2_pa());
            let dm = (gas_flow(self.p.n2_cda_m2, p_reg, self.bottle_n2_t, p_tank, 1.4, R_N2) * dt).min(self.bottle_n2_kg);
            if dm > 0.0 {
                let p_before = self.bottle_n2_pa();
                self.n2_kg += dm;
                self.bottle_n2_kg -= dm;
                let p_after = self.bottle_n2_kg * R_N2 * self.bottle_n2_t / self.p.n2_bottle_volume_m3;
                if p_before > 0.0 && p_after > 0.0 {
                    self.bottle_n2_t *= (p_after / p_before).powf(0.3 / 1.3);
                }
            }
        }
        self.bottle_n2_t += (ambient_k - self.bottle_n2_t) * (dt / 600.0);
        // Vent: ullage gas (N2O vapour and N2 in proportion to partial pressure), choked through the vent.
        if paths.vent && p_tank > atm {
            let dm = gas_flow(self.p.vent_cda_m2, p_tank, self.t_k, atm, 1.3, R_N2O) * dt;
            let n2_share = (self.n2_pa() / p_tank).clamp(0.0, 1.0);
            self.n2_kg = (self.n2_kg - dm * n2_share).max(0.0);
            let out = (dm * (1.0 - n2_share)).min(self.n2o_kg);
            self.n2o_kg -= out;
            vapour_out += out;
        }
        // Dump: liquid from the bottom while there is liquid, else gas.
        if paths.dump && p_tank > atm {
            if self.liquid_kg() > 0.0 {
                let dm = (liquid_flow(self.p.dump_cda_m2, rl, p_tank - atm) * dt).min(self.liquid_kg());
                self.n2o_kg -= dm;
            } else {
                let dm = gas_flow(self.p.dump_cda_m2, p_tank, self.t_k, atm, 1.3, R_N2O) * dt;
                let n2_share = (self.n2_pa() / p_tank).clamp(0.0, 1.0);
                self.n2_kg = (self.n2_kg - dm * n2_share).max(0.0);
                let out = (dm * (1.0 - n2_share)).min(self.n2o_kg);
                self.n2o_kg -= out;
                vapour_out += out;
            }
        }
        // Pilot into the motor: liquid while it lasts, then choked vapour (the tail).
        self.ox_flow = 0.0;
        self.ox_liquid = false;
        if paths.pilot && p_tank > chamber_pa {
            if self.liquid_kg() > 0.01 {
                // Pc = k mdot, mdot = CdA sqrt(2 rho (P - Pc)): Pc^2 = a^2 (P - Pc), a = k CdA sqrt(2 rho)
                let pc = if self.chamber_k > 0.0 {
                    let a2 = (self.chamber_k * self.p.injector_cda_m2).powi(2) * 2.0 * rl;
                    (-a2 + (a2 * a2 + 4.0 * a2 * p_tank).sqrt()) / 2.0
                } else {
                    chamber_pa
                };
                let dm = (liquid_flow(self.p.injector_cda_m2, rl, p_tank - pc) * dt).min(self.liquid_kg());
                self.n2o_kg -= dm;
                self.ox_flow = dm / dt;
                self.ox_liquid = true;
            } else if self.n2o_kg > 0.0 {
                let dm = (gas_flow(self.p.injector_cda_m2, p_tank, self.t_k, chamber_pa.max(atm), 1.3, R_N2O) * dt).min(self.n2o_kg);
                let n2_share = (self.n2_pa() / p_tank).clamp(0.0, 1.0);
                self.n2_kg = (self.n2_kg - dm * n2_share).max(0.0);
                let out = (dm * (1.0 - n2_share)).min(self.n2o_kg);
                self.n2o_kg -= out;
                vapour_out += out;
                self.ox_flow = out / dt;
            }
        }
        // Phase change: liquid evaporates to refill the vapour space (or vapour condenses), at the latent heat.
        let vapour_after = self.n2o_kg - self.liquid_kg();
        let evaporated = (vapour_after - (vapour_before - vapour_out)).clamp(-1.0, 1.0);
        if self.liquid_kg() > 0.0 || evaporated < 0.0 {
            q -= evaporated * n2o_latent(self.t_k);
        }
        self.t_k = (self.t_k + q / self.heat_capacity()).clamp(183.0, 309.0);
    }
}

#[derive(Clone, Debug, serde::Deserialize)]
#[serde(default)]
pub struct MotorParams {
    pub isp_s: f64,
    pub of_ratio: f64,
    /// Thrust multiplier (1 = the measured 11-16 motor; ~1.8 reaches 10 kft with the assumed airframe).
    pub impulse_scale: f64,
    /// C_F x A_t (m^2): chamber pressure = thrust / this.
    pub cf_at_m2: f64,
}
impl Default for MotorParams {
    fn default() -> Self {
        // 11-16 fire re-extracted with the KG50 unloading: 9.19 kN s from 5.6 kg N2O + 1.1 kg fuel (Isp ~140 s); the
        // launcher overrides these from tools/flight_profile/motors/*.json at start
        Self { isp_s: 139.9, of_ratio: 5.09, impulse_scale: 1.0, cf_at_m2: 2000.0 / (350.0 * PSI_PA) }
    }
}
impl MotorParams {
    /// Chamber pressure per oxidiser flow (Pa per kg/s).
    pub fn chamber_k(&self) -> f64 {
        self.thrust(1.0) / self.cf_at_m2
    }
    pub fn thrust(&self, ox_flow: f64) -> f64 {
        self.isp_s * G0 * (1.0 + 1.0 / self.of_ratio) * ox_flow * self.impulse_scale
    }
    pub fn chamber_pa(&self, thrust: f64) -> f64 {
        thrust / self.cf_at_m2
    }
}

#[derive(Clone, Debug, serde::Deserialize)]
#[serde(default)]
pub struct LoadCellParams {
    pub kg_per_v: f64,
    pub zero_v: f64,
    pub noise_kg: f64,
    pub line_kg: f64,
    pub settle_s: f64,
    pub drift_kg_per_c: f64,
}
pub fn kg50_default() -> LoadCellParams {
    LoadCellParams { kg_per_v: 12_915.0, zero_v: 0.2112, noise_kg: 0.05, line_kg: 0.05, settle_s: 1.5, drift_kg_per_c: 0.0 }
}
pub fn kg1000_default() -> LoadCellParams {
    LoadCellParams { kg_per_v: 545.8, zero_v: 0.2126, noise_kg: 0.1, line_kg: 0.1, settle_s: 0.05, drift_kg_per_c: 0.0 }
}
impl Default for LoadCellParams {
    fn default() -> Self {
        kg50_default()
    }
}

#[derive(Clone, Debug, Default)]
pub struct LoadCellFaults {
    /// Fraction of the load carried around the cell (rod friction), 0..1.
    pub bypass: f64,
    /// KG1000 supply-dip: the amplifier clamps at its top rail.
    pub supply_dip: bool,
    /// Open lead: about half sensitivity and more noise.
    pub open_lead: bool,
}

/// One load cell: the force on it (kg-force) through settling, faults, drift and noise into an ADC-pin voltage.
#[derive(Clone, Debug)]
pub struct LoadCell {
    pub p: LoadCellParams,
    pub faults: LoadCellFaults,
    settled_kg: f64,
    rng: u64,
}
impl LoadCell {
    pub fn new(p: LoadCellParams, seed: u64) -> Self {
        Self { p, faults: LoadCellFaults::default(), settled_kg: 0.0, rng: seed | 1 }
    }
    fn gauss(&mut self) -> f64 {
        // xorshift + Box-Muller
        let mut next = || {
            self.rng ^= self.rng << 13;
            self.rng ^= self.rng >> 7;
            self.rng ^= self.rng << 17;
            ((self.rng >> 11) as f64 + 0.5) / (1u64 << 53) as f64
        };
        let (u, v) = (next(), next());
        (-2.0 * u.ln()).sqrt() * (2.0 * std::f64::consts::PI * v).cos()
    }
    /// The force on the cell `load_kg` at time `t` with ambient `ambient_c`; returns the pin voltage.
    pub fn sample(&mut self, t: f64, dt: f64, load_kg: f64, ambient_c: f64) -> f64 {
        let applied = load_kg * (1.0 - self.faults.bypass.clamp(0.0, 1.0));
        let a = if self.p.settle_s > 0.0 { (dt / self.p.settle_s).min(1.0) } else { 1.0 };
        self.settled_kg += (applied - self.settled_kg) * a;
        let lines = self.p.line_kg * ((2.0 * std::f64::consts::PI * 4.0 * t).sin() * 0.6 + (2.0 * std::f64::consts::PI * 10.0 * t).sin() * 0.4);
        let noise = self.gauss() * if self.faults.open_lead { 0.6 } else { self.p.noise_kg };
        let sensitivity = if self.faults.open_lead { 0.5 } else { 1.0 };
        let kg = self.settled_kg * sensitivity + self.p.drift_kg_per_c * (ambient_c - 20.0) + lines + noise;
        self.p.zero_v + kg / self.p.kg_per_v
    }
}

/// ISA density with the temperature offset of the ambient setting.
pub fn air_density(h_msl: f64, ambient_c: f64, pad_msl: f64) -> f64 {
    let isa_pad = 288.15 - 0.0065 * pad_msl;
    let offset = ambient_c + 273.15 - isa_pad;
    let t = 288.15 - 0.0065 * h_msl.min(11_000.0) + offset;
    101_325.0 * (1.0 - 2.25577e-5 * h_msl).powf(5.25588) / (287.05 * t)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn n2o_saturation_matches_nist() {
        // NIST webbook: 20 C 5.06 MPa (rho_l 786, rho_v ~160), 0 C 3.13 MPa, -20 C 1.82 MPa
        for (c, mpa, tol) in [(20.0, 5.06, 0.08), (0.0, 3.13, 0.06), (-20.0, 1.82, 0.05)] {
            let p = n2o_psat(273.15 + c) / 1e6;
            assert!((p - mpa).abs() < tol, "{c} C: {p} MPa");
        }
        assert!((n2o_rho_liquid(293.15) - 786.0).abs() < 15.0);
        assert!((n2o_rho_vapour(293.15) - 160.0).abs() < 10.0);
        assert!(n2o_latent(293.15) > 1.3e5 && n2o_latent(293.15) < 2.2e5);
    }

    #[test]
    fn nitrous_fills_only_with_a_path_and_venting_cools_and_drops_pressure() {
        let amb = 293.15;
        let mut f = Fluids::new(FluidParams::default(), amb);
        f.step(5.0, Paths::default(), amb, 0.0);
        assert_eq!(f.pressure_psi(), 0.0);
        // closed vent: self-pressurises to the bottle and stops
        f.step(30.0, Paths { nitrous_in: true, ..Default::default() }, amb, 0.0);
        let sealed = f.n2o_kg;
        assert!(f.pressure_psi() > 600.0 && sealed > 0.1, "{} psi {} kg", f.pressure_psi(), sealed);
        // venting while filling cools the tank: pressure below the bottle, more liquid comes in
        // filling with the vent open: liquid accumulates up to liquid-full, never past it
        f.step(120.0, Paths { nitrous_in: true, vent: true, ..Default::default() }, amb, 0.0);
        assert!(f.liquid_kg() > 4.0, "{} kg liquid", f.liquid_kg());
        assert!(f.n2o_kg < f.p.tank_volume_m3 * n2o_rho_liquid(f.t_k), "never overfilled past liquid-full");
        // venting alone: evaporation cools the liquid and the pressure follows the saturation curve down
        f.step(3.0, Paths { dump: true, ..Default::default() }, amb, 0.0);
        assert!(f.liquid_kg() > 1.0);
        let (t0, p0) = (f.t_k, f.pressure_pa());
        f.step(120.0, Paths { vent: true, ..Default::default() }, amb, 0.0);
        assert!(f.t_k < t0 - 1.0, "evaporative cooling {} -> {}", t0, f.t_k);
        assert!(f.pressure_pa() < p0, "vent drops pressure");
        // dump drains
        let before = f.n2o_kg;
        f.step(5.0, Paths { dump: true, ..Default::default() }, amb, 0.0);
        assert!(f.n2o_kg < before - 1.0);
        // warmer ambient means higher vapour pressure
        assert!(n2o_psat(308.0) > n2o_psat(293.0) + 1.5e6);
    }

    #[test]
    fn nitrogen_regulated_and_bottle_depletes() {
        let amb = 293.15;
        let mut f = Fluids::new(FluidParams { n2_regulator_psi: 500.0, ..Default::default() }, amb);
        let b0 = f.bottle_n2_pa();
        f.step(120.0, Paths { nitrogen_in: true, ..Default::default() }, amb, 0.0);
        assert!(f.pressure_psi() > 450.0 && f.pressure_psi() <= 501.0, "regulated {}", f.pressure_psi());
        assert!(f.bottle_n2_pa() < b0);
        assert!(f.bottle_n2_t < amb, "bottle cools on blowdown");
    }

    #[test]
    fn blowdown_motor_matches_the_measured_impulse() {
        let amb = 293.15;
        let mut f = Fluids::new(FluidParams::default(), amb);
        f.n2o_kg = 5.6;
        let m = MotorParams::default();
        f.chamber_k = m.chamber_k();
        let (mut impulse, mut t, mut chamber, mut liquid_end) = (0.0, 0.0, 0.0, None);
        while t < 15.0 {
            f.step(0.005, Paths { pilot: true, ..Default::default() }, amb, chamber);
            let thrust = m.thrust(f.ox_flow);
            chamber = m.chamber_pa(thrust);
            impulse += thrust * 0.005;
            if !f.ox_liquid && liquid_end.is_none() && t > 0.1 {
                liquid_end = Some(t);
            }
            t += 0.005;
        }
        assert!((impulse - 9192.0).abs() < 1500.0, "impulse {impulse}");
        let le = liquid_end.unwrap();
        assert!(le > 1.5 && le < 4.5, "liquid phase {le}");
    }

    #[test]
    fn load_cell_settles_to_the_load_with_faults() {
        let mut c = LoadCell::new(kg50_default(), 7);
        let mut v = 0.0;
        for i in 0..2000 {
            v = c.sample(i as f64 * 0.01, 0.01, 10.0, 20.0);
        }
        assert!(((v - 0.2112) * 12_915.0 - 10.0).abs() < 0.5, "{}", (v - 0.2112) * 12_915.0);
        c.faults.open_lead = true;
        for i in 0..2000 {
            v = c.sample(20.0 + i as f64 * 0.01, 0.01, 10.0, 20.0);
        }
        assert!(((v - 0.2112) * 12_915.0 - 5.0).abs() < 2.5, "half sensitivity");
    }
}
