mod architecture;
mod debug;
mod pool;
mod stm32g4;
mod stm32h5;
mod stm32u5;
pub use architecture::{mcu_catalog, Architecture, ArchitectureKind, McuDescriptor, McuKind};
pub use debug::{CrashDiagnostic, FaultRegisters, RegisterFile};
pub use pool::{FixedPool, PoolStats};
