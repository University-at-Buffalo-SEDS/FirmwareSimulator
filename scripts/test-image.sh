#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: test-image.sh IMAGE}"
docker_command=(docker run)
if [[ -n "${SEDS_FIRMWARE_SIM_DOCKER_NETWORK:-}" ]]; then
    docker_command+=(--network "$SEDS_FIRMWARE_SIM_DOCKER_NETWORK")
fi

check_renode() {
    local command="$1"
    shift
    local output
    if ! output="$("${docker_command[@]}" --rm --entrypoint /opt/renode/renode "$image" \
        --disable-xwt --console --execute "$command" 2>&1)"; then
        printf '%s\n' "$output"
        return 1
    fi
    printf '%s\n' "$output"
    if printf '%s\n' "$output" | grep -Eiq \
        'there was an error executing command|fatal error|could not compile|error E[0-9]+|CPU abort|trying to execute code outside RAM or ROM'; then
        return 1
    fi
    local expected
    for expected in "$@"; do
        printf '%s\n' "$output" | grep -Fq "$expected" || {
            echo "missing Renode contract output: $expected" >&2
            return 1
        }
    done
}

catalog="$("${docker_command[@]}" --rm "$image" list-mcus)"
"${docker_command[@]}" --rm --entrypoint dotnet "$image" \
    /opt/firmware-sim/uart-check/UartConcurrency.dll
check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl; cpu IsHalted true; sysbus WriteDoubleWord 0x4000440c 17000; sysbus WriteDoubleWord 0x40004400 9; sysbus WriteDoubleWord 0x40004428 0x42; python "u = monitor.Machine[\"sysbus.usart2\"]; assert u.TransmittedBytes == 0; assert u.ReadDoubleWord(0x1c) & 0xc0 == 0; print(\"UART_TX_PENDING_PASS\")"; emulation RunFor "0.002s"; python "u = monitor.Machine[\"sysbus.usart2\"]; assert u.TransmittedBytes == 1; assert u.ReadDoubleWord(0x1c) & 0xc0 == 0xc0; print(\"UART_TX_BAUD_TIMING_PASS\")"; quit' 'UART_TX_PENDING_PASS' 'UART_TX_BAUD_TIMING_PASS'
# RX frames must not accumulate before HAL starts the controller. Exercise
# the actual IRQ line and retained FIFO behavior across stop/restart.
for mcu in stm32h523 stm32u585; do
    check_renode "mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/${mcu}.repl; python \"from Antmicro.Renode.Core.CAN import CANMessageFrame; c = monitor.Machine[\\\"sysbus.fdcan1\\\"]; f = CANMessageFrame(0x321, System.Array[System.Byte]([1,2,3])); c.OnFrameReceived(f); assert c.ReadDoubleWord(0x90) == 0; c.WriteDoubleWord(0x54, 0x2d); c.WriteDoubleWord(0x5c, 1); c.OnFrameReceived(f); assert c.ReadDoubleWord(0x90) == 0; assert not c.Int0.IsSet; c.WriteDoubleWord(0x18, 0); c.OnFrameReceived(f); assert c.ReadDoubleWord(0x90) & 7 == 1; assert c.Int0.IsSet; assert monitor.Machine[\\\"sysbus\\\"].ReadDoubleWord(0xe000e204) & 128; c.WriteDoubleWord(0x50, 1); assert not c.Int0.IsSet; c.WriteDoubleWord(0x18, 1); c.OnFrameReceived(f); assert c.ReadDoubleWord(0x90) & 7 == 1; c.WriteDoubleWord(0x18, 0); c.OnFrameReceived(f); assert c.ReadDoubleWord(0x90) & 7 == 2; assert c.Int0.IsSet; print(\\\"CAN_INIT_RX_IRQ_PASS\\\")\"; quit" 'CAN_INIT_RX_IRQ_PASS'
done
# Exercise actual CS framing (including split HAL transfers), all sensor IDs,
# and EXTI port selection, mask-independent pending edges and W1C clearing.
check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/flight-sensors.repl; python "s = monitor.Machine[\"sysbus.spi1.flightSensors\"]; assert s.Transmit(0x80) == 255; s.OnGPIO(0, False); s.Transmit(0x80); s.FinishTransmission(); assert s.Transmit(0) == 0; assert s.Transmit(0) == 0x60; s.OnGPIO(0, True); s.OnGPIO(1, False); s.Transmit(0x80); assert s.Transmit(0) == 0x0f; s.OnGPIO(1, True); s.OnGPIO(2, False); s.Transmit(0x80); assert s.Transmit(0) == 0; assert s.Transmit(0) == 0x1e; s.OnGPIO(2, True); e = monitor.Machine[\"sysbus.exti\"]; e.WriteDoubleWord(0x64, 0x02000000); e.WriteDoubleWord(0, 128); e.OnGPIO(7, True); assert e.ReadDoubleWord(12) == 0; e.OnGPIO(39, True); assert e.ReadDoubleWord(12) == 128; assert not e.Connections[7].IsSet; e.WriteDoubleWord(128, 128); assert e.Connections[7].IsSet; e.WriteDoubleWord(12, 128); assert not e.Connections[7].IsSet; print(\"FC_SENSOR_CS_EXTI_PASS\")"; quit' 'FC_SENSOR_CS_EXTI_PASS'
# A DMA register read must transfer command/dummy/ID through the real SPI
# controller and both DMA channels, without directly populating firmware RAM.
check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/flight-sensors.repl; spi1.flightSensors OnGPIO 0 false; sysbus WriteDoubleWord 0x20000000 0x80; sysbus WriteDoubleWord 0x40020090 0x80000; sysbus WriteDoubleWord 0x40020094 0; sysbus WriteDoubleWord 0x40020098 3; sysbus WriteDoubleWord 0x4002009c 0x40013030; sysbus WriteDoubleWord 0x400200a0 0x20000020; sysbus WriteDoubleWord 0x40020064 0x101; sysbus WriteDoubleWord 0x40020110 8; sysbus WriteDoubleWord 0x40020114 0x400; sysbus WriteDoubleWord 0x40020118 3; sysbus WriteDoubleWord 0x4002011c 0x20000000; sysbus WriteDoubleWord 0x40020120 0x40013020; sysbus WriteDoubleWord 0x400200e4 0x101; sysbus WriteDoubleWord 0x40013008 0xc007; sysbus WriteDoubleWord 0x40013004 3; sysbus WriteDoubleWord 0x40013000 1; sysbus WriteDoubleWord 0x40013000 0x201; python "assert monitor.Machine[\"sysbus\"].ReadDoubleWord(0x20000020) == 0x600000; assert monitor.Machine[\"sysbus.gpdma\"].GetCompletedTransfers() == 2; print(\"FC_SPI_DMA_PASS\")"; quit' 'FC_SPI_DMA_PASS'

# STM32H523 channel 0 is IRQ 27 (not IRQ 29 from a different DMA map).
check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/flight-sensors.repl; sysbus WriteDoubleWord 0x40020090 0x80008; sysbus WriteDoubleWord 0x40020094 0x200; sysbus WriteDoubleWord 0x40020098 4; sysbus WriteDoubleWord 0x4002009c 0x20000000; sysbus WriteDoubleWord 0x400200a0 0x20000020; sysbus WriteDoubleWord 0x40020064 0x101; python "assert monitor.Machine[\"sysbus\"].ReadDoubleWord(0xe000e200) & (1 << 27); print(\"FC_DMA_IRQ27_PASS\")"; quit' 'FC_DMA_IRQ27_PASS'

# HAL_SPI_Transmit packs an 8-bit register address and value into one
# halfword store. Both frames must reach the sensor, and reads retain CS.
check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/flight-sensors.repl; spi1.flightSensors OnGPIO 0 false; sysbus WriteDoubleWord 0x40013008 7; sysbus WriteDoubleWord 0x40013004 2; sysbus WriteDoubleWord 0x40013000 0x201; sysbus WriteWord 0x40013020 0x331b; spi1.flightSensors OnGPIO 0 true; python "s = monitor.Machine[\"sysbus.spi1.flightSensors\"]; s.OnGPIO(0, False); s.Transmit(0x9b); s.Transmit(0); assert s.Transmit(0) == 0x33; print(\"FC_PACKED_SPI_PASS\")"; quit' 'FC_PACKED_SPI_PASS'

# Match baro_read_reg: transmit command+dummy, keep GPIO CS low, then
# HAL_SPI_Receive switches COMM to RX-only and generates clocks without TXDR.
check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/flight-sensors.repl; spi1.flightSensors OnGPIO 0 false; sysbus WriteDoubleWord 0x40013008 7; sysbus WriteDoubleWord 0x40013004 2; sysbus WriteDoubleWord 0x40013000 0x201; sysbus WriteWord 0x40013020 0x0080; sysbus WriteDoubleWord 0x40013000 0; sysbus WriteDoubleWord 0x4001300c 0x440000; sysbus WriteDoubleWord 0x40013004 1; sysbus WriteDoubleWord 0x40013000 0x201; python "assert monitor.Machine[\"sysbus\"].ReadByte(0x40013030) == 0x60; print(\"FC_RX_ONLY_SPI_PASS\")"; quit' 'FC_RX_ONLY_SPI_PASS'

for mcu in \
    stm32g431 stm32g441 stm32g471 stm32g473 stm32g474 stm32g483 stm32g484 stm32g491 stm32g4a1 \
    stm32h523 stm32h533 stm32h543 stm32h553 stm32h562 stm32h563 stm32h573 \
    stm32u575 stm32u585 stm32u595 stm32u599 stm32u5a5 stm32u5a9; do
    printf '%s\n' "$catalog" | grep -Fq "$mcu"
done

for arch in stm32 stm32g4 stm32h5 stm32u5; do
    "${docker_command[@]}" --rm "$image" self-test --arch "$arch"
done

for mcu in stm32g491 stm32h523 stm32u585; do
    check_renode \
        "mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/${mcu}.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/${mcu}-peripherals.repl; quit"
done

for mcu in stm32g491 stm32h523 stm32u585; do
    case "$mcu" in
        stm32g491) stack=0x2001BFF0 ;;
        stm32h523) stack=0x2003FFF0 ;;
        stm32u585) stack=0x200BFFF0 ;;
    esac
    check_renode \
        "mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/${mcu}.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/${mcu}-peripherals.repl; cpu AssembleBlock 0x08000000 \"ldr r0, =0x40022000; movs r1, #5; str r1, [r0]; ldr r2, [r0]; b .\"; physicalFlash EndHostLoading; cpu SetRegister 13 ${stack}; cpu PC 0x08000000; emulation RunFor \"0.00001s\"; echo \"FLASH_ACR_READBACK\"; cpu GetRegister 2; quit" \
        'FLASH_ACR_READBACK' '0x5'
done

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/stm32g491-peripherals.repl; cpu AssembleBlock 0x08000000 "ldr r0, =0x40022008; ldr r1, =0x45670123; str r1, [r0]; ldr r1, =0xCDEF89AB; str r1, [r0]; ldr r0, =0x40022014; movs r1, #1; str r1, [r0]; ldr r0, =0x08010000; ldr r1, =0x12345678; str r1, [r0]; ldr r1, =0xABCDEF00; str r1, [r0, #4]; b ."; physicalFlash EndHostLoading; physicalFlash ArmPowerCut 1; cpu SetRegister 13 0x2001BFF0; cpu PC 0x08000000; emulation RunFor "0.0001s"; physicalFlash GetPowerCutTriggered; physicalFlash GetOperationTrace; sysbus ReadDoubleWord 0x08010000; python "from Antmicro.Renode.Core.CAN import CANMessageFrame; monitor.Machine[\"sysbus.fdcan1\"].OnFrameReceived(CANMessageFrame(0x321, System.Array[System.Byte]([1,2,3])))"; sysbus WriteDoubleWord 0x48000000 1; sysbus WriteDoubleWord 0x48000018 1; sysbus ReadDoubleWord 0x48000010; quit' 'True' 'program_unit' '0x12345678'

# The linked gateway receives through G491 USART2. Exercise the actual receive
# register rather than accepting a platform that merely reserves the address.
check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl; sysbus WriteDoubleWord 0x40004400 0x25; usart2 WriteChar 0x5A; sysbus ReadDoubleWord 0xE000E204; sysbus ReadDoubleWord 0x40004424; quit' '0x00000040' '0x0000005A'

# DAQ uses the U5 data cache and SDMMC FIFO writes for FileX provisioning.
check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32u585.repl; dcache1 WriteDoubleWord 0 0xB01; dcache1 ReadDoubleWord 4; sdmmc1 CardCapacityBytes 4194304; sysbus WriteDoubleWord 0x420C8008 0x10000; sysbus WriteDoubleWord 0x420C800C 0x1007; sysbus WriteDoubleWord 0x420C8028 4; sysbus WriteDoubleWord 0x420C8008 0; sysbus WriteDoubleWord 0x420C800C 0x1019; sysbus WriteDoubleWord 0x420C8080 0x44332211; sysbus WriteDoubleWord 0x420C800C 0x1011; sysbus ReadDoubleWord 0x420C8080; quit' '0x00000010' '0x44332211'

# Every fixed-layout controller must expose all three TX entries as free after
# reset. G4 has two instances; H5 and U5 have one. All bundled part numbers map
# to one of these three runtime-checked platform profiles.
check_renode 'emulation CreateCANHub "fdcan-test" false; mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl; connector Connect sysbus.fdcan1 fdcan-test; sysbus ReadDoubleWord 0x400064C4; sysbus WriteDoubleWord 0x40006418 0; sysbus WriteDoubleWord 0x400064CC 1; sysbus ReadDoubleWord 0x400064D4; quit' '0x00000003' '0x00000001'
check_renode 'emulation CreateCANHub "fdcan-test" false; mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl; connector Connect sysbus.fdcan2 fdcan-test; sysbus ReadDoubleWord 0x400068C4; sysbus WriteDoubleWord 0x40006818 0; sysbus WriteDoubleWord 0x400068CC 1; sysbus ReadDoubleWord 0x400068D4; quit' '0x00000003' '0x00000001'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/stm32h523-peripherals.repl; sysbus WriteDoubleWord 0x08040000 0x12345678; sysbus WriteDoubleWord 0x40022004 0x45670123; sysbus WriteDoubleWord 0x40022004 0xCDEF89AB; sysbus WriteDoubleWord 0x40022028 0x80000024; sysbus ReadDoubleWord 0x08040000; physicalFlash GetOperationCount; physicalFlash GetOperationTrace; quit' '0xFFFFFFFF' '0x0000000000000002' 'erase_start,erase_complete'

check_renode 'emulation CreateCANHub "fdcan-test" false; mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; connector Connect sysbus.fdcan1 fdcan-test; sysbus ReadDoubleWord 0x4000A4C4; sysbus WriteDoubleWord 0x4000A418 0; sysbus WriteDoubleWord 0x4000A4CC 1; sysbus ReadDoubleWord 0x4000A4D4; quit' '0x00000003' '0x00000001'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32u585.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/stm32u585-peripherals.repl; sysbus WriteDoubleWord 0x08100000 0x12345678; sysbus WriteDoubleWord 0x40022008 0x45670123; sysbus WriteDoubleWord 0x40022008 0xCDEF89AB; sysbus WriteDoubleWord 0x40022028 0x10802; sysbus ReadDoubleWord 0x08100000; physicalFlash GetOperationCount; physicalFlash GetOperationTrace; quit' '0xFFFFFFFF' '0x0000000000000002' 'erase_start,erase_complete'

check_renode 'emulation CreateCANHub "fdcan-test" false; mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32u585.repl; connector Connect sysbus.fdcan1 fdcan-test; sysbus ReadDoubleWord 0x4000A4C4; sysbus WriteDoubleWord 0x4000A418 0; sysbus WriteDoubleWord 0x4000A4CC 1; sysbus ReadDoubleWord 0x4000A4D4; quit' '0x00000003' '0x00000001'

# STM32H5/U5 CMSIS: channel 0 starts at +0x50, CTR1 at +0x90,
# CCR at +0x64. Exercise incrementing byte copies and W1C completion.
check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/gpdma-cache-contract.repl; sysbus WriteDoubleWord 0x20000000 0x44332211; sysbus WriteDoubleWord 0x40020090 0x80008; sysbus WriteDoubleWord 0x40020094 0x200; sysbus WriteDoubleWord 0x40020098 4; sysbus WriteDoubleWord 0x4002009c 0x20000000; sysbus WriteDoubleWord 0x400200a0 0x20000020; sysbus WriteDoubleWord 0x40020064 0x101; sysbus ReadDoubleWord 0x20000020; sysbus ReadDoubleWord 0x40020060; sysbus WriteDoubleWord 0x4002005c 0x100; sysbus ReadDoubleWord 0x40020060; cache WriteDoubleWord 0 2; cache GetInvalidations; quit' '0x44332211' '0x00000100' '0x00000000' '0x0000000000000001'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/adc-contract.repl; sysbus WriteDoubleWord 0x50000030 0x80; sysbus WriteDoubleWord 0x50000008 5; sysbus ReadDoubleWord 0x50000040; quit' '0x00000FFF'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/trustzone-contract.repl; cpu SAURegionNumber 0; cpu SAURegionBaseAddress 0x08002000; cpu SAURegionLimitAddress 0x08002FE1; cpu SAUControl 1; cpu TrustZoneEnabled; cpu SAUControl; quit' 'True' '0x00000001'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; sdmmc CardCapacityBytes 4096; sdmmc GetCardPresent; sysbus WriteDoubleWord 0x46008008 512; sysbus WriteDoubleWord 0x4600800C 0x1010; sysbus ReadDoubleWord 0x46008010; sysbus ReadDoubleWord 0x46008014; sysbus ReadDoubleWord 0x46008034; python "monitor.Machine[\"sysbus.usb\"].InjectPacket(System.Array[System.Byte]([1,2,3]), 1)"; usb GetBytesInjected; quit' 'True' '0x00000010' '0x00080040' '0x0000000000000003'
