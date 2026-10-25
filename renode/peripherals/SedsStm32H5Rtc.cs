using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Time;
using Antmicro.Renode.Utilities;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Timers
{
    // H5 binary RTC subset: independent LSI, protected initialization and retained
    // 32-bit down-counter. Calendar/alarm/UTC functions are intentionally absent.
    public sealed class SedsStm32H5Rtc : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32H5Rtc(IMachine machine,
            Antmicro.Renode.Peripherals.Miscellaneous.SedsStm32Rcc rcc,
            Antmicro.Renode.Peripherals.Miscellaneous.SedsStm32Pwr pwr)
        {
            this.machine = machine;
            this.rcc = rcc;
            this.pwr = pwr;
            lastUs = NowUs();
            rcc.H5RtcClockChanged += ClockChanged;
            rcc.H5BackupReset += BackupReset;
            BackupReset();
        }

        public uint ReadDoubleWord(long offset)
        {
            UpdateCounter();
            if(!BusEnabled) return 0;
            switch(offset)
            {
                case 0x08: return binaryMode == Binary ? counter : prescaler & 0x7FFFu;
                case 0x0C: return 7u | binaryMode | (init ? Init : 0u)
                    | (init && ClockReady ? InitReady : 0u) | (initialized ? 0x10u : 0u);
                case 0x10: return prescaler;
                case 0x18: return control;
                default: return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            UpdateCounter();
            if(!BusEnabled || !pwr.BackupWriteEnabled) return;
            if(offset == 0x24)
            {
                if(value == 0xCAu) { unlockStage = true; unlocked = false; }
                else if(value == 0x53u && unlockStage) { unlocked = true; unlockStage = false; }
                else { unlocked = unlockStage = false; }
                return;
            }
            if(!unlocked) return;
            if(offset == 0x0C)
            {
                bool wasInit = init;
                bool requested = (value & Init) != 0;
                if((wasInit || requested) && ClockReady) binaryMode = value & 0x300u;
                init = requested;
                if(wasInit && !init) initialized = true;
            }
            else if(offset == 0x10 && init && ClockReady)
            {
                prescaler = value & 0x007F7FFFu;
                counter = uint.MaxValue;
                remainder = 0;
            }
            else if(offset == 0x18) control = value;
            running = ClockReady && !init && binaryMode == Binary;
        }

        // A CPU/IWDG reset must not erase the backup-domain counter/configuration.
        public void Reset() { UpdateCounter(); unlocked = unlockStage = false; }
        public long Size { get { return 0x400; } }

        private void BackupReset()
        {
            prescaler = 0x007F00FFu;
            binaryMode = control = 0;
            counter = uint.MaxValue;
            remainder = 0;
            init = initialized = running = unlocked = unlockStage = false;
            lastUs = NowUs();
        }
        private void ClockChanged()
        {
            UpdateCounter();
            running = ClockReady && !init && binaryMode == Binary;
        }
        private void UpdateCounter()
        {
            var now = NowUs();
            if(running && now >= lastUs)
            {
                // Keeping the fractional numerator avoids rounding the LSI divider.
                ulong denominator = 1000000UL * (((prescaler >> 16) & 0x7Fu) + 1UL);
                ulong numerator = (now - lastUs) * 32000UL + remainder;
                counter = unchecked(counter - (uint)(numerator / denominator));
                remainder = numerator % denominator;
            }
            lastUs = now;
        }
        private bool BusEnabled { get { return (rcc.ReadDoubleWord(0xA8) & (1u << 21)) != 0; } }
        private bool ClockReady
        {
            get
            {
                var control = rcc.ReadDoubleWord(0xF0);
                return (control & 0x00018300u) == 0x00008200u
                    && (control & 0x0C000000u) == 0x0C000000u;
            }
        }
        private ulong NowUs()
        {
            // CPU reads can occur inside a partially executed time quantum; monitor
            // reads have no effective CPU domain and must use machine virtual time.
            return Math.Max((ulong)machine.ElapsedVirtualTime.TimeElapsed.TotalMicroseconds,
                (ulong)TimeDomainsManager.Instance.GetEffectiveVirtualTimeStamp().TimeElapsed.TotalMicroseconds);
        }
        private const uint Binary = 0x100u, Init = 0x80u, InitReady = 0x40u;
        private readonly IMachine machine;
        private readonly Antmicro.Renode.Peripherals.Miscellaneous.SedsStm32Rcc rcc;
        private readonly Antmicro.Renode.Peripherals.Miscellaneous.SedsStm32Pwr pwr;
        private uint counter, prescaler, binaryMode, control;
        private ulong lastUs, remainder;
        private bool init, initialized, running, unlocked, unlockStage;
    }
}
