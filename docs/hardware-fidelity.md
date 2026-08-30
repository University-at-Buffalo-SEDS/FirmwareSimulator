# Hardware fidelity

FirmwareSimulator is a deterministic virtual platform, not a cycle-accurate electrical model. Each run emits a `fidelity` object so CI cannot confuse modeled behavior with hardware certification.

## Executed behavior

- Cortex-M4F/M33 instructions execute in pinned Renode virtual time.
- An exact `mcu` selects a bundled or inline silicon descriptor and constrains CPU model, platform, total flash/RAM capacity, flash geometry, security, and modeled OTA controllers. G491, H523, and U585 have real-board validation; other bundled descriptors have platform-contract validation.
- Direct firmware boot and combined-factory boot execute independently. Factory flash contains only the factory binary; MSP and PC come from its vector table, while ELFs provide symbols only.
- Selected CAN, UART, USB, SPI, I2C, timers, ADC, SDMMC, and DMA paths run through MMIO/wire models. Configured failure/disconnect schedules are passed into instruction-coupled sensor models.
- Embedded flash is executable `MappedMemory` and remains nonvolatile across
  peripheral resets. A CPU memory-access hook on the selected G4/H5/U5 flash profile
  observes the stores made by real firmware and enforces HAL unlock, status
  clearing, bank/page erase selection, aligned contiguous programming units,
  and one-to-zero transitions. Host-side image loading is sealed before reset
  execution so it cannot be mistaken for firmware programming.
- The structural flash interruption model classifies old-image, new-image, or
  recovery-required state at every operation boundary. Firmware-driven UART,
  CAN, USB, or SDMMC OTA executes the modeled controller receive path and records flash operations and a
  linked-symbol boot outcome. With `every_flash_operation`, each observed
  boundary (`erase_start`, `erase_complete`, or `program_unit`) is rerun in a
  fresh machine, power is cut at that exact boundary, flash is retained, and
  the real bootloader is executed and classified.
- Linked bays run in one deterministic clock domain and can require firmware activity probes at each endpoint.

## Explicit limits

The run report retains these explicit limitations:

- full RCC clock-tree propagation into every peripheral;
- cache timing, eviction, and stale-line coherency; cache maintenance/status is modeled;
- full STM32 GTZC peripheral/MPC programming. Cortex-M33 execution and SAU
  flash/RAM attribution are enforced for configured secure regions;
- transistor-level GPIO/ADC behavior. GPIO register configuration, pulls,
  open-drain release, initial external levels, wiring, and active-low board
  connections are modeled, as are deterministic ADC channel samples/noise;
- exhaustive behavior for every undocumented or unused register offset. The
  broad placeholder RAM regions have been removed, and strict MMIO faults on
  unmapped addresses, but an implemented model may return a documented reset
  value for an unsupported offset.

These limits must not be treated as passed tests. Hardware release qualification should pair simulator scenarios with target-board tests and captured SPI/I2C/CAN transactions, reset causes, fault registers, and interrupt ordering.

## Adding fidelity

Implement the smallest reference-manual surface required by a real board. Add a Renode smoke overlay under `renode/tests`, a Rust contract test, and a real linked-firmware probe. An unsupported register should be visible and attributable; broad zero-returning ranges must not be described as modeled peripherals.
