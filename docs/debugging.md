# Crash and register debugging

When a simulated phase fails after loading a layout, the CLI prints a JSON crash diagnostic before exiting. It contains the failing phase, causal error chain, and recent simulated events. CPU/fault registers are included only when they were read from Renode; later behavioral phases never fabricate register values.

Keep the JSON with the failing CI artifact and rerun with the same `--seed`. The PC is constrained to the configured application slot and stack pointers to the configured RAM size, which makes invalid layout/register state easy to spot.

The `execution.register_dump` values are read from the emulated Cortex-M after executing the linked ELF. When tracing is enabled, `execution.trace` points to the Renode instruction trace. On hardware, the hard-fault handler should persist the full architectural and SCB fault register set and the bootloader recovery transport should upload it.

Configured memory probes include every sample in the JSON report together with observed minima/maxima and the drop from the first post-startup sample to the final sample. Export stable `volatile uint32_t` or `ULONG` symbols for allocator availability, low-water level, allocation failures, panics, and lock failures. This attributes a soak failure to the actual running ELF rather than the behavioral traffic model.

Useful decoding commands for a real ELF are:

```sh
arm-none-eabi-addr2line -e build/Release/Board.elf -f -C 0x<pc>
arm-none-eabi-objdump -d -S build/Release/Board.elf
```
