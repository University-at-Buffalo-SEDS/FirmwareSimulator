#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: test-image.sh IMAGE}"

check_renode() {
    local command="$1"
    shift
    local output
    output="$(docker run --rm --entrypoint /opt/renode/renode "$image" \
        --disable-xwt --console --execute "$command" 2>&1)"
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

catalog="$(docker run --rm "$image" list-mcus)"
for mcu in \
    stm32g431 stm32g441 stm32g471 stm32g473 stm32g474 stm32g483 stm32g484 stm32g491 stm32g4a1 \
    stm32h523 stm32h533 stm32h543 stm32h553 stm32h562 stm32h563 stm32h573 \
    stm32u575 stm32u585 stm32u595 stm32u599 stm32u5a5 stm32u5a9; do
    printf '%s\n' "$catalog" | grep -Fq "$mcu"
done

for arch in stm32 stm32g4 stm32h5 stm32u5; do
    docker run --rm "$image" self-test --arch "$arch"
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

# Every fixed-layout controller must expose all three TX entries as free after
# reset. G4 has two instances; H5 and U5 have one. All bundled part numbers map
# to one of these three runtime-checked platform profiles.
check_renode 'emulation CreateCANHub "fdcan-test" false; mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl; connector Connect sysbus.fdcan1 fdcan-test; sysbus ReadDoubleWord 0x400064C4; sysbus WriteDoubleWord 0x40006418 0; sysbus WriteDoubleWord 0x400064CC 1; sysbus ReadDoubleWord 0x400064D4; quit' '0x00000003' '0x00000001'
check_renode 'emulation CreateCANHub "fdcan-test" false; mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl; connector Connect sysbus.fdcan2 fdcan-test; sysbus ReadDoubleWord 0x400068C4; sysbus WriteDoubleWord 0x40006818 0; sysbus WriteDoubleWord 0x400068CC 1; sysbus ReadDoubleWord 0x400068D4; quit' '0x00000003' '0x00000001'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/stm32h523-peripherals.repl; sysbus WriteDoubleWord 0x08040000 0x12345678; sysbus WriteDoubleWord 0x40022004 0x45670123; sysbus WriteDoubleWord 0x40022004 0xCDEF89AB; sysbus WriteDoubleWord 0x40022028 0x80000024; sysbus ReadDoubleWord 0x08040000; physicalFlash GetOperationCount; physicalFlash GetOperationTrace; quit' '0xFFFFFFFF' '0x0000000000000002' 'erase_start,erase_complete'

check_renode 'emulation CreateCANHub "fdcan-test" false; mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; connector Connect sysbus.fdcan1 fdcan-test; sysbus ReadDoubleWord 0x4000A4C4; sysbus WriteDoubleWord 0x4000A418 0; sysbus WriteDoubleWord 0x4000A4CC 1; sysbus ReadDoubleWord 0x4000A4D4; quit' '0x00000003' '0x00000001'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32u585.repl; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/stm32u585-peripherals.repl; sysbus WriteDoubleWord 0x08100000 0x12345678; sysbus WriteDoubleWord 0x40022008 0x45670123; sysbus WriteDoubleWord 0x40022008 0xCDEF89AB; sysbus WriteDoubleWord 0x40022028 0x10802; sysbus ReadDoubleWord 0x08100000; physicalFlash GetOperationCount; physicalFlash GetOperationTrace; quit' '0xFFFFFFFF' '0x0000000000000002' 'erase_start,erase_complete'

check_renode 'emulation CreateCANHub "fdcan-test" false; mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32u585.repl; connector Connect sysbus.fdcan1 fdcan-test; sysbus ReadDoubleWord 0x4000A4C4; sysbus WriteDoubleWord 0x4000A418 0; sysbus WriteDoubleWord 0x4000A4CC 1; sysbus ReadDoubleWord 0x4000A4D4; quit' '0x00000003' '0x00000001'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/gpdma-cache-contract.repl; sysbus WriteDoubleWord 0x20000000 0x44332211; sysbus WriteDoubleWord 0x40020110 0x4040; sysbus WriteDoubleWord 0x40020118 4; sysbus WriteDoubleWord 0x4002011c 0x20000000; sysbus WriteDoubleWord 0x40020120 0x20000020; sysbus WriteDoubleWord 0x40020100 1; sysbus ReadDoubleWord 0x20000020; cache WriteDoubleWord 0 2; cache GetInvalidations; quit' '0x44332211' '0x0000000000000001'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/adc-contract.repl; sysbus WriteDoubleWord 0x50000030 0x80; sysbus WriteDoubleWord 0x50000008 5; sysbus ReadDoubleWord 0x50000040; quit' '0x00000FFF'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/tests/trustzone-contract.repl; cpu SAURegionNumber 0; cpu SAURegionBaseAddress 0x08002000; cpu SAURegionLimitAddress 0x08002FE1; cpu SAUControl 1; cpu TrustZoneEnabled; cpu SAUControl; quit' 'True' '0x00000001'

check_renode 'mach create; machine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32h523.repl; sdmmc CardCapacityBytes 4096; sdmmc GetCardPresent; sysbus WriteDoubleWord 0x46008008 512; sysbus WriteDoubleWord 0x4600800C 0x1010; sysbus ReadDoubleWord 0x46008010; sysbus ReadDoubleWord 0x46008014; sysbus ReadDoubleWord 0x46008034; python "monitor.Machine[\"sysbus.usb\"].InjectPacket(System.Array[System.Byte]([1,2,3]), 1)"; usb GetBytesInjected; quit' 'True' '0x00000010' '0x00000040' '0x0000000000000003'
