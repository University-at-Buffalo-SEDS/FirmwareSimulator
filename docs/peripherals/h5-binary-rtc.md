# STM32H5 binary RTC

`SedsStm32H5Rtc` models the H523 binary RTC used by the actual FC watchdog
recovery clock. The H523 platform registers it at `0x44007800`. RCC LSI readiness,
RTC clock selection/enable, PWR backup write access, APB access, WPR keys and
INIT/INITF are honored. The 32-bit SSR counts down in virtual time at
32 kHz / (PREDIV_A + 1), including fractional remainder and wrap. CPU reads use
the effective virtual timestamp; monitor reads also account for machine time.
Reads and wall time do not fabricate ticks. Backup configuration/counter survive
ordinary/IWDG resets; backup-domain reset clears them. RCC selection cannot be
silently stolen from another clock owner. LSE readiness follows enable, but RTC
calendar, LSE counting, alarms and UTC/calendar conversion are outside this subset.

Run the actual-source register contracts offline with the cached .NET test image:

```sh
docker run --rm --network=none --user "$(id -u):$(id -g)" \
  --env HOME=/tmp --env DOTNET_CLI_HOME=/tmp \
  --mount "type=bind,source=$PWD,target=/src" --workdir /src \
  --entrypoint dotnet seds-simulator-peripheral-tests:workspace-20261025 \
  run --project tests/h5-rtc/H5Rtc.csproj
```

The contracts cover negative write/source gates, oscillator/APB gating, nominal
1 kHz and fractional pacing, full-width wrap, monitor versus CPU time, retained
reset state and other RCC variants. No firmware safety state is overridden.

On 2026-10-25 an isolated real Renode 1.16.1 coldboot used unchanged frozen FC ELF
`4e03ca4cad878958c3a3b2396d0f63f0db87f2bf09c3126aba97c44baea131b3`, its
matching LaunchCore image, actual flash controller and both layout RAM regions.
After four virtual seconds, the old model produced `clock_ready=0` and
`g_fc_watchdog_resume_rejected=1`. The corrected model produced `clock_ready=1`,
`g_fc_watchdog_resume_rejected=0`, BDCR `0x0C008200`, ICSR `0x117`, PRER
`0x001F0000` and SSR `0xFFFFF134`. Both runs had zero CFSR. The proof exercised
actual FC clock initialization; it did not exercise Launch, physical outputs,
retained resume payload restoration, or any hardware. Standalone CAN has no peer;
its expected disconnected-CAN diagnostics do not qualify network behavior.

Local evidence: `.workspace-progress/h5-rtc-{register-contract,coldboot,coldboot-baseline}.log`
at the SEDS workspace root. These logs apply only to this source followup; frozen
linked candidate images are unchanged and require a new image to use this model.
