# Hardware fidelity

Machine-readable run records are kept in the external artifact archive described in [qualification status](qualification-status.md).

FirmwareSimulator is a deterministic virtual platform, not a cycle-accurate electrical model. Each run emits a `fidelity` object so CI cannot confuse modeled behavior with hardware certification.

### Flight Computer sensor acquisition

The H523 model connects GPIO chip-selects, register-configured data-ready
timers, port-selected EXTI edges, and SPI RX/TX requests to GPDMA. Channels
0–7 use H523 IRQs 27–34. CS remains asserted across separate HAL SPI calls;
controller-level transfer completion does not terminate the sensor transaction.
The BMP390 trim/sample values exercise the firmware's compensation math.
SPI byte/halfword/word packing and master receive-only clock generation are
modeled: the barometer's split transmit/receive HAL calls must read a real chip
ID before it can configure conversions. Docker contracts cover that sequence.

Earlier FC instrumented firmware injected IMU packets from its telemetry thread;
those runs qualify transport only. The injector is removed. Qualification must
require actual DMA-delivered samples and both IMU and barometer publications,
then separately measure their arrival cadence at GroundStation. An observed
boot or a live networking thread alone does not establish sensor health.

The SPI controller retains Renode's unbounded transmit FIFO and immediate wire
transfer behavior; it is not a cycle-accurate SPI bandwidth model. This early
completion case exposed a firmware lost-wakeup race, now covered by the FC's
production-code completion test. Electrical sensor noise remains synthetic.

## STM32G4 UART DMA

USART1 and USART2 execute the production UART DMA paths: configured 8N1
wire timing, bounded receive/transmit FIFOs, RXNE, IDLE and overrun flags,
write-one-to-clear interrupts, and DMAMUX-selected byte transfers into actual
firmware memory. DMA maintains remaining counts, circular reload, half/full
transfer flags and NVIC interrupts. Changing interrupt enables on an active
channel preserves its transfer position, as STM32 HAL requires.

Renode contracts exercise RX timing, IDLE, normal/circular transfers,
DMAR/DMAMUX gating, abort, reset, and exact TX bytes across a half-transfer
interrupt configuration change. The concurrent model test checks 500,000 bytes
in each direction. RF and Gateway firmware no longer substitute UART polling
or blocking transmission in instrumented simulator builds: both builds arm
ReceiveToIdle DMA, and RF retains DMA timeout/retry recovery. Native two-build
regressions check real HAL success, BUSY, and error results. These contracts do
not by themselves qualify a complete network or establish hardware throughput.

Only the byte-wide USART1/2 paths used by these boards are covered. Full UART
prescaler/oversampling, parity, nine-bit data, flow control and electrical radio
behavior remain outside this validation. The input wire queue bridges host PTY
scheduling to virtual byte timing; it does not represent extra MCU FIFO memory.

## STM32G4 CRC

The G4 CRC peripheral now executes the production hardware-CRC adapter instead
of forcing its software fallback. The model implements programmable 32-bit
polynomial and INIT, reset, input/output reversal, and byte/halfword/word writes.
Four contracts compare IEEE and non-reflected CRC results, incremental writes,
multiple lengths and seeds, and reset against an independent bitwise reference.
Peripheral bus latency and 7/8/16-bit polynomial modes are not modeled. These
contracts establish functional compatibility, not physical CRC timing.

## CAN wire timing and host clock

Linked G4/H5/U5 controllers reserve all three physical TX slots and share a
wire scheduler per CAN link. TXFQS/TXBRP reflect occupied slots until a frame
finishes. The model calculates nominal bit time from NBTP and the configured
FDCAN kernel frequency; U585 uses 96 MHz and G4/H523 use 170 MHz. Regression
contracts cover FIFO saturation, cancellation/reuse, reset, and serialization
between controllers. An aborted/reused slot cannot complete its replacement
from an obsolete request; pending host storage is bounded per controller/slot.

The wire uses conservative stuffing for the boards' extended-ID CAN FD frames
without BRS. It does not implement identifier-priority arbitration, electrical
noise, retransmission timing, or arbitrary bit-rate switching. Unassigned
`BusName` preserves instantaneous transmission for isolated register contracts.
Do not infer physical CAN throughput from this model alone.

GroundStation HITL routing and validation deadlines use the same virtual clock
as the MCU bay (`GS_SIM_CLOCK_FILE`). Pausing Renode pauses route aging; a board
reset does not reset the host clock. Partial/failed file reads retain the last
valid monotonic tick. A fixed wall-time divisor remains available for legacy
runs, but does not qualify current shared-clock latency measurements.

## Executed behavior

CAN reception is disabled while CCCR.INIT is set, including before firmware
startup and while stopped. Earlier wrapper versions incorrectly accumulated
pre-initialization frames. On U5 this could trigger an RX interrupt while HAL
was still READY, clear the event without draining the FIFO, and strand the
interrupt-driven receiver. Docker contracts check INIT suppression, started
reception, IRQ delivery, W1C clearing, and stop/restart behavior.

- Cortex-M4F/M33 instructions execute in pinned Renode virtual time.
- An exact `mcu` selects a bundled or inline silicon descriptor and constrains CPU model, platform, total flash/RAM capacity, flash geometry, security, and modeled OTA controllers. G491, H523, and U585 have real-board validation; other bundled descriptors have platform-contract validation.
- Direct firmware boot and combined-factory boot execute independently. Factory flash contains only the factory binary; MSP and PC come from its vector table, while ELFs provide symbols only.
- Selected CAN, UART, USB, SPI, I2C, timers, ADC, SDMMC, and DMA paths run through MMIO/wire models. STM32G4, H5, and U5 FDCAN use their fixed three-entry message-RAM geometry rather than configurable generic M_CAN RAM. Configured failure/disconnect schedules are passed into instruction-coupled sensor models.
- The G4 platform exposes USART1, USART2, and UART4 at their silicon MMIO and NVIC locations; linked gateway traffic can enter USART2 through the firmware-visible receive register and interrupt path.
- Embedded flash is executable `MappedMemory` and remains nonvolatile across
  peripheral resets. A CPU memory-access hook on the selected G4/H5/U5 flash profile
  observes the stores made by real firmware and enforces HAL unlock, status
  clearing, bank/page erase selection, aligned contiguous programming units,
  and one-to-zero transitions. Host-side image loading is sealed before reset
  execution so it cannot be mistaken for firmware programming.
- The structural flash interruption model classifies old-image, new-image, or
  recovery-required state at every operation boundary. Firmware-driven UART,
  CAN, USB, or SDMMC OTA executes the modeled controller receive path and records flash operations and a
  linked-symbol boot outcome. With `every_flash_operation`, each observed
  boundary (`erase_start`, `erase_complete`, or `program_unit`) is rerun in a
  fresh machine, power is cut at that exact boundary, flash is retained, and
  the real bootloader is executed and classified.
- Linked bays run in one deterministic clock domain and can require firmware activity probes at each endpoint.

## Explicit limits

The run report retains these explicit limitations:

- full RCC clock-tree propagation into every peripheral;
- cache timing, eviction, and stale-line coherency; cache maintenance/status is modeled;
- full STM32 GTZC peripheral/MPC programming. Cortex-M33 execution and SAU
  flash/RAM attribution are enforced for configured secure regions;
- transistor-level GPIO/ADC behavior. GPIO register configuration, pulls,
  open-drain release, initial external levels, wiring, and active-low board
  connections are modeled, as are deterministic ADC channel samples/noise;
- exhaustive behavior for every undocumented or unused register offset. The
  broad placeholder RAM regions have been removed, and strict MMIO faults on
  unmapped addresses, but an implemented model may return a documented reset
  value for an unsupported offset.

These limits must not be treated as passed tests. Hardware release qualification should pair simulator scenarios with target-board tests and captured SPI/I2C/CAN transactions, reset causes, fault registers, and interrupt ordering.

## Adding fidelity

Implement the smallest reference-manual surface required by a real board. Add a Renode smoke overlay under `renode/tests`, a Rust contract test, and a real linked-firmware probe. An unsupported register should be visible and attributable; broad zero-returning ranges must not be described as modeled peripherals.
# Pico-Fi link and Gateway UART timing

Current qualification uses the Pi’s native Ethernet TCP listener, discovered by
one gateway-side Pico-Fi client, then its UART link to the gateway. There is no
GroundStation-side Pico or I2C hop. The following mailbox scenario is retained
only for legacy regression coverage.

## Legacy I2C scenario (excluded from current qualification)

The UART Pico and I2C Pico are independent, continuously running devices.
GroundStation restart replaces only its host I2C session; UART reception and
transmission continue without that host. Partial I2C operations and broken replies
must not terminate the bridge. Unexpected bridge errors fail the bay run instead
of leaving a blocked terminal write and an indefinitely waiting time source.

The I2C mailbox follows `pico-fi/src/bridge/overwrite_queue.rs` (8 queued packets,
8,192 bytes, overwrite oldest on pressure) and `i2c_task::stage_response_packet`
(stage the newest queued packet when the current multi-slot transfer finishes).
It is not an unlimited lossless telemetry queue. Split UART sync bytes survive
nonblocking reads. Queue pressure must not evict a partially transmitted UART
frame: only whole pending packets may be overwritten, matching the physical
Pico byte-packet ring. Packet-count and byte-budget regression tests cover this
case. Host-to-Gateway writes submit one frame without blocking the reverse
direction; partial writes retain their offset. The USART model serializes RX
and TX with its configured BRR and 8N1 timers in virtual time. An additional
wall-time byte pacer previously created artificial inter-character IDLE gaps
and DMA restart losses. Radio interference and Pico Wi-Fi radio physics remain
outside this model.

The STM32 UART FIFOs are shared by CPU, timer and host-input execution and must
be thread-safe. Docker builds and `scripts/test-image.sh` run the actual C#
model through a concurrent 500,000-byte TX/RX integrity regression. This uses
narrow Renode API stubs to isolate synchronization; the real Renode register
contracts separately verify baud timing and status flags.

## Independent watchdog

G4, H5 and U5 platforms use the firmware's real IWDG key/prescaler/reload
register sequence, driven by an independent 32 kHz virtual clock. Configuration
latches synchronously. A timeout resets the machine and preserves RCC's IWDG
reset-cause flag until firmware clears RMVF. The model's reset counter survives
resets; unexpected `SEDS_IWDG_RESET` events fail single-board and linked-bay
qualification rather than hide a stopped task behind a reboot.

The register contract tests protected writes, feed timing, /4 and /256
prescalers, all three MCU reset-flag offsets, flag clearing and inactive reloads.
Window mode, early-warning interrupts, oscillator tolerance and debug-freeze
control are not modeled. Current board firmware does not enable those modes.


## Gateway field regression (2026-10-20)

The hardware baseline (`gateway-hardware-baseline-2026-10-20.json`, external archive)
retains three read-only samples from the exact matched gateway ELF, after the
DAQ/Pico updates and GroundStation restart. During the 7.18-second interval,
DAQ CAN ingress advances by 3,688 frames and its ring-drop count by 1,179
(32%). UART TX advances by 1,192 frames with zero pending frames, retries,
exhausted transfers or UART-send failures in all three snapshots. CAN and
network queue servicing account for about 96% of the interval. Valve sensor
reports still stall even while valve status arrives. These observations locate
loss before the Pico, but do not establish a single root cause for every drop.

The Ethernet transport model now matches the dual-core Pico firmware's two
128-frame / 16 KiB mailboxes. Occupancy charges one kind byte plus payload,
excluding the wire envelope. An in-flight UART or TCP frame lives outside its
mailbox, and partial writes retain ownership. Full uplink mailboxes reject new
whole frames, never accepted templates/receipts; downlink applies backpressure.
UART received while disconnected is discarded. I2C's legacy path is unchanged
and is outside current qualification. These are transport contracts: the model
does not execute RP2040 cores or model W5500 electrical/CPU timing.

Run the separate no-outage baseline with:

```sh
python3 scripts/test-route-recovery.py --workspace /path/to/snapshot \
  --image seds-firmware-simulator:gateway-fidelity-20261020 \
  --hardware-fill-profile --evidence-dir /path/to/evidence --timeout 3600
```

This selects P6 differential input, one gateway-side Pico-Fi UART at 1 Mbaud (checked against the actual
build cache), and 120 seconds of instruction-coupled execution with no scheduled
reset or link failure. The matching hosted backend must include the opt-in
`GS_SIM_VALIDATE_FILL_DELIVERY` observer. After 20 seconds of discovery warmup,
every ten-second window requires delivery of at least 99.5% of actual DAQ
publications, and valve battery/current/pressure to reach at least 0.9 Hz. The
configured rate cap is not substituted for measured producer counts. Arrival
gaps must remain below 500 ms for loadcells and 3 seconds for valve sensors.
Duplicate packet IDs cannot inflate counts. Only the selected 50 kg input counts.
Every failed window fails qualification; required passing windows prevent an
uninitialized or missing observer from being treated as success.

The earlier five correctly spaced sample intervals establish only a short
cadence observation, despite their old '250 Hz stream' log label. They do not
qualify sustained throughput. Graph-history row counts are decimated and must
not substitute for ingress counts. The new threshold is a minimum acceptance
floor, not a lossless-recording claim. Hardware-to-simulator timing agreement
must be reported explicitly: Renode is not cycle accurate, and no random drop
percentage or accelerated clock is used to manufacture the measured field result.
A passing legacy release run does not qualify this new profile.

The profile now rejects I2C, the manual TCP bypass, a second GS-side Pico, and
incorrect UART baud/endpoints. Its transport path is Pi main Ethernet → UDP
discovery/framed TCP → gateway-side Pico-Fi → USART2. HTTPS is disabled only for
the headless hosted validation process; production listener defaults are unchanged.

The first frozen 120-second Jupiter baseline failed: zero passing sustained
windows, about 20 KG1000 reports/second, and no selected P6 KG50 reports after
startup. Inspection found that the ADC model read only SCAN's low eight bits.
The real firmware selects P6 with bit 9 and temperature with bit 12. Both became
a zero-mask MUX/CH0 response, so P6 never set its completion flag and temperature
scans waited for their timeout. The model now scans all sixteen channel IDs,
returns signed differential CH2−CH3 data for P6, restarts from the highest enabled
ID, and supplies a nominal temperature fixture. The Mono regression executes the
actual C# peripheral with low-bit, P6-only, mixed P6/CH0, TEMP-only and ID15 scans.
This is a source-model correction, not a gateway throughput fix. Re-run the
unchanged sustained acceptance thresholds before attributing loss to the gateway.
