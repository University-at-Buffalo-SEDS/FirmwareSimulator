# FirmwareSimulator

A deterministic Rust simulator for SEDS embedded firmware. A board is described by a JSON layout and passed to the simulator beside its built firmware artifacts. No board-specific layout is compiled into the simulator.

## Repository layout

- `core/`: STM32G4, STM32H5, and STM32U5 architecture/memory behavior
- `peripherals/`: IMU, barometer, GPS, ADC, and pressure-transducer models with deterministic fault injection
- `src/`: layout loading, SEDSNet traffic stress, flash, boot, and OTA orchestration
- `schema/`: board-layout JSON schema
- `tests/`: the simulator's own test suite

## Run

```sh
cargo test --all-targets
cargo run -- run --layout examples/stm32g4-board.json --firmware-root /path/to/board
```

The firmware repository supplies both inputs when running Docker:

```sh
docker run --rm \
  -v "$PWD:/firmware:ro" \
  ghcr.io/university-at-buffalo-seds/firmwaresimulator:stm32g4 \
  run --layout /firmware/sim/board.json --firmware-root /firmware
```

The simulation validates BSP flash geometry and artifact placement, drives every configured peripheral through normal and devised error behavior, stresses the configured SEDSNet main pool without unbounded allocation, and checks power interruption throughout the board's dual-slot, delta-only, or recovery-transport OTA flow.

Images for all three architecture families are built and tested by GitHub Actions. Board repositories expose this through `build.py test --full` after producing firmware, bootloader, factory, and OTA artifacts.

## Documentation

- [Usage](docs/usage.md)
- [Board layouts](docs/board-layouts.md)
- [Peripheral models](docs/peripherals.md)
- [Crash and register debugging](docs/debugging.md)
- [Adding MCU architectures and HAL backends](docs/adding-architectures.md)

