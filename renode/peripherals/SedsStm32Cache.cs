using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Memory
{
    // STM32 ICACHE control/status and monitor-counter model. Renode's CPU
    // translation cache remains the execution backend; maintenance commands
    // complete synchronously and preserve firmware-visible ordering/status.
    public sealed class SedsStm32Cache : IDoubleWordPeripheral, IKnownSize
    {
        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case 0x00: return control;
            case 0x04: return status;
            case 0x08: return interruptEnable;
            case 0x10: return hitMonitor;
            case 0x14: return missMonitor;
            default: return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case 0x00:
                control = value & ~(1u << 1);
                if((value & (1u << 1)) != 0) { invalidations++; status |= 1u << 1; }
                break;
            case 0x08: interruptEnable = value; break;
            case 0x0c: status &= ~value; break;
            case 0x10: hitMonitor = value; break;
            case 0x14: missMonitor = value; break;
            }
        }

        public ulong GetInvalidations() { return invalidations; }
        public bool GetEnabled() { return (control & 1) != 0; }
        public long Size { get { return 0x400; } }
        public void Reset() { control = status = interruptEnable = hitMonitor = missMonitor = 0; invalidations = 0; }

        private uint control, status, interruptEnable, hitMonitor, missMonitor;
        private ulong invalidations;
    }
}
