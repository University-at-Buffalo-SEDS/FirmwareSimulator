# FirmwareSimulator v0.4.12

- Keep Pico-Fi device state and serial transport alive across GroundStation restarts.
- Preserve queued UART bytes and external GPIO inputs through concurrency and MCU reset.
- Capture linked-bay memory probes separately from console logs; reject missing,
  duplicate and out-of-range samples.
- Support repeated service-only and selectable firmware-group restart regressions.
  Reset command counters still require new execution and fresh returned state.
- Bundle GroundStation using the published SEDSNet v4.0.33 dependency.

Qualification includes the initial 16-second linked gate, Gateway-only and
fill-group 120-second restart tests, and two complete 600-second seven-board
plus GroundStation soaks. Both long runs passed all 20 scheduled command/state
responses, discovery and traffic attribution, and memory/stack bounds. No
recorded allocator failures, panics or HardFaults occurred. Detailed evidence
and earlier negative controls are in [route recovery](route-recovery.md).

Finite simulation does not guarantee indefinite uptime or every physical
power-loss condition. Hardware validation and separate OTA/SD-content checks
remain necessary. Response bounds use the simulator's existing scaled host
validation clock, not measured hardware latency.
