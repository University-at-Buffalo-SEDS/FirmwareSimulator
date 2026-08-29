use serde::Serialize;

use crate::layout::BoardLayout;

use super::{Architecture, ArchitectureKind};

#[derive(Clone, Debug, Serialize)]
pub struct RegisterFile {
    pub r0_r12: [u32; 13],
    pub msp: u32,
    pub psp: u32,
    pub lr: u32,
    pub pc: u32,
    pub xpsr: u32,
    pub control: u32,
    pub primask: u32,
    pub basepri: u32,
    pub faultmask: u32,
}

#[derive(Clone, Debug, Serialize)]
pub struct FaultRegisters {
    pub cfsr: u32,
    pub hfsr: u32,
    pub dfsr: u32,
    pub mmfar: u32,
    pub bfar: u32,
    pub afsr: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sfsr: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sfar: Option<u32>,
}

#[derive(Clone, Debug, Serialize)]
pub struct CrashDiagnostic {
    pub board: String,
    pub architecture: ArchitectureKind,
    pub simulated_phase: String,
    pub reason: String,
    pub registers: RegisterFile,
    pub fault_registers: FaultRegisters,
    pub recent_events: Vec<String>,
    pub note: &'static str,
}

impl CrashDiagnostic {
    pub fn capture(layout: &BoardLayout, seed: u64, phase: &str, reason: String) -> Self {
        let architecture = Architecture::for_kind(layout.architecture);
        let ram = layout.memory.ram_regions.first();
        let ram_base = ram.map_or(0x2000_0000, |region| region.base);
        let ram_size = ram.map_or(architecture.default_ram_size, |region| region.size);
        let mut state = seed.max(1);
        let mut general = [0_u32; 13];
        for register in &mut general {
            state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
            *register = (state >> 32) as u32;
        }
        let pc_span = layout.memory.slot_a_size.saturating_sub(4).max(4);
        let pc = layout.memory.slot_a_base + ((seed % pc_span) & !1);
        let stack_top = ram_base.saturating_add(ram_size) & !7;
        let is_m33 = matches!(
            layout.architecture,
            ArchitectureKind::Stm32h5 | ArchitectureKind::Stm32u5
        );
        Self {
            board: layout.name.clone(),
            architecture: layout.architecture,
            simulated_phase: phase.into(),
            reason,
            registers: RegisterFile {
                r0_r12: general,
                msp: stack_top as u32,
                psp: stack_top.saturating_sub(0x200) as u32,
                lr: 0xffff_fff9,
                pc: pc as u32,
                xpsr: 0x2100_0000,
                control: 0,
                primask: 0,
                basepri: 0,
                faultmask: 0,
            },
            fault_registers: FaultRegisters {
                cfsr: 0,
                hfsr: 0x4000_0000,
                dfsr: 0,
                mmfar: 0,
                bfar: 0,
                afsr: 0,
                sfsr: is_m33.then_some(0),
                sfar: is_m33.then_some(0),
            },
            recent_events: vec![
                "layout_loaded".into(),
                "artifacts_mapped".into(),
                phase.into(),
            ],
            note: "Behavioral-simulator snapshot; use a CPU backend for instruction-accurate registers.",
        }
    }
}
