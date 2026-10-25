using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Time;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public sealed class SedsStm32Rcc : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Rcc(string variant, SedsStm32Pwr pwr = null)
        {
            if(variant != "g4" && variant != "h5" && variant != "u5" && variant != "h7" && variant != "h7rs") throw new ArgumentException("unknown RCC variant");
            this.variant = variant;
            this.pwr = pwr;
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            uint value;
            registers.TryGetValue(offset, out value);
            if(offset == ResetFlagsOffset) value |= resetFlags;
            if(variant == "h5" && offset == 0xF0)
            {
                value &= ~((1u << 27) | 2u);
                if((value & (1u << 26)) != 0) value |= 1u << 27;
                if((value & 1u) != 0) value |= 2u;
            }
            if(offset == 0)
            {
                if(variant == "g4") value |= 0x400u | ((value & (1u << 24)) << 1);
                else if(variant == "h5") value |= 5u | ((value & 0x15011101u) << 1);
                else if(variant == "h7" || variant == "h7rs") value |= 4u | ((value & 0x15011080u) << 1);
                else value |= 0x500u | ((value & 0x05011101u) << 1);
            }
            else if((variant == "h7" || variant == "h7rs") && offset == 0x10) value = (value & ~0x38u) | ((value & 7u) << 3);
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
            if(offset == ResetFlagsOffset)
            {
                if((value & (1u << 23)) != 0) resetFlags = 0;
                // Reset-cause flags are read-only; RMVF is a write-one clear.
                value &= ~0xFF800000u;
            }
            if(variant == "h5" && offset == 0xF0)
            {
                uint previous;
                registers.TryGetValue(offset, out previous);
                const uint protectedBits = 0x000183FDu;
                value &= ~((1u << 27) | 2u); // Ready flags are read-only.
                if(pwr != null && !pwr.BackupWriteEnabled)
                    value = (value & ~protectedBits) | (previous & protectedBits);
                if((value & (1u << 16)) != 0)
                {
                    value &= ~0x000083FDu;
                    if((previous & (1u << 16)) == 0 && H5BackupReset != null) H5BackupReset();
                }
                else if((previous & 0x300u) != 0)
                    value = (value & ~0x300u) | (previous & 0x300u);
            }
            registers[offset] = value;
            writes++;
            if(variant == "h5" && (offset == 0xF0 || offset == 0xA8) && H5RtcClockChanged != null)
                H5RtcClockChanged();
            if((variant == "g4" && offset == 0x08)
                || ((variant == "h7" || variant == "h7rs") && offset == 0x10)
                || ((variant == "h5" || variant == "u5") && offset == 0x1c))
                clockSwitches++;
        }

        public ulong GetWrites() { return writes; }
        public ulong GetClockSwitches() { return clockSwitches; }
        public long Size { get { return 0x400; } }
        public void Reset()
        {
            uint backup;
            registers.TryGetValue(0xF0, out backup);
            registers.Clear();
            if(variant == "h5") registers[0xF0] = backup;
            writes = clockSwitches = 0;
            if(H5RtcClockChanged != null) H5RtcClockChanged();
        }
        public event Action H5RtcClockChanged;
        public event Action H5BackupReset;

        public void NoteWatchdogReset() { resetFlags |= 1u << 29; }
        private long ResetFlagsOffset { get { return variant == "g4" ? 0x94 : (variant == "h5" || variant == "u5" ? 0xF4 : -1); } }
        // Reset cause survives a peripheral/machine reset until firmware clears RMVF.
        private uint resetFlags;
        private readonly string variant;
        private readonly SedsStm32Pwr pwr;
        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
        private ulong writes, clockSwitches;
    }

    // Functional IWDG model for the register sequence used by G4/H5/U5 firmware.
    // Time is driven by the independent 32 kHz clock, not CPU or ThreadX progress.
    public sealed class SedsStm32Iwdg : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Iwdg(IMachine machine, SedsStm32Rcc rcc)
        {
            this.machine = machine;
            this.rcc = rcc;
            timer = new LimitTimer(machine.ClockSource, 32000, this, "iwdg",
                limit: 16384, direction: Direction.Ascending, enabled: false, eventEnabled: true);
            timer.LimitReached += Expire;
            Reset();
        }
        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
                case 4: return prescaler;
                case 8: return reload;
                // Configuration latches synchronously; no update remains pending.
                case 12: return 0;
                case 16: return 0xFFF;
                default: return 0;
            }
        }
        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0)
            {
                if(value == 0x5555) { unlocked = true; return; }
                unlocked = false;
                if(value == 0xCCCC && !timer.Enabled) { Reload(); timer.Enabled = true; }
                else if(value == 0xAAAA && timer.Enabled) Reload();
                return;
            }
            if(!unlocked) return;
            if(offset == 4) prescaler = Math.Min(value & 7u, 6u);
            else if(offset == 8) reload = value & 0xFFFu;
            else if(offset == 16 && (value & 0xFFFu) != 0xFFFu)
                this.Log(LogLevel.Warning, "IWDG window mode is not modeled");
        }
        private void Reload()
        {
            timer.Limit = ((ulong)reload + 1) * (4UL << (int)prescaler);
            timer.Value = 0;
        }
        private void Expire()
        {
            timer.Enabled = false;
            resetCount++;
            this.Log(LogLevel.Error, "SEDS_IWDG_RESET count={0}", resetCount);
            rcc.NoteWatchdogReset();
            machine.RequestReset();
        }
        public ulong GetResetCount() { return resetCount; }
        public long Size { get { return 0x400; } }
        public void Reset()
        {
            timer.Enabled = false;
            timer.Value = 0;
            prescaler = 0; reload = 0xFFF; unlocked = false;
        }
        private readonly IMachine machine;
        private readonly SedsStm32Rcc rcc;
        private readonly LimitTimer timer;
        private uint prescaler, reload;
        private bool unlocked;
        // Test diagnostic survives resets so repeated recovery cannot hide failures.
        private ulong resetCount;
    }

    public sealed class SedsStm32Pwr : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Pwr(string variant)
        {
            if(variant != "g4" && variant != "h5" && variant != "u5" && variant != "h7" && variant != "h7rs") throw new ArgumentException("unknown PWR variant");
            this.variant = variant;
        }

        public uint ReadDoubleWord(long offset)
        {
            uint value;
            registers.TryGetValue(offset, out value);
            if(variant == "h7")
            {
                if(offset == 0x04 || offset == 0x18) value |= 1u << 13;
            }
            else if(variant == "h7rs")
            {
                if(offset == 0x04 || offset == 0x14) value |= 2u;
            }
            else if(variant == "h5")
            {
                value |= 1u << 13;
                if(offset == 0x14) value |= 1u << 3;
            }
            else if(variant == "u5")
            {
                if(offset == 0x0c)
                {
                    // VOSR: voltage scaling and EPOD transitions complete
                    // synchronously in the functional model.
                    value |= (1u << 14) | (1u << 15);
                }
                else if(offset == 0x3c)
                {
                    // SVMSR reports the voltage range selected in VOSR plus
                    // ACTVOSRDY. Cube HAL waits for both VOSRDY and ACTVOSRDY.
                    uint vosr;
                    registers.TryGetValue(0x0c, out vosr);
                    value = (value & ~0x30000u) | (vosr & 0x30000u);
                    value |= 1u << 15;
                }
            }
            return value;
        }

        public bool BackupWriteEnabled
        {
            get { uint value; return variant == "h5" && registers.TryGetValue(0x24, out value) && (value & 1u) != 0; }
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
            if(variant != "g4" && variant != "h5" && variant != "u5" && variant != "h7" && variant != "h7rs") throw new ArgumentException("unknown SYSCFG variant");
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
