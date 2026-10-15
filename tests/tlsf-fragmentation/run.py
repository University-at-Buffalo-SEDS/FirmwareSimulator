#!/usr/bin/env python3
"""Run the captured tiny-allocation fragmentation failure on a 32-bit Cortex-M."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tlsf-dir", type=Path, required=True, help="Board's third_party/tlsf directory")
    parser.add_argument("--cc", default="arm-none-eabi-gcc", help="ARM GCC compiler")
    parser.add_argument("--image", required=True, help="FirmwareSimulator Docker image with Renode")
    args = parser.parse_args()
    fixture = Path(__file__).resolve().parent
    compiler = shutil.which(args.cc) or args.cc
    nm = str(Path(compiler).with_name("arm-none-eabi-nm"))
    with tempfile.TemporaryDirectory(prefix="tlsf-arm-fragments-") as temp:
        out = Path(temp)
        # The simulator image runs as an unprivileged user.
        out.chmod(0o755)
        subprocess.run([compiler, "-mcpu=cortex-m4", "-mthumb", "-O2", "-ffreestanding",
            "-fno-builtin", "-DNDEBUG", "-ffunction-sections", "-fdata-sections", "-nostdlib",
            "-I"+str(args.tlsf_dir.resolve()), str(fixture/"fragment.c"),
            str(args.tlsf_dir.resolve()/"tlsf.c"), "-T", str(fixture/"fragment.ld"),
            "-Wl,--gc-sections", "-o", str(out/"fragment.elf")], check=True)
        symbols = {row.split()[-1]: int(row.split()[0],16)
            for row in subprocess.check_output([nm,str(out/"fragment.elf")],text=True).splitlines()
            if len(row.split()) == 3}
        (out/"memory.repl").write_text(
            "flash: Memory.MappedMemory @ sysbus 0x08000000\n    size: 0x80000\n"
            "ram: Memory.MappedMemory @ sysbus 0x20000000\n    size: 0x18000\n")
        (out/"test.resc").write_text(
            "mach create\nmachine LoadPlatformDescription @/opt/firmware-sim/renode/platforms/stm32g491.repl\n"
            "machine LoadPlatformDescription @/work/memory.repl\nsysbus LoadELF @/work/fragment.elf\n"
            'emulation RunFor "0.1"\n'
            'python "assert monitor.Machine[\\"sysbus\\"].ReadDoubleWord('
            +str(symbols["result"])+') == 0x51edcafe"\n'
            'echo "TLSF_ARM_SMALL_FRAGMENT_PASS"\nquit\n')
        result = subprocess.run(["docker","run","--rm","--entrypoint","/opt/renode/renode",
            "-v",str(out)+":/work:ro",args.image,"--disable-xwt","--console","-p","/work/test.resc"],
            check=True,capture_output=True,text=True)
        # Renode may return success after a script assertion; require its marker.
        output = result.stdout + result.stderr
        errors = ("AssertionError", "there was an error executing command",
                  "could not compile", "CPU abort", "Traceback")
        if "TLSF_ARM_SMALL_FRAGMENT_PASS" not in output or any(
                error.lower() in output.lower() for error in errors):
            raise RuntimeError(output)
        print("TLSF_ARM_SMALL_FRAGMENT_PASS")

if __name__ == "__main__":
    main()
