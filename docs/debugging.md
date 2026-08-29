# Crash and register debugging

When a simulated phase fails after loading a layout, the CLI prints a JSON crash diagnostic before exiting. It contains:

- R0–R12, MSP, PSP, LR, PC, xPSR, CONTROL, PRIMASK, BASEPRI, and FAULTMASK
- CFSR, HFSR, DFSR, MMFAR, BFAR, and AFSR
- SFSR and SFAR for Cortex-M33 STM32H5/U5 boards
- the failing phase, causal error chain, and recent simulated events

Keep the JSON with the failing CI artifact and rerun with the same `--seed`. The PC is constrained to the configured application slot and stack pointers to the configured RAM size, which makes invalid layout/register state easy to spot.

These registers are deterministic behavioral state, not values recovered by executing the ARM binary. For a real device crash, the firmware hard-fault handler should persist the same register set and the bootloader recovery transport should upload it. For instruction-accurate simulated values, add a CPU backend that calls the same `CrashDiagnostic` serialization surface.

Useful decoding commands for a real ELF are:

```sh
arm-none-eabi-addr2line -e build/Release/Board.elf -f -C 0x<pc>
arm-none-eabi-objdump -d -S build/Release/Board.elf
```

