# STM32G4 backup registers

`SedsStm32G4Tamp` models the 32 G4 TAMP backup words at offsets `0x100..0x17C`,
mapped at `0x40002400` in the G491 platform. Reads return stored data; the model
contains no board marker constants. RCC `APB1ENR1.RTCAPBEN` gates bus access and
PWR `CR1.DBP` gates writes. Ordinary peripheral, machine and IWDG resets retain
the words. RCC APB1ENR1 resets to `0x00000400`, so RTCAPBEN remains enabled
and retained words are readable immediately after MCU reset. PWR DBP resets
cleared, so new writes remain blocked. A newly created instance
starts at zero. An allowed RCC `BDCR.BDRST` assertion clears the words and holds
them reset until released; DBP protects this backup-domain control register.

This is a backup-register subset, not an RTC, calendar or tamper input model.
The existing generic PWR register model does not enforce the PWREN bus clock;
this change does not add that behavior. Clearing PWREN after DBP was programmed
does not clear the stored DBP permission. No firmware recovery guard is bypassed.

The register offsets and reset contracts follow ST
[RM0440](https://www.st.com/resource/en/reference_manual/dm00355726-stm32g4-series-advanced-arm-based-32-bit-mcus-stmicroelectronics.pdf),
section 7.4.17 for APB1ENR1 reset `0x00000400`, and section 7.4.27 for retained,
DBP-protected BDCR. Clock-off negative tests explicitly disable RTCAPBEN;
they do not assume MCU reset disables it.

The motivating saved run observed unmapped writes at `0x40002578/0x4000257C`
and a failed read at `0x40002578`. The preserved AB ELF identifies the read as
`board_abort_latch_prepare_recovery` checking its actual retained marker.
That failure reaches firmware IO before the flash transaction. Adding a model
is not evidence that the old run recovered, or that a later recovery will pass
every other exclusion and durable-write check.

Proposed qualification is `tests/g4-tamp/G4Tamp.csproj` against the actual model
sources, the unchanged `tests/h5-rtc/H5Rtc.csproj` regression, and
`tests/test_g4_tamp.py`. The first Python method exercises the actual C# source
with lightweight Renode API stubs. The second requires an explicitly selected
cached Renode image, mounts these exact model/platform files, and checks real
MMIO mapping, machine-reset retention, protected backup reset and marker
readback failure controls. It does not run board firmware or qualify recovery.

On 2026-11-07, direct Roslyn compilation and execution of the actual model and
causal sources passed, including LSCO retention during backup-domain reset.
The unchanged H5 RTC fixture also passed against the shared System model.
Real Renode 1.16.1 passed the G4 platform/MMIO/reset/readback script with zero
warnings. Local receipts are under
`.workspace-progress/sim-g4-backup-model-20261107/execution7/actual1`.
These are model contracts; a newly built image and a separately authorized
firmware recovery scene are still required. Never substitute a constant
successful readback, a generic RAM reset, or a changed board guard.
