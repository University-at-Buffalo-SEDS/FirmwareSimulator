# FirmwareSimulator

A deterministic Rust orchestrator and Renode execution environment for SEDS embedded firmware. A board is described by JSON and passed beside its linked ELF and packaged artifacts. No board-specific layout is compiled into the Rust executable.

## Repository layout

- `core/`: STM32G4, STM32H5, and STM32U5 architecture/memory behavior
- `peripherals/`: IMU, barometer, GPS, ADC, and pressure-transducer models with deterministic fault injection
- `renode/platforms/`: STM32G491, STM32H523, and STM32U585 CPU/MMIO platforms
- `src/`: ELF execution, linked-bay orchestration, traffic stress, flash, boot, and OTA checks
- `schema/`: board-layout JSON schema
- `tests/`: the simulator's own test suite

## Run

```sh
cargo test --all-targets
cargo run -- self-test --arch stm32g4
```

The firmware repository supplies both inputs when running Docker:

```sh
docker run --rm \
  -v "$PWD:/firmware:ro" \
  ghcr.io/university-at-buffalo-seds/firmwaresimulator:stm32g4 \
  run --layout /firmware/sim/board.json --firmware-root /firmware
```

Instruction and linked-bay execution are documented and validated in Docker. Native execution remains available for development when a compatible Renode installation is present, but it emits a warning and is unsupported: native Renode, library, and platform differences can change results. Every firmware repository's `build.py test --full` path uses Docker.

The simulation executes the linked ARM ELF for deterministic virtual time and reports live CPU registers. It separately executes the exact factory binary from the bootloader reset vector and requires the application boot-success symbol, covering LaunchCore metadata, validation, and jump behavior. It also validates BSP flash geometry and artifact placement, drives configured peripheral fault models, stresses the SEDSNet main pool, and checks power interruption throughout the board's dual-slot, delta-only, or recovery-transport OTA flow.

Multiple firmware images can execute together in a single synchronized Renode process. `firmware-sim bay --topology examples/avionics-bay.json` creates one machine per node and connects its declared CAN/UART controllers through shared virtual buses. One Renode process is intentional: it gives the entire bay a common deterministic virtual clock. The Docker image is the supported executor and can be replicated for independent bays; native execution remains an unsupported development option.

Images for all three architecture families are built and tested by GitHub Actions. Board repositories expose this through `build.py test --full` after producing firmware, bootloader, factory, and OTA artifacts.

## Documentation

- [Usage](docs/usage.md)
- [Board layouts](docs/board-layouts.md)
- [Peripheral models](docs/peripherals.md)
- [Crash and register debugging](docs/debugging.md)
- [Adding MCU architectures and HAL backends](docs/adding-architectures.md)
