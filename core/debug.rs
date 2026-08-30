use serde::Serialize;

use crate::layout::BoardLayout;

use super::ArchitectureKind;

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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub registers: Option<RegisterFile>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fault_registers: Option<FaultRegisters>,
    pub recent_events: Vec<String>,
    pub note: &'static str,
}

impl CrashDiagnostic {
    pub fn capture(layout: &BoardLayout, seed: u64, phase: &str, reason: String) -> Self {
        let _ = seed;
        Self {
            board: layout.name.clone(),
            architecture: layout.architecture,
            simulated_phase: phase.into(),
            reason,
            registers: None,
            fault_registers: None,
            recent_events: vec![
                "layout_loaded".into(),
                "artifacts_mapped".into(),
                phase.into(),
            ],
            note: "No CPU snapshot was captured for this phase; synthetic register values are never emitted.",
        }
    }
}
