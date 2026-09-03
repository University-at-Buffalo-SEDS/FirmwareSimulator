# Usage guide

FirmwareSimulator is normally invoked by a firmware repository's `build.py`.
Use the CLI directly when developing a board layout, profiling one image, or
linking several firmware images into a deterministic network.

## Prerequisites

The supported execution environment is the published Docker image. Install
Docker and build the firmware repository's ELF, bootloader ELF, application
binary, bootloader binary, and combined factory image before running the
simulator. Native `run`, `profile`, and `bay` execution requires a compatible
Renode installation and intentionally prints an unsupported-environment
warning.

Set an image name once for the examples below:

```sh
SIM_IMAGE=ghcr.io/university-at-buffalo-seds/firmwaresimulator:latest
```

For release qualification, pin `SIM_IMAGE` to the version tested by the
firmware repository instead of relying on the moving `latest` tag.

## Firmware repository workflow

Run only the board repository's unit tests:

```sh
./build.py test
```

Build the debug firmware, bootloader, combined factory image, and `.seds` OTA
artifact, then validate and simulate them in Docker:

```sh
./build.py test --all
```

Use `./build.py test --all --release` for release artifacts. `--full` remains a
compatibility alias for `--all`.

The board bridge first pulls the repository-linked image. If the registry is
unavailable, it reuses a previously built local image or shallow-clones
`FirmwareSimulator/main` and builds one. A sibling checkout is not selected
implicitly. Set `SEDS_FIRMWARE_SIM_IMAGE` to select another published image, or
`SEDS_FIRMWARE_SIM_SOURCE` to build a particular local checkout.

## Board layout

Put the layout at `sim/board.json` in the firmware repository. Artifact and
repository-supplied platform paths are relative to `--firmware-root`. Start
from [the complete G4 example](../examples/stm32g4-board.json), then replace all
memory, partition, artifact, probe, peripheral, and OTA values with values from
that board's BSP and linker output. JSON addresses are decimal integers.

Validate artifacts and memory placement without executing firmware:

```sh
docker run --rm -v "$PWD:/firmware:ro" "$SIM_IMAGE" \
  validate --layout /firmware/sim/board.json --firmware-root /firmware
```

Execute direct ELF boot, exact factory-image boot, configured peripherals,
behavioral traffic, and OTA scenarios with a repeatable seed:

```sh
docker run --rm -v "$PWD:/firmware:ro" "$SIM_IMAGE" \
  run --layout /firmware/sim/board.json --firmware-root /firmware --seed 24277
```

Put the global `--json` option before the subcommand for a machine-readable
report:

```sh
docker run --rm -v "$PWD:/firmware:ro" "$SIM_IMAGE" \
  --json run --layout /firmware/sim/board.json --firmware-root /firmware
```

The seed controls behavioral traffic, sensor values, injected failures, and
debug-register values. Before execution, every loadable ELF segment is checked
against the exact flash and RAM-bank map. The report distinguishes real
instruction-coupled paths from behavioral-only checks; behavioral traffic is
not evidence of firmware allocator stability.

## Supported platforms

`architecture` selects an execution profile and `mcu` selects exact catalog
geometry. Run `firmware-sim list-mcus` (or the Docker equivalent below) for the
machine-readable catalog:

```sh
docker run --rm "$SIM_IMAGE" list-mcus
```

The bundled catalog contains these exact parts:

| Platform | MCU values | Core | Board-validated example |
| --- | --- | --- | --- |
| `stm32g4` | `stm32g431`, `stm32g441`, `stm32g471`, `stm32g473`, `stm32g474`, `stm32g483`, `stm32g484`, `stm32g491`, `stm32g4a1` | Cortex-M4F | `stm32g491` |
| `stm32h5` | `stm32h523`, `stm32h533`, `stm32h543`, `stm32h553`, `stm32h562`, `stm32h563`, `stm32h573` | Cortex-M33 | `stm32h523` |
| `stm32u5` | `stm32u575`, `stm32u585`, `stm32u595`, `stm32u599`, `stm32u5a5`, `stm32u5a9` | Cortex-M33 | `stm32u585` |
| `stm32` | Repository-supplied exact descriptor | Supported Renode Cortex-M model | None; validation belongs to the supplying repository |

The remaining bundled parts are platform-contract tested, not individually
hardware-qualified. A shared `.repl` file means compatible modeled IP, not
permission to reuse another part's flash or RAM capacity.

### STM32G4 example

Select the G4 profile and an exact G4 catalog entry. This example uses the
hardware-validated G491 and its 512 KiB flash, 112 KiB RAM, 2 KiB erase, and
8-byte program geometry:

```json
{
  "architecture": "stm32g4",
  "mcu": "stm32g491",
  "memory": {
    "flash_base": 134217728,
    "flash_size": 524288,
    "ram_regions": [{"name": "sram", "base": 536870912, "size": 114688}],
    "erase_size": 2048,
    "write_alignment": 8
  }
}
```

These are selector and physical-geometry fields, not a complete layout; copy
the remaining required partitions and artifacts from the complete example and
the board BSP. G4 exposes the catalog-listed USART, FDCAN, and USB OTA names;
G491/G4A1 additionally list `uart4` and `fdcan2`.

```sh
docker run --rm "$SIM_IMAGE" self-test --arch stm32g4
```

### STM32H5 example

Select `stm32h5` for the H5 flash controller and fixed-layout H5 FDCAN model.
The hardware-validated H523 geometry is:

```json
{
  "architecture": "stm32h5",
  "mcu": "stm32h523",
  "memory": {
    "flash_base": 134217728,
    "flash_size": 524288,
    "ram_regions": [{"name": "sram", "base": 536870912, "size": 278528}],
    "erase_size": 8192,
    "write_alignment": 16
  }
}
```

H5 catalog entries support `fdcan1`, `usb`, and `sdmmc` OTA transports and are
TrustZone-capable. Enable `board.security.trustzone` only when the firmware and
declared secure regions are configured for it.

```sh
docker run --rm "$SIM_IMAGE" self-test --arch stm32h5
```

### STM32U5 example

Select `stm32u5` for the U5 flash controller and fixed-layout U5 FDCAN model.
The hardware-validated U585 geometry is:

```json
{
  "architecture": "stm32u5",
  "mcu": "stm32u585",
  "memory": {
    "flash_base": 134217728,
    "flash_size": 2097152,
    "ram_regions": [{"name": "sram", "base": 536870912, "size": 786432}],
    "erase_size": 8192,
    "write_alignment": 16
  }
}
```

U5 catalog entries support `usart1`, `fdcan1`, `usb`, and `sdmmc1` OTA
transports and are TrustZone-capable.

```sh
docker run --rm "$SIM_IMAGE" self-test --arch stm32u5
```

### Repository-supplied STM32 example

Use generic `stm32` only when the firmware repository supplies an exact
descriptor and Renode platform. The descriptor's `name` must equal `mcu`, its
`architecture` must be `stm32`, and `platform_file` is relative to the firmware
root:

```json
{
  "architecture": "stm32",
  "mcu": "stm32custom",
  "mcu_descriptor": {
    "name": "stm32custom",
    "architecture": "stm32",
    "core_model": "cortex-m7",
    "platform_file": "sim/stm32custom.repl",
    "platform_from_firmware": true,
    "flash_profile": "stm32g4",
    "flash_base": 134217728,
    "flash_size": 2097152,
    "ram_base": 536870912,
    "ram_size": 524288,
    "erase_size": 2048,
    "write_alignment": 8,
    "trustzone_capable": false,
    "uart_ota": [],
    "can_ota": [],
    "usb_ota": [],
    "sdmmc_ota": [],
    "board_validated": false
  }
}
```

The selected `flash_profile` must match the real controller generation; it is
not a CPU-family shortcut. A different flash controller requires simulator
code. The example values illustrate the contract and do not describe real
silicon. See [Board layouts](board-layouts.md#adding-stm32-parts) for the full
requirements.

```sh
docker run --rm "$SIM_IMAGE" self-test --arch stm32
```

## Memory profiling

Declare `execution.memory_probes` in the board layout, then run a longer idle
or background profile:

```sh
docker run --rm --platform linux/amd64 -v "$PWD:/firmware:ro" "$SIM_IMAGE" \
  profile --layout /firmware/sim/board.json --firmware-root /firmware \
  --virtual-time-ms 10000 --sample-count 20 \
  --traffic-iterations 1000000 --seed 24277
```

This command does not inject behavioral packets into the running ELF. Set
`execution.require_stack_probe` for RTOS boards and provide at least one
stack-named probe with a positive minimum. Use a linked bay for allocator tests
under real network traffic. Both modes are bounded tests, not proofs of
unlimited operation.

## Linked firmware bay

Mount a directory containing all firmware repositories and a topology file.
Firmware-node `layout` and `firmware_root` paths are resolved relative to the
topology. The simulator executes every MCU in one synchronized Renode process:

```sh
docker run --rm -v /path/to/system:/system:ro "$SIM_IMAGE" \
  bay --topology /system/avionics-bay.json
```

Start with [the three-node CAN example](../examples/avionics-bay.json) or
[the RF/Power connected soak](../examples/rf-power-bay.json). Link kinds are
`can`, `uart`, `radio`, `pico_fi`, and `routed_serial`. Every endpoint must
declare either `activity_probe` or both `tx_probe` and `rx_probe`; host endpoints
are exempt because the simulator cannot read firmware symbols from a host
process. Minimums make an idle firmware link fail instead of masquerading as a
connected test. Top-level
`assertions` can enforce minimums, maximums, required bit masks, or a particular
sample from any declared firmware probe.

Host executables can join serial-style links through `host_nodes`. Each
`serial_links` entry maps a link's generated PTY path into the named environment
variable. `network_variable_cache` persists host state across bay invocations;
relative cache paths resolve beside the topology. Give the container a writable
mount for that path.

```json
{
  "name": "host-to-gateway",
  "nodes": [
    {"name": "gateway", "layout": "gateway/sim/board.json", "firmware_root": "gateway"}
  ],
  "host_nodes": [
    {
      "name": "groundstation",
      "binary": "/system/GroundStation",
      "args": ["--headless"],
      "network_variable_cache": "/state/network-variables.json",
      "serial_links": [{"link": "radio_path", "env": "GS_RADIO_PORT"}]
    }
  ],
  "links": [
    {
      "name": "radio_path",
      "kind": "routed_serial",
      "transport_path": ["RF radio", "ground-station router", "Pico-Fi tunnel"],
      "endpoints": [
        {"node": "groundstation", "peripheral": "host"},
        {"node": "gateway", "peripheral": "usart2", "tx_probe": "radio_tx", "rx_probe": "radio_rx"}
      ]
    }
  ]
}
```

Firmware endpoint probe names must exist in the corresponding board layout. A
host endpoint's `peripheral` is descriptive; its connection is provided by the
`serial_links` environment-variable mapping.

## Complete seven-board qualification

From a clean FirmwareSimulator checkout, obtain the seven firmware migration
branches, run their unit tests, build release artifacts, and execute the linked
GroundStation, avionics-bay, and fill-system test:

```sh
python3 scripts/run-full-system.py
```

Useful variants are:

```sh
python3 scripts/run-full-system.py --debug
python3 scripts/run-full-system.py --workspace /path/to/reusable/workspace
python3 scripts/run-full-system.py --skip-unit-tests
```

Existing checkouts must be clean. The launcher fetches and fast-forwards their
`migration/sedlaunch-sedsnet-mainline` branches and reuses completed clones and
build caches on the next run.

## CLI reference

```text
firmware-sim [--json] validate --layout PATH [--firmware-root PATH]
firmware-sim [--json] run --layout PATH [--firmware-root PATH] [--seed N]
firmware-sim [--json] profile --layout PATH [--firmware-root PATH]
    [--virtual-time-ms N] [--sample-count N] [--traffic-iterations N] [--seed N]
firmware-sim [--json] bay --topology PATH
firmware-sim self-test --arch {stm32,stm32g4,stm32h5,stm32u5}
firmware-sim list-mcus
```

The authoritative configuration contracts are
[`schema/board-layout.schema.json`](../schema/board-layout.schema.json) and
[`schema/avionics-bay.schema.json`](../schema/avionics-bay.schema.json).
