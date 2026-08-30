# Board layouts

Each firmware repository owns `sim/board.json`. The file and its referenced artifacts are mounted together; paths under `artifacts` are relative to `--firmware-root`.

Required sections:

- `architecture`: CPU family (`stm32g4`, `stm32h5`, or `stm32u5`)
- `mcu`: exact supported part (`stm32g491`, `stm32h523`, or `stm32u585`); it must match `architecture` and selects the platform from the unified image
- `memory`: BSP flash base/size, every physical RAM bank, bootloader and application partitions, erase/program alignment, and the independent SEDSNet main-pool budget
- `artifacts`: linked firmware and bootloader ELFs, packaged application, bootloader binary, exact combined factory image, and optional `.seds` OTA file
- `execution`: virtual runtime, optional PC tracing, and the symbol proving firmware startup completed (defaults to ThreadX `_tx_thread_schedule`)
- `traffic`: deterministic packet count, maximum payload, and dispatch policy
- `board`: optional clock overrides, signal connections, DMA routing declarations,
  strict-MMIO policy, and security attribution requested by the board
- `ota`: transport timing, firmware-observable boot outcomes, and power-cut policy
- `peripherals`: devices, their optional instruction-coupled `model`/`bus`, and
  devised failure behavior the board must survive

Peripheral implementation code belongs to FirmwareSimulator. Firmware repos
select it only through `sim/board.json`; the simulator generates and loads the
per-board Renode overlay at runtime, so an architecture definition does not
silently provide devices absent from the board configuration.

Use decimal JSON integers even when the BSP header uses hexadecimal constants. Copy partition values from `Bootloader/board_config.h`; do not infer them from a generated linker script. Validate a layout with:

```sh
firmware-sim validate --layout sim/board.json --firmware-root .
```

The authoritative schema is `schema/board-layout.schema.json` and `examples/stm32g4-board.json` is a complete example.

The direct run loads the firmware ELF and requires `execution.boot_success_symbol` (normally the scheduler) to execute. The factory run loads ELF symbols without loading ELF bytes, maps only the exact generated factory binary at the BSP flash base, initializes MSP and PC from its vector table, and requires `execution.factory_boot_success_symbol` (default `main`). Validation rejects vectors outside declared flash/RAM.

Set `artifacts.updated_firmware` to the unpackaged next application image when supplying `artifacts.ota`. The flash interruption model then uses the real old/new image pair. Without it, a deterministic one-bit mutation is retained only as a structural self-test and `updated_image_from_artifact` is false in the report.

If a Renode architecture model cannot deliver its HAL timebase interrupt efficiently, set `execution.hal_tick_address` to the linked `uwTick` address. The simulator then advances that word whenever firmware calls `HAL_GetTick`, preserving timeout and delay progress without inventing a board-wide timer peripheral. `hal_tick_step` defaults to one millisecond and may be increased to accelerate long startup delays. The address must come from the current ELF/map and remain inside a declared physical RAM region.

Define `memory.ram_regions` from the exact part/BSP linker map, including separate DMA or retention banks. The simulator creates only those flash and RAM mappings and rejects any firmware or bootloader ELF `PT_LOAD` virtual/load range that crosses a physical boundary. The sum of allocator pools is not a substitute for the chip RAM size.

Exact-MCU validation also checks the physical flash base, SRAM address window, total capacity, erase size, and programming alignment. A family-compatible but different part cannot silently reuse one of these platforms.

## Firmware-driven OTA

Set `ota.firmware_driven` and provide both `artifacts.ota` and at least one
observable outcome. UART bytes are delivered through the selected STM32
USART's receive interface, while CAN/CAN-FD frames enter the selected MCAN
controller, so the firmware's MMIO, interrupt, parser, and flash-writing paths
execute:

```json
"ota": {
  "firmware_driven": true,
  "chunk_size": 128,
  "start_after_ms": 250,
  "inter_byte_us": 100,
  "transport": {"kind": "uart", "peripheral": "uart4"},
  "outcomes": [
    {"name": "old", "symbol": "BootOldImage", "image": "old"},
    {"name": "new", "symbol": "BootUpdatedImage", "image": "new"},
    {"name": "recovery", "symbol": "EnterRecovery", "image": "recovery"}
  ],
  "power_cuts": {"every_flash_operation": true, "reboot_time_ms": 500}
}
```

Transport names and outcome symbols are validated. CAN/CAN-FD uses `can_id`
and an optional `mtu` up to 64 bytes and injects frames through the selected
real FDCAN/MCAN receive method. USB uses `endpoint` (default 1) and packetizes
the artifact through the G4 PMA or H5/U5 OTG receive FIFO. SDMMC mounts the OTA
artifact as a block-aligned removable-card image; firmware must perform normal
card discovery and read it through FIFO or IDMA.

## Adding STM32 parts

Boards and chips are separate descriptions. A new pin/memory variant can reuse
an existing STM32 IP-family model, but it still needs a catalog/platform entry
with its exact Cortex-M core, capacities, IRQ numbers, peripheral addresses,
and flash geometry. A part with a different peripheral generation also needs a
behavioral model for that IP block. Do not alias a new ordering code to a
nearby part merely because its firmware links.

The checked-in MCU catalog records, in one descriptor, the exact core model,
platform file, flash/SRAM limits, flash geometry, TrustZone capability, and OTA
controller names. Adding another ordering code starts there; validation also
checks that the referenced Renode platform declares the catalogued Cortex-M
core. This supports Cortex-M0/M0+/M3/M4/M7/M23/M33/M55 additions without
putting board wiring into the silicon definition, but each new part still needs
its accurate platform and peripheral-IP implementation.

## Board clocks and wires

`board.clocks` targets a named MCU peripheral. Because Renode frequencies are
constructor parameters, the simulator materializes a per-run copy of the MCU
platform and substitutes the validated value before creating the machine.
Unknown targets and peripherals without a frequency fail validation/loading.

`board.connections` carries digital signals between Renode endpoints. A source
such as `gpio.0` means numbered output zero; a target such as `gpio@1` means
input one. With `active_low`, the generated board contains an explicit signal
inverter. The STM32 GPIO model exposes every port pin, drives wires from
ODR/BSRR/BRR, and merges external input state into IDR according to MODER.

`board.dma_routes` uses the same named-output form, for example
`{"request":"spi1.DMAReceive","controller":"dmamux","channel":11}`.
G4 routes target `dma1`/`dma2` through DMAMUX. H5/U5 routes target `gpdma`;
linear blocks move bytes through the real system bus and expose channel
completion/error state and interrupts. Linked-list registers are retained, but
complex linked-list execution remains outside the current contract.
`strict_mmio` changes the Renode system bus to throw an address fault for every
unmapped read or write.

For H523/U585, `board.security.trustzone` enables the Cortex-M33 TrustZone
execution mode in the generated platform. `secure_regions` must be 32-byte
aligned flash/RAM ranges. The simulator subtracts them from the physical memory
map and programs the remaining ranges into the Cortex-M33 SAU as non-secure,
leaving the requested ranges secure; layouts needing more than eight SAU
regions fail validation. This enforces CPU attribution, but is not a complete
model of STM32 GTZC peripheral and MPC register programming.

`board.pins` establishes initial external GPIO states before either firmware
image starts, for example `{"gpio":"gpio","pin":17,"initial":"high"}`.
Use `floating` to let firmware PUPDR configuration determine the input. GPIO
IDR also observes open-drain release and internal pulls. ADC declarations may
set deterministic `channel_samples` and bounded `noise_lsb`; these are digital
stimuli, not an electrical SPICE model.

For soak profiling, set `execution.sample_count` and add `execution.memory_probes`. Each probe reads an exported 32-bit firmware symbol at equal virtual-time intervals. `minimum`, `maximum`, and `max_end_drop` turn allocator reserve, failure counters, panic counters, and sustained pool loss into hard failures. Set `memory_probe_warmup_samples` to exclude a known number of initial samples only from the start-to-end drop calculation; minima, maxima, and failure counters still cover every sample. Probe real allocator state; the synthetic `memory.sedsnet_pool` traffic budget is complementary, is reported as `behavioral_pool_model`, and is not evidence about the firmware's ThreadX pools or total physical RAM. To exercise network traffic through firmware, connect real nodes in a bay topology; bay execution samples and enforces each node's probes while the CAN/UART link is active.
