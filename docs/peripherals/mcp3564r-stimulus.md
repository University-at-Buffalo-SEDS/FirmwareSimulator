# MCP3564R raw sensor stimulus

`SedsMcp3564r(stimulusPath: "/diagnostics/daq-adc.csv")` opts into an external raw-input fixture. The simulator layout uses `stimulus_path` and restricts paths to its simulation/diagnostic mount. This changes only the ADC conversion input; firmware still configures and reads the actual SPI/DMA model, applies its own calibration and publishes telemetry. It does not inject packets, acknowledgements, checklist receipts or sequence qualification.

The UTF-8 file contains exactly sixteen comma-separated decimal integers, one per MCP channel ID 0–15, with a maximum of 1024 characters. Values are raw unsigned 24-bit representations 0–16777215. Signed differential samples use two's-complement encoding: `8388607` is +8388607, `8388608` is −8388608 and `16777215` is −1. IDs 8–11 are differential pairs; ID 9 is the DAQ P6 input. The unchanged CONFIG3 data-format and SCAN logic performs sign extension, left alignment and channel tagging. `SetChannelSample(uint channel, uint value)` applies the same range bounds for monitor-driven input.

The first conversion polls the file, then one conversion out of every ten. Each read is bounded to 1025 characters and all sixteen values are validated before any change. Missing, unreadable, malformed, oversized and out-of-range files retain all last-good samples and increment the saturating `StimulusRejected` counter without repeated log messages. Writers should atomically replace the small file. The fixture holds input values rather than adding the legacy artificial drift. Without `stimulusPath`, there is no added filesystem I/O and the existing per-conversion +17 raw-count drift and internal temperature/reference fixtures remain unchanged.

The Renode machine-clock model produces conversions independently of SPI reads. It derives settled SCAN intervals from the board's 16 MHz MCLK, CONFIG1 prescaler/OSR and CONFIG2 AZ_MUX. START begins conversions; standby and reset stop them. ADCDATA remains latched during a read, and STATUS distinguishes a fresh result from a repeated read. The IRQ signal asserts on conversion completion and releases on a read or before the following conversion. With EN_STP disabled, it drives the DAQ's PB10 input and EXTI10. This digital model does not establish analog settling accuracy, noise, supply/reference variation or hardware service-time limits. Existing frozen images and qualification evidence do not gain this source-only feature retroactively.

Run the actual production-class scan/sign/fixture contracts with Mono:

```sh
python3 -m unittest discover -s tests -p test_mcp3564r_scan.py -v
```

On hosts without Mono, explicitly select an already installed .NET 8 SDK image; the test mounts only its temporary generated contract, disables container networking and runs as the invoking user:

```sh
CSHARP_TEST_DOTNET_IMAGE=seds-simulator-peripheral-tests:workspace-20261025 python3 -m unittest discover -s tests -p test_mcp3564r_scan.py -v
```


The null-machine constructor used by the older register-format unit fixture retains its explicit read-paced behavior. Renode injects a real machine and always uses clock-driven conversions; production simulation must not use the null-machine fixture.

The ready-contract test checks status freshness, SCAN channel tagging, unread-result IRQ renewal, standby/reset and temperature prescaling. Its actual Renode check exercises PB10/EXTI10/NVIC and the five-byte command/status/result frame. That check rejects interrupts before the programmed conversion interval, verifies the first tagged channel and checks GPIO/EXTI/NVIC state. Timers explicitly count upward from zero when rearmed:

```sh
CSHARP_TEST_DOTNET_IMAGE=mcr.microsoft.com/dotnet/sdk:8.0 \
RENODE_TEST_IMAGE=<locally-built-simulator-image> \
python3 -m unittest discover -s tests -p 'test_mcp3564r*.py' -v
```
