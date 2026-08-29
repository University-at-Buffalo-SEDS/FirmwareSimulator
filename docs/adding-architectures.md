# Adding MCU architectures and HAL backends

Architecture constants live in one file per family under `core/`: `stm32g4.rs`, `stm32h5.rs`, and `stm32u5.rs`. Shared partition validation and register serialization stay architecture-neutral.

## Add another STM32 family

1. Add an `ArchitectureKind` value and Clap/Serde name in `core/architecture.rs`.
2. Create `core/stm32xx.rs` with vector alignment and default flash/RAM sizes, then register the module in `core/mod.rs` and `Architecture::for_kind`.
3. Decide whether the core is M4, M33, or another profile and expose its additional fault/security registers in `core/debug.rs`.
4. Add it to the JSON schema, Docker/CI architecture matrix, self-test matrix, and layout-validation tests.
5. Create a real board layout from that board's BSP and run its full factory/OTA test.

## Add a HAL backend

Keep vendor HAL emulation outside the generic device models. A backend should provide:

- memory-mapped register read/write with access-width and reset-value rules
- interrupt pending/enable/priority state and deterministic delivery
- clock/timer advancement without wall-clock sleeps
- DMA and bus completion/error events
- a bridge from HAL handles to a device model in `peripherals/`
- CPU register snapshot and fault-status capture through `CrashDiagnostic`

Start with the smallest HAL surface used by a board. Unknown register accesses must fail with the address, width, PC, and recent events instead of returning zero; that is what turns driver crashes into actionable diagnostics. Add contract tests for reset values, legal and illegal accesses, interrupt ordering, DMA completion, timeouts, and register snapshots before listing the backend as supported.

An instruction backend such as Renode, QEMU, or Unicorn can be placed behind this contract. It must be pinned in Docker, deterministic under a seed, and tested independently for all supported cores before board CI relies on it.
