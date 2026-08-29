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

## Direct use

```sh
cargo test --all-targets
cargo run -- validate --layout /board/sim/board.json --firmware-root /board
cargo run -- run --layout /board/sim/board.json --firmware-root /board --seed 24277
```

The seed makes traffic, sensor values, failures, and debug registers repeatable. A successful run prints JSON containing artifact sizes, peripheral behavior, SEDSNet pool high-water usage, allocation failures, remaining allocations, and OTA interruption results.

The simulator is behavioral: it validates firmware/BSP contracts and devised driver behavior, but does not execute ARM instructions. Unit tests and cross-compilation remain part of `test --full`; instruction-accurate execution requires a CPU backend as described in [Adding MCU architectures](adding-architectures.md).

