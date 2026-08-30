# FirmwareSimulator

A deterministic Rust orchestrator and Renode execution environment for SEDS embedded firmware. A board is described by JSON and passed beside its linked ELF and packaged artifacts. One multi-architecture (`linux/amd64` and `linux/arm64`) container contains reusable STM32G4, STM32H5, and STM32U5 execution profiles; the layout selects an exact built-in or repository-supplied silicon descriptor at runtime.

## Repository layout

- `core/`: STM32G4, STM32H5, and STM32U5 architecture/memory behavior
- `mcu/catalog.json`: runtime MCU catalog, capacities, CPU models, and profile mappings
- `peripherals/`: IMU, barometer, GPS, ADC, and pressure-transducer models with deterministic fault injection
- `renode/platforms/`: reusable STM32G4, STM32H5, and STM32U5 CPU/MMIO profiles
- `src/`: ELF execution, linked-bay orchestration, traffic stress, flash, boot, and OTA checks
- `schema/`: board-layout JSON schema
- `tests/`: the simulator's own test suite

## Run

```sh
cargo test --all-targets
cargo run -- self-test --arch stm32g4
cargo run -- list-mcus
```

The firmware repository supplies both inputs when running Docker:

```sh
docker run --rm \
  -v "$PWD:/firmware:ro" \
  ghcr.io/university-at-buffalo-seds/firmwaresimulator:latest \
  run --layout /firmware/sim/board.json --firmware-root /firmware
```

Instruction and linked-bay execution are documented and validated in Docker. Native execution remains available for development when a compatible Renode installation is present, but it emits a warning and is unsupported: native Renode, library, and platform differences can change results. Every firmware repository's `build.py test --all` path uses Docker.

`firmware-sim list-mcus` reports 22 bundled silicon lines: STM32G431/G441/G471/G473/G474/G483/G484/G491/G4A1, STM32H523/H533/H543/H553/H562/H563/H573, and STM32U575/U585/U595/U599/U5A5/U5A9. G491, H523, and U585 are validated with real SEDS board firmware; the additional compatible-profile lines are platform-contract tested. A board can add another part without rebuilding the image by providing `mcu_descriptor` and a firmware-root-relative Renode platform. The descriptor must select one of the implemented G4/H5/U5 flash-controller profiles, so unsupported flash IP is rejected rather than approximated.

The simulation executes the linked ARM ELF for deterministic virtual time and reports live CPU registers. It separately maps only the exact factory binary, initializes MSP/PC from that binary's vectors, and uses ELFs only for symbols. It validates BSP flash geometry and artifact placement, injects configured faults into instruction-coupled peripheral models, stresses the separate behavioral SEDSNet pool, and models STM32 flash unlock/erase/program behavior. Configured UART, CAN/CAN-FD, USB, or SDMMC OTA data can traverse the firmware-visible receive and flash path. The JSON `fidelity` section explicitly lists behavior outside the hardware model.

Multiple firmware images can execute together in a single synchronized Renode process. `firmware-sim bay --topology examples/avionics-bay.json` creates one machine per node and connects its declared CAN/UART controllers through shared virtual buses. One Renode process is intentional: it gives the entire bay a common deterministic virtual clock. The Docker image is the supported executor and can be replicated for independent bays; native execution remains an unsupported development option.

One repository-linked image containing every bundled descriptor and platform profile is built and tested by GitHub Actions. Board repositories expose this through `build.py test --all` after producing firmware, bootloader, factory, and OTA artifacts.

The organization package administrator must make the `firmwaresimulator` container package public once in its GitHub package settings. GHCR preserves that visibility for later image versions; GitHub does not provide a supported REST endpoint for changing package visibility from the publishing workflow.

## Documentation

- [Usage](docs/usage.md)
- [Board layouts](docs/board-layouts.md)
- [Peripheral models](docs/peripherals.md)
- [Hardware fidelity and explicit limits](docs/hardware-fidelity.md)
- [Crash and register debugging](docs/debugging.md)
- [Adding MCU architectures and HAL backends](docs/adding-architectures.md)
