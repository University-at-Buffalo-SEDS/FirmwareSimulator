# STM32H7 support

Use `architecture: "stm32h7"` and an exact silicon-line name in `mcu`. The catalog includes all 20 lines:

| Lines | Internal flash maximum | Erase unit | Program unit |
| --- | --- | --- | --- |
| H723, H725, H733, H735 | 1 MiB | 128 KiB | 32 bytes |
| H730, H750 | 128 KiB | 128 KiB | 32 bytes |
| H742, H743, H745, H747, H753, H755, H757 | 2 MiB | 128 KiB | 32 bytes |
| H7A3, H7B3 | 2 MiB | 8 KiB | 16 bytes |
| H7B0 | 128 KiB | 8 KiB | 16 bytes |
| H7R3, H7R7, H7S3, H7S7 | 64 KiB | 8 KiB | 16 bytes |

Names use lowercase, for example `stm32h743` or `stm32h7s3`. As with the other families, these are silicon lines rather than individual package/order codes. Select the actual part's capacity in the board layout, within the catalog maximum.

The profiles execute Cortex-M7 instructions and expose RCC/PWR startup status, GPIO, USART1, TIM6, SPI1, configurable-message-RAM FDCAN1/2, USB FS and SDMMC1. Flash models implement bank-local unlock, sector erase, aligned programming, status clearing, operation tracing, power-cut injection and reset persistence. Classic H7, H7A/B and H7R/S use separate register layouts. H7R/S peripheral interrupts use their own platform profile.

The catalog's `ram_regions` records physical RAM banks. Board layouts can select banks or subranges; gaps and capacity overruns are rejected. H742 uses its smaller AXI SRAM and separated SRAM1/SRAM2. H72x/H73x and H7R/S use the maximum AXI allocation with minimum TCM allocation; runtime TCM repartitioning and ECC capacity changes are not modeled.

Example H743 memory layout for a 2 MiB part:

```json
{
  "flash_base": 134217728,
  "flash_size": 2097152,
  "ram_regions": [
    {"name": "dtcm", "base": 536870912, "size": 131072},
    {"name": "axi", "base": 603979776, "size": 524288},
    {"name": "ahb", "base": 805306368, "size": 294912}
  ],
  "bootloader_size": 131072,
  "slot_a_base": 134348800,
  "slot_a_size": 1966080,
  "erase_size": 131072,
  "write_alignment": 32,
  "sedsnet_pool": 4096
}
```

These are functional profiles, not board-qualified H7 firmware targets. H745/H747/H755/H757 execute only the M7 image: the M4, hardware semaphores and inter-core synchronization are not implemented. External XIP flash, DMA, cache coherency/timing, graphics, Ethernet, cryptographic accelerators, flash option bytes, bank swapping and partial flash-word force writes are outside these profiles. Native H7 FDCAN completes transmissions without modeling missing bus acknowledgments. H730/H750's single internal sector cannot independently hold an erasable bootloader and application. H7R/S bootflash support does not imply support for an application's external memory device.

Validation includes every catalog entry's geometry and RAM boundaries, overlay generation, and Renode instruction-level flash contracts for all three flash profiles (`renode/tests/h7-flash.resc`, `h7ab-flash.resc`, and `h7rs-flash.resc`). Run `cargo test` and `scripts/test-image.sh IMAGE` against a newly built image.

Hardware references: [ST H7 family overview](https://www.st.com/en/microcontrollers-microprocessors/stm32h7-series.html), [ST H7 CMSIS headers](https://github.com/STMicroelectronics/cmsis-device-h7/tree/master/Include), [ST H7RS CMSIS headers](https://github.com/STMicroelectronics/cmsis-device-h7rs/tree/main/Include), [H742/H743 datasheet, memory map](https://www.st.com/resource/en/datasheet/stm32h742vi.pdf), and [H7S3/S7 datasheet, embedded SRAM](https://www.st.com/resource/en/datasheet/stm32h7s3v8.pdf).
