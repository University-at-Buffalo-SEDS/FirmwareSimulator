# Board layouts

Each firmware repository owns `sim/board.json`. The file and its referenced artifacts are mounted together; paths under `artifacts` are relative to `--firmware-root`.

Required sections:

- `architecture`: `stm32g4`, `stm32h5`, or `stm32u5`
- `memory`: BSP flash base/size, every physical RAM bank, bootloader and application partitions, erase/program alignment, and the independent SEDSNet main-pool budget
- `artifacts`: linked firmware and bootloader ELFs, packaged application, bootloader binary, exact combined factory image, and optional `.seds` OTA file
- `execution`: virtual runtime, optional PC tracing, and the symbol proving firmware startup completed (defaults to ThreadX `_tx_thread_schedule`)
- `traffic`: deterministic packet count, maximum payload, and dispatch policy
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

The direct run loads the firmware ELF and requires `execution.boot_success_symbol` (normally the scheduler) to execute. The factory run loads both ELFs for symbols, overlays the exact generated factory binary at the BSP flash base, starts at the bootloader reset vector, and requires `execution.factory_boot_success_symbol` (default `main`). This tests generated initial metadata and the real bootloader selection/validation/jump path without conflating it with the longer application-runtime check already performed by the direct run.

If a Renode architecture model cannot deliver its HAL timebase interrupt efficiently, set `execution.hal_tick_address` to the linked `uwTick` address. The simulator then advances that word whenever firmware calls `HAL_GetTick`, preserving timeout and delay progress without inventing a board-wide timer peripheral. `hal_tick_step` defaults to one millisecond and may be increased to accelerate long startup delays. The address must come from the current ELF/map and remain inside a declared physical RAM region.

Define `memory.ram_regions` from the exact part/BSP linker map, including separate DMA or retention banks. The simulator creates only those flash and RAM mappings and rejects any firmware or bootloader ELF `PT_LOAD` virtual/load range that crosses a physical boundary. The sum of allocator pools is not a substitute for the chip RAM size.

For soak profiling, set `execution.sample_count` and add `execution.memory_probes`. Each probe reads an exported 32-bit firmware symbol at equal virtual-time intervals. `minimum`, `maximum`, and `max_end_drop` turn allocator reserve, failure counters, panic counters, and sustained pool loss into hard failures. Set `memory_probe_warmup_samples` to exclude a known number of initial samples only from the start-to-end drop calculation; minima, maxima, and failure counters still cover every sample. Probe real allocator state; the synthetic `memory.sedsnet_pool` traffic budget is complementary, is reported as `behavioral_pool_model`, and is not evidence about the firmware's ThreadX pools or total physical RAM. To exercise network traffic through firmware, connect real nodes in a bay topology; bay execution samples and enforces each node's probes while the CAN/UART link is active.
