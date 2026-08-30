# Adding MCU architectures and HAL backends

Architecture constants live in one file per family under `core/`, while executable MCU descriptions live under `renode/platforms/`. Shared partition validation and register serialization stay architecture-neutral.

STM32 ordering codes are silicon descriptions, not board descriptions. Add a
new descriptor for each exact supported part and reuse an existing architecture
and peripheral model only when the reference manuals identify the same IP
generation and register layout. Cortex-M0/M0+/M3/M7/M23/M33/M55 cores can be
added through Renode's corresponding CPU model; adding the core alone does not
make that STM32 part supported.

## Add another STM32 family

1. Add an exact descriptor to `mcu/catalog.json`, or put `mcu_descriptor` and a
   firmware-relative platform in the board repository when the part can reuse
   an existing flash profile. Record its architecture, Renode core/platform,
   physical capacity, flash geometry, TrustZone capability, and available OTA
   controllers. Use the generic `stm32` execution profile for an external exact
   platform; add an `ArchitectureKind` only for a genuinely new bundled IP
   profile.
2. Create `core/stm32xx.rs` with vector alignment and default flash/RAM sizes, then register the module in `core/mod.rs` and `Architecture::for_kind`.
3. Decide whether the core is M4, M33, or another profile and expose its additional fault/security registers in `core/debug.rs`.
4. Add it to the JSON schema, unified-image self-test loop, platform smoke overlays, and layout-validation tests.
5. Create a real board layout from that board's BSP and run its full factory/OTA test.
6. Add a Renode platform with the exact CMSIS memory map and enough modeled clock, flash, GPIO, DMA, timer, and communication behavior for the board to reach its configured boot-success symbol.

## Add a HAL backend

Keep vendor HAL emulation outside the generic device models. A backend should provide:

- memory-mapped register read/write with access-width and reset-value rules
- interrupt pending/enable/priority state and deterministic delivery
- clock/timer advancement without wall-clock sleeps
- DMA and bus completion/error events
- a bridge from HAL handles to a device model in `peripherals/`
- CPU register snapshot and fault-status capture through `CrashDiagnostic`

Start with the smallest HAL surface used by a board. Unknown register accesses must fail with the address, width, PC, and recent events instead of returning zero; that is what turns driver crashes into actionable diagnostics. Add contract tests for reset values, legal and illegal accesses, interrupt ordering, DMA completion, timeouts, and register snapshots before listing the backend as supported.

Renode 1.16.1 is the pinned instruction backend. New platforms must load without monitor errors in CI and must boot a real linked firmware ELF and exact factory image locally before being listed as supported. Keep the platform narrow and explicit; add real peripheral models rather than broad zero-returning MMIO ranges.
