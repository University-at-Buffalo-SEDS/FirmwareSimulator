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

The direct run loads the firmware ELF and requires the success symbol to execute. The factory run loads both ELFs for symbols, overlays the exact generated factory binary at the BSP flash base, starts at the bootloader reset vector, and requires the application success symbol. This tests generated initial metadata and the real bootloader selection/validation/jump path.

Define `memory.ram_regions` from the exact part/BSP linker map, including separate DMA or retention banks. The simulator creates only those flash and RAM mappings and rejects any firmware or bootloader ELF `PT_LOAD` virtual/load range that crosses a physical boundary. The sum of allocator pools is not a substitute for the chip RAM size.

For soak profiling, set `execution.sample_count` and add `execution.memory_probes`. Each probe reads an exported 32-bit firmware symbol at equal virtual-time intervals. `minimum`, `maximum`, and `max_end_drop` turn allocator reserve, failure counters, panic counters, and sustained pool loss into hard failures. Probe real allocator state; the synthetic `memory.sedsnet_pool` traffic budget is complementary and is not evidence about the firmware's ThreadX pools or total physical RAM.
