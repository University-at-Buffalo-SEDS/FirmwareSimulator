# Usage

## Firmware repository workflow

Run the board's existing unit tests:

```sh
./build.py test
```

Build debug firmware, bootloader, combined factory image, and `.seds` OTA
transport, then run the board layout and artifacts inside Docker:

```sh
./build.py test --all
```

Use `./build.py test --all --release` to build and simulate release artifacts.
`--full` remains a compatibility alias for `--all`.

The board bridge first pulls the repository-linked
`ghcr.io/university-at-buffalo-seds/firmwaresimulator:latest` image. That one
image contains every reusable platform profile; `mcu` and optional
`mcu_descriptor` in `board.json` select the silicon and platform. If the registry is unavailable, the bridge reuses a previously built
local image or shallow-clones `FirmwareSimulator/main` and builds one. A sibling
checkout is not selected implicitly. Set `SEDS_FIRMWARE_SIM_IMAGE` to test a
different published image, or set `SEDS_FIRMWARE_SIM_SOURCE` to explicitly
build a particular local checkout.

## Container use

```sh
cargo test --all-targets
cargo run -- validate --layout /board/sim/board.json --firmware-root /board
docker run --rm -v /path/to/board:/firmware:ro \
  ghcr.io/university-at-buffalo-seds/firmwaresimulator:latest \
  run --layout /firmware/sim/board.json --firmware-root /firmware --seed 24277

docker run --rm -v /path/to/avionics:/avionics:ro \
  ghcr.io/university-at-buffalo-seds/firmwaresimulator:latest \
  bay --topology /avionics/avionics-bay.json
```

The seed makes behavioral traffic, sensor values, failures, and debug registers repeatable. Before execution, each loadable ELF segment is checked against the board's exact flash and RAM-bank map; Renode is then given only those physical regions. A successful run prints aligned test, peripheral, memory-probe, and register matrices. Pass `--json` for the complete machine-readable report, including the explicit `instruction_coupled` and `firmware_path_exercised` fields. The separately labelled behavioral traffic model must not be used to certify firmware allocator stability.

`run` and `bay` are documented and validated only in Docker. The image contains the pinned Renode runtime and platform models, so the normal host workflow does not need a Renode installation. Native execution is permitted when `RENODE` points to a compatible executable (or Renode is installed at a recognized path), but it emits an unsupported, use-at-your-own-risk warning. `run` requires the linked ELFs, executes ARM instructions, and then runs the behavioral traffic/peripheral/OTA layers. Set `execution.trace` in the board layout to retain a Renode binary-PC trace inside the container's output mount.

For a full bay, mount a directory containing all firmware repositories and the topology file into the container. Each node names its board layout and firmware root relative to the topology file. CAN and UART links execute the real firmware endpoints in one synchronized virtual-time domain. Multi-node CAN hubs explicitly disable controller self-loopback, matching physical normal-mode traffic and preventing relay firmware from amplifying an artificial echo. Bay runs take multiple samples of every node's configured allocator probes and enforce their thresholds; use this path for RF/Power connected-load qualification.

`examples/rf-power-bay.json` is the checked-in connected RF/Power soak when FirmwareSimulator, RFBoard26, and PowerBoard26 are sibling repositories. It wires both FDCAN interrupt lines as well as the CAN hub. Each endpoint declares an `activity_probe`; the bay fails unless that firmware ISR counter reaches `minimum_activity` (default one), preventing an idle allocator profile from masquerading as a connected-network test.

Run an idle/background real-firmware allocator profile using the probes declared by one board:

```sh
docker run --rm --platform linux/amd64 -v "$PWD:/firmware:ro" \
  ghcr.io/university-at-buffalo-seds/firmwaresimulator:latest \
  profile --layout /firmware/sim/board.json --firmware-root /firmware \
  --virtual-time-ms 10000 --sample-count 20 --traffic-iterations 1000000
```

This single-board command does not inject its behavioral packets into the ELF. Use a linked `bay` topology for network-load allocator testing. Both modes are bounded tests, not a mathematical guarantee of infinite operation; increase virtual time and repeat representative bay scenarios for release qualification.
