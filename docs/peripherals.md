# Peripheral models

Built-in types are `imu`, `barometer`, `gps`, `adc`, `pressure_transducer`, and
`storage`. Every device requires `type` and `name`. Optional common behavior:

- `failure_every`: return a deterministic driver error every Nth access
- `disconnect_after`: behave as disconnected after N accesses

For instruction-coupled devices these controls are passed into the Renode wire/MMIO model, so firmware observes invalid bus data, missing completion, or peripheral error status. The report's read/error totals remain deterministic behavioral-model counts and are labelled `behavioral_counts_only`; use firmware probes to assert that the real driver noticed and recovered from each injected fault.

ADC models accept `bits`, `channels`, per-channel `channel_samples`, and optional
deterministic `noise_lsb`; pressure transducers accept `max_psi`. The full test
exercises ordinary data, intermittent failures, and disconnection without
relying on hardware.

Removable storage uses the `sd_card` model and accepts `capacity_bytes`, which
must be a multiple of 512 and defaults to 4 MiB. H5 and U5 attach it to the
layout bus name `sdmmc1`; the H5 profile maps that logical name to its `sdmmc`
controller.

The SDMMC instruction model supports polling/FIFO block reads and writes,
including FileX provisioning and log writes. Command-start is self-clearing and
reported CSD capacity follows the backing card. This is not validation of card
wear, real write latency, or every DMA mode. Assert firmware write/drop counters;
the behavioral storage count alone does not prove a file was written correctly.

STM32U5 SPI uses the CFG1/CFG2 register generation. Its DMA connections are
`spi2.DMARecieve` (the upstream spelling) and `spi2.DMATransmit`. GPDMA channels
start at offset `0x50` with stride `0x80`; peripheral-source requests transfer one
beat, and completion occurs at the block boundary. The U5 profile also models
DCACHE1 command completion at `0x40031400`. Linked-list DMA is not yet covered.

ADC `channel_samples` indexes are physical channel numbers, not positions in a
firmware scan list. For example, DAQ's battery measurement uses ADC1 channel 5;
an unrealistic voltage there intentionally triggers firmware power-loss logic.

## Add a peripheral

`model` plus `bus` declares that the device is attached to the emulated CPU bus.
The run report marks those devices as `instruction_coupled: true`. Omitting both
keeps a device in the deterministic behavioral/fault-injection layer only; the
report marks it false so that layer cannot be mistaken for driver coverage.

The built-in register/wire models are `neo_m9n` (SPI GPS), `bmi088` (SPI IMU),
`bmp390` (SPI barometer), `ltc2990` (I2C monitor/ADC), and `stm32_adc` (STM32 ADC
MMIO). The simulator generates a Renode overlay from the board layout and
attaches only the selected devices. An unsupported model, bus, address, or
architecture combination is a configuration error.

1. Add its layout fields to `PeripheralSpec` in `peripherals/models.rs`.
2. Validate board-facing constraints in `validate`; reject impossible configurations instead of silently clamping them.
3. Add deterministic sampling in `sample`. Use the supplied seeded generator so CI failures reproduce.
4. Treat expected timeout, busy, corrupt-data, and disconnected behavior as observable results. Do not panic for a device-level failure.
5. Extend `schema/board-layout.schema.json` and add normal/fault/disconnect cases under `tests/`.
6. Add the device to the consuming board's `sim/board.json` and run `./build.py test --all`.

Instruction-coupled models exercise the real firmware driver through emulated
MMIO and wire transactions. The deterministic behavioral layer remains useful
for high-volume failure injection, but is reported separately and is not a
substitute for driver execution.
