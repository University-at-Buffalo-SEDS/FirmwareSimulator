# MCP3564R raw sensor stimulus

`SedsMcp3564r(stimulusPath: "/diagnostics/daq-adc.csv")` opts into an external raw-input fixture. The simulator layout uses `stimulus_path` and restricts paths to its simulation/diagnostic mount. This changes only the ADC conversion input; firmware still configures and reads the actual SPI/DMA model, applies its own calibration and publishes telemetry. It does not inject packets, acknowledgements, checklist receipts or sequence qualification.

The UTF-8 file contains exactly sixteen comma-separated decimal integers, one per MCP channel ID 0–15, with a maximum of 1024 characters. Values are raw unsigned 24-bit representations 0–16777215. Signed differential samples use two's-complement encoding: `8388607` is +8388607, `8388608` is −8388608 and `16777215` is −1. IDs 8–11 are differential pairs; ID 9 is the DAQ P6 input. The unchanged CONFIG3 data-format and SCAN logic performs sign extension, left alignment and channel tagging. `SetChannelSample(uint channel, uint value)` applies the same range bounds for monitor-driven input.

The first conversion read polls the file, then one read out of every ten conversions (~20 ms at 500 Hz aggregate scan rate). Each read is bounded to 1025 characters and all sixteen values are validated before any change. Missing, unreadable, malformed, oversized and out-of-range files retain all last-good samples and increment the saturating `StimulusRejected` counter without repeated log messages. Writers should atomically replace the small file. The fixture holds input values rather than adding the legacy artificial drift. Without `stimulusPath`, there is no added filesystem I/O and the existing per-conversion +17 raw-count drift and internal temperature/reference fixtures remain unchanged.

This cadence is conversion-count based, not a physical conversion timing model. It bounds fixture I/O for the configured scan rate; it does not model analog settling, noise, supply/reference variation or proprietary hardware behavior. Existing frozen images and qualification evidence do not gain this source-only feature retroactively.

Run the actual production-class scan/sign/fixture contracts with Mono:

```sh
python3 -m unittest discover -s tests -p test_mcp3564r_scan.py -v
```

On hosts without Mono, explicitly select an already installed .NET 8 SDK image; the test mounts only its temporary generated contract, disables container networking and runs as the invoking user:

```sh
CSHARP_TEST_DOTNET_IMAGE=seds-simulator-peripheral-tests:workspace-20261025 python3 -m unittest discover -s tests -p test_mcp3564r_scan.py -v
```
