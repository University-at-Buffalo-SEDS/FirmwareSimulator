# FirmwareSimulator

A deterministic Rust orchestrator and Renode execution environment for SEDS embedded firmware. A board is described by JSON and passed beside its linked ELF and packaged artifacts. One multi-architecture (`linux/amd64` and `linux/arm64`) container contains reusable STM32G4, STM32H5, STM32H7, and STM32U5 execution profiles; the layout selects an exact built-in or repository-supplied silicon descriptor at runtime.

## Repository layout

- `core/`: STM32G4, STM32H5, STM32H7, and STM32U5 architecture/memory behavior
- `mcu/catalog.json`: runtime MCU catalog, capacities, CPU models, and profile mappings
- `peripherals/`: sensor and removable-storage models with deterministic fault injection
- `renode/platforms/`: reusable STM32G4, STM32H5, STM32H7, and STM32U5 CPU/MMIO profiles
- `src/`: ELF execution, linked-bay orchestration, traffic stress, flash, boot, and OTA checks
- `schema/`: board-layout and linked-bay JSON schemas
- `tests/`: the simulator's own test suite

## Run

```sh
cargo test --all-targets
cargo run -- self-test --arch stm32g4
cargo run -- self-test --arch stm32h5
cargo run -- self-test --arch stm32h7
cargo run -- self-test --arch stm32u5
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

`firmware-sim list-mcus` reports 42 bundled silicon lines: STM32G431/G441/G471/G473/G474/G483/G484/G491/G4A1, STM32H523/H533/H543/H553/H562/H563/H573, STM32U575/U585/U595/U599/U5A5/U5A9, and all 20 STM32H7 lines (see [H7 support](docs/stm32h7.md)). G491, H523, and U585 are validated with real SEDS board firmware; the additional compatible-profile lines are platform-contract tested. A board can add another part without rebuilding the image by providing `mcu_descriptor` and a firmware-root-relative Renode platform. The descriptor must select one of the implemented G4/H5/H7/H7AB/H7RS/U5 flash-controller profiles, so unsupported flash IP is rejected rather than approximated.

The simulation executes the linked ARM ELF for deterministic virtual time and reports live CPU registers. It separately maps only the exact factory binary, initializes MSP/PC from that binary's vectors, and uses ELFs only for symbols. It validates BSP flash geometry and artifact placement, injects configured faults into instruction-coupled peripheral models, stresses the separate behavioral SEDSNet pool, and models STM32 flash unlock/erase/program behavior. Configured UART, CAN/CAN-FD, USB, or SDMMC OTA data can traverse the firmware-visible receive and flash path. The JSON `fidelity` section explicitly lists behavior outside the hardware model.

Multiple firmware images can execute together in a single synchronized Renode process. `firmware-sim bay --topology examples/avionics-bay.json` creates one machine per node and connects its declared CAN/UART controllers through shared virtual buses. One Renode process is intentional: it gives the entire bay a common deterministic virtual clock. The Docker image is the supported executor and can be replicated for independent bays; native execution remains an unsupported development option.

Bay topologies can schedule retained-flash resets with `reboots`. Firmware nodes reset the selected
MCU, reload debugger symbols, and preserve modeled physical flash. Host nodes restart the selected
process while retaining its configured network-variable cache; serial and Pico-Fi links accept the
replacement process without resetting their peer. Pair the event with persistence, rediscovery, and
liveness probes to verify restoration before network resynchronization and successful rejoin.

Pico host links can select `pico_fi_discovery_to_uart` with the host environment key
`GS_FILL_PICO_DISCOVERY_PORT`. The Pico transport model queries the GroundStation UDP
discovery listener, validates its reply and connects to the advertised TCP port.
GroundStation binds an available port and reconnects through discovery after a
restart. `pico_fi_tcp_to_uart` retains explicit TCP endpoints for comparison;
neither Ethernet mode uses the I²C mailbox. Native tests exercise actual UDP,
TCP and a UART PTY, including a changed host port and bidirectional framing.
The transport model does not execute RP2040 firmware or emulate DHCP/W5500
electrical behavior; Pico-Fi's production addressing policy and ARP driver have
separate native tests and compiled MCU images.

One repository-linked image contains every bundled descriptor and platform profile. GitHub Actions and GitLab CI definitions provide the build/test pipeline; release0.4.13 was built and tested manually on native AMD64 and ARM64 because organization Actions is unavailable. See the [manual publication proof](docs/qualification/sedsnet-4.2.3-manual-image.json). Board repositories pin the `v0.4.14` image through `build.py test --all` after producing firmware, bootloader, factory, and OTA artifacts. The image resolves GroundStation’s SEDSnet major-version-4 dependency from crates.io. Explicit source-revision and registry-version build arguments support reproducible candidate checks; the application manifest keeps the major-version selector. The seven-board service-only and mixed-restart ten-minute qualification is described in [qualification status](docs/qualification-status.md) and [route recovery](docs/route-recovery.md). Release publishing updates `latest`, the version tag, and the `stm32g4`, `stm32h5`, `stm32h7`, and `stm32u5` aliases to the same Linux AMD64/ARM64 manifest.

The linked gate requires GroundStation to discover and canonically label all
seven board nodes, assign nonzero application traffic to each originating
sender, and correlate a routed Valve command with its matching returned state
ACK. On Docker hosts that cannot create bridge interfaces, set
`SEDS_FIRMWARE_SIM_DOCKER_NETWORK=host` when invoking a board test.

To qualify the complete system from a fresh simulator checkout without manually
cloning any board repositories, run:

```sh
python3 scripts/run-full-system.py
```

The launcher clones or fast-forwards all seven firmware migration branches into
`build/full-system`, runs every repository's unit suite, builds release firmware,
and executes the linked GroundStation, avionics-bay, and fill-system test. Pass
`--debug` for debug firmware or `--workspace /path` to keep the checkouts and
build cache elsewhere.

The organization package administrator must make the `firmwaresimulator` container package public once in its GitHub package settings. GHCR preserves that visibility for later image versions; GitHub does not provide a supported REST endpoint for changing package visibility from the publishing workflow.

## Documentation

- [Usage guide and per-platform examples](docs/usage.md)
- [Board layouts](docs/board-layouts.md)
- [Peripheral models](docs/peripherals.md)
- [Hardware fidelity and explicit limits](docs/hardware-fidelity.md)
- [Crash and register debugging](docs/debugging.md)
- [Adding MCU architectures and HAL backends](docs/adding-architectures.md)

### Manual image build

Run from the simulator checkout on a native Docker host:

```sh
docker build -t firmware-simulator:local .
bash scripts/test-image.sh firmware-simulator:local
```

The image builds the GroundStation backend from dev with its version4 SEDSnet
selector. Reproducible release checks can supply `GROUNDSTATION_REF` and
`GROUNDSTATION_SEDSNET_RELEASE` as Docker build arguments. Test AMD64 and ARM64
images on their native hosts before publishing the combined release manifest.

Candidate host builds can verify their actual Cargo selection with `python3 scripts/verify-sedsnet-source.py --workspace /path/to/GroundStation26 --source /path/to/SEDSnet --update-lock`. Run subsequent Cargo checks with the same path-patch configuration. Image tests read the bundled SDK source proof; `EXPECTED_SEDSNET_VERSION=4.2.4` requires that exact version.

Version 0.4.14 validation and publication are tracked in the [manual image record](docs/qualification/sedsnet-4.2.4-manual-image.json).

Version 0.4.14 validation and publication are tracked in the [manual image record](docs/qualification/sedsnet-4.2.4-manual-image.json).

### SD recording fixtures

The SDMMC model uses a sparse temporary file, so card capacity does not reserve
an equally large byte array. FIFO/IDMA reads, writes, reset retention, removal
and failure injection operate on that backing image. `SaveCardImage` creates a
new sparse export containing the written pages; it never overwrites a capture.

Storage descriptors may opt into `"filesystem": "fat32"`, or
`"filesystem": "fat32:SEDSDAQ.ID"` to prepare FAT32 with one provisioning marker.
The container supplies `dosfstools` and `mtools` for this fixture setup. Default
cards remain blank for provisioning/missing-filesystem tests. The extended
recording soak uses prepared 64 GiB DAQ/FC cards and observes FC writer counters;
the default FC blank-card fixture alone does not qualify recording. This tests
filesystem/record ownership, not physical-card latency or power-loss durability.
