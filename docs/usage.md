# Usage

## Firmware repository workflow

Run the board's existing unit tests:

```sh
./build.py test
```

Build release firmware, bootloader, combined factory image, and `.seds` OTA transport, then run the board layout and artifacts inside Docker:

```sh
./build.py test --full
```

When the simulator source is checked out beside the firmware repositories, the bridge builds that source. Otherwise it pulls `ghcr.io/university-at-buffalo-seds/firmwaresimulator:<architecture>`. Set `SEDS_FIRMWARE_SIM_IMAGE` to test a different published image.

## Container use

```sh
cargo test --all-targets
cargo run -- validate --layout /board/sim/board.json --firmware-root /board
docker run --rm -v /path/to/board:/firmware:ro \
  ghcr.io/university-at-buffalo-seds/firmwaresimulator:stm32g4 \
  run --layout /firmware/sim/board.json --firmware-root /firmware --seed 24277

docker run --rm -v /path/to/avionics:/avionics:ro \
  ghcr.io/university-at-buffalo-seds/firmwaresimulator:stm32g4 \
  bay --topology /avionics/avionics-bay.json
```

The seed makes traffic, sensor values, failures, and debug registers repeatable. A successful run prints JSON containing artifact sizes, peripheral behavior (including an explicit `instruction_coupled` flag), real firmware memory probes, SEDSNet model pool high-water usage, allocation failures, remaining allocations, and OTA interruption results.

`run` and `bay` are documented and validated only in Docker. The image contains the pinned Renode runtime and platform models, so the normal host workflow does not need a Renode installation. Native execution is permitted when `RENODE` points to a compatible executable (or Renode is installed at a recognized path), but it emits an unsupported, use-at-your-own-risk warning. `run` requires the linked ELFs, executes ARM instructions, and then runs the behavioral traffic/peripheral/OTA layers. Set `execution.trace` in the board layout to retain a Renode binary-PC trace inside the container's output mount.

For a full bay, mount a directory containing all firmware repositories and the topology file into the container. Each node names its board layout and firmware root relative to the topology file. CAN and UART links execute in one synchronized virtual-time domain; this is more deterministic than connecting unsynchronized emulator containers.

Run a real-firmware allocator soak using the probes declared by the board:

```sh
docker run --rm --platform linux/amd64 -v "$PWD:/firmware:ro" \
  ghcr.io/university-at-buffalo-seds/firmwaresimulator:stm32g4 \
  profile --layout /firmware/sim/board.json --firmware-root /firmware \
  --virtual-time-ms 10000 --sample-count 20 --traffic-iterations 1000000
```

This is a bounded soak, not a mathematical guarantee of infinite operation. Increase virtual time and repeat with multiple seeds for release qualification.
