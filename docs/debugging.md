# Crash and register debugging

On a Renode command failure, the runner allows 250 ms for trailing exception
details before stopping the monitor. Linked runs retain the final Renode
output even when GroundStation also exits, so a host startup failure does not
hide an independent platform-loading error.

For raw monitor-output diagnostics, mount a writable directory into Docker and
set `FIRMWARE_SIM_DIAGNOSTICS_DIR` to that container path. Each standalone Renode
execution saves a uniquely named `renode-*.log` without overwriting earlier runs.
The directory must already exist and be writable by the container user. These
transcripts complement the CPU/probe report; they are not instruction traces.
Layouts with an SD card also save `sd-*.img` before the standalone firmware
machine is cleared. Inspect that FAT image using filesystem tools to validate
file contents, timestamps and recorded values; write counters alone cannot do so.
The capture contains completed sector writes, not data still buffered in firmware.

When a simulated phase fails after loading a layout, the CLI prints a JSON crash diagnostic before exiting. It contains the failing phase, causal error chain, and recent simulated events. CPU/fault registers are included only when they were read from Renode; later behavioral phases never fabricate register values.

Keep the JSON with the failing CI artifact and rerun with the same `--seed`. The PC is constrained to the configured application slot and stack pointers to the configured RAM size, which makes invalid layout/register state easy to spot.

The `execution.register_dump` values are read from the emulated Cortex-M after executing the linked ELF. When tracing is enabled, `execution.trace` points to the Renode instruction trace. On hardware, the hard-fault handler should persist the full architectural and SCB fault register set and the bootloader recovery transport should upload it.

Configured memory probes include every sample in the JSON report together with observed minima/maxima and the drop from the first post-startup sample to the final sample. Export stable `volatile uint32_t` or `ULONG` symbols for allocator availability, low-water level, allocation failures, panics, and lock failures. This attributes a soak failure to the actual running ELF rather than the behavioral traffic model.

Useful decoding commands for a real ELF are:

```sh
arm-none-eabi-addr2line -e build/Release/Board.elf -f -C 0x<pc>
arm-none-eabi-objdump -d -S build/Release/Board.elf
```
# GroundStation restart and simulated UART ownership

A simulated host power cycle kills and reaps the old process before restarting
it. Linux PTYs can retain `TIOCEXCL` while Renode keeps their master descriptor
open, even after GroundStation exits abruptly. The bay runner retains a
non-reading UART descriptor and clears that flag only after reaping the owning
host. This models terminal teardown on power cycle without recreating the radio
cable, consuming bytes, relaxing hardware serial ownership, or touching Pico-Fi
I2C sockets. A Linux regression covers busy-before-cleanup and repeated reopen.
