# Board layouts

Each firmware repository owns `sim/board.json`. The file and its referenced artifacts are mounted together; paths under `artifacts` are relative to `--firmware-root`.

Required sections:

- `architecture`: `stm32g4`, `stm32h5`, or `stm32u5`
- `memory`: BSP flash base/size, bootloader and application partitions, erase/program alignment, and the SEDSNet main-pool budget
- `artifacts`: linked firmware and bootloader ELFs, packaged application, bootloader binary, exact combined factory image, and optional `.seds` OTA file
- `execution`: virtual runtime, optional PC tracing, and the symbol proving firmware startup completed (defaults to ThreadX `_tx_thread_schedule`)
- `traffic`: deterministic packet count, maximum payload, and dispatch policy
- `peripherals`: the devices and devised failure behavior the board must survive

Use decimal JSON integers even when the BSP header uses hexadecimal constants. Copy partition values from `Bootloader/board_config.h`; do not infer them from a generated linker script. Validate a layout with:

```sh
firmware-sim validate --layout sim/board.json --firmware-root .
```

The authoritative schema is `schema/board-layout.schema.json` and `examples/stm32g4-board.json` is a complete example.

The direct run loads the firmware ELF and requires the success symbol to execute. The factory run loads both ELFs for symbols, overlays the exact generated factory binary at the BSP flash base, starts at the bootloader reset vector, and requires the application success symbol. This tests generated initial metadata and the real bootloader selection/validation/jump path.
