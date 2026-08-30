using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public sealed class SedsStm32Rcc : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Rcc(string variant)
        {
            if(variant != "g4" && variant != "h5" && variant != "u5") throw new ArgumentException("unknown RCC variant");
            this.variant = variant;
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            uint value;
            registers.TryGetValue(offset, out value);
            if(offset == 0)
            {
                if(variant == "g4") value |= 0x400u | ((value & (1u << 24)) << 1);
                else if(variant == "h5") value |= 5u | ((value & 0x15011101u) << 1);
                else value |= 0x500u | ((value & 0x05011101u) << 1);
            }
            else if(variant == "g4" && offset == 0x08) value = (value & ~0xcu) | ((value & 3u) << 2);
            else if(variant == "g4" && offset == 0x98 && (value & 1) != 0) value |= 2;
            else if(variant == "h5" && offset == 0x1c) value = (value & ~0x18u) | ((value & 3u) << 3);
            else if(variant == "u5" && offset == 0xf0) value |= (value & 0x04000000u) << 1;
            else if(variant == "u5" && offset == 0x1c) value = (value & ~0x0cu) | ((value & 3u) << 2);
            else if(variant == "u5" && offset == 0x10) value = (value & ~0x38u) | ((value & 7u) << 3);
            return value;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            registers[offset] = value;
            writes++;
            if((variant == "g4" && offset == 0x08) || (variant != "g4" && offset == 0x1c))
                clockSwitches++;
        }

        public ulong GetWrites() { return writes; }
        public ulong GetClockSwitches() { return clockSwitches; }
        public long Size { get { return 0x400; } }
        public void Reset() { registers.Clear(); writes = clockSwitches = 0; }

        private readonly string variant;
        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
        private ulong writes, clockSwitches;
    }

    public sealed class SedsStm32Pwr : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Pwr(string variant)
        {
            if(variant != "g4" && variant != "h5" && variant != "u5") throw new ArgumentException("unknown PWR variant");
            this.variant = variant;
        }

        public uint ReadDoubleWord(long offset)
        {
            uint value;
            registers.TryGetValue(offset, out value);
            if(variant == "h5")
            {
                value |= 1u << 13;
                if(offset == 0x14) value |= 1u << 3;
            }
            else if(variant == "u5")
            {
                value |= 1u << 10;
                if(offset == 0x3c) value |= 0x30000;
                else if(offset == 0x0c) value |= 1u << 14;
            }
            return value;
        }

        public void WriteDoubleWord(long offset, uint value) { registers[offset] = value; }
        public long Size { get { return 0x400; } }
        public void Reset() { registers.Clear(); }
        private readonly string variant;
        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
    }

    public sealed class SedsStm32Syscfg : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Syscfg(string variant)
        {
            if(variant != "g4" && variant != "h5" && variant != "u5") throw new ArgumentException("unknown SYSCFG variant");
            this.variant = variant;
        }

        public uint ReadDoubleWord(long offset)
        {
            uint value;
            if(registers.TryGetValue(offset, out value)) return value;
            this.Log(LogLevel.Warning, "Unhandled {0} SYSCFG read at offset 0x{1:X}", variant, offset);
            unhandled++;
            return 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset >= 0 && offset < Size && (offset & 3) == 0) registers[offset] = value;
            else { this.Log(LogLevel.Warning, "Unhandled {0} SYSCFG write at offset 0x{1:X}", variant, offset); unhandled++; }
        }

        public ulong GetUnhandledAccesses() { return unhandled; }
        public long Size { get { return 0x400; } }
        public void Reset() { registers.Clear(); unhandled = 0; }
        private readonly string variant;
        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
        private ulong unhandled;
    }
}
