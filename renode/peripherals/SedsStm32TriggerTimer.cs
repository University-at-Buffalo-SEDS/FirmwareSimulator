using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
namespace Antmicro.Renode.Peripherals.Timers
{
    // Preserve the STM32 base timer/counter/interrupt implementation. Its MMS
    // field is only tagged upstream; ADC TRGO requests need its stored value.
    public sealed class SedsStm32TriggerTimer : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32TriggerTimer(IMachine machine, uint frequency = 170000000, uint initialLimit = 0xffff)
        {
            timer = new STM32_Timer(machine, frequency: frequency, initialLimit: initialLimit);
        }
        public uint ReadDoubleWord(long offset)
        {
            return timer.ReadDoubleWord(offset) | (offset == 4 ? masterMode : 0u);
        }
        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 4) { masterMode = value & 0x70; value &= ~0x70u; }
            timer.WriteDoubleWord(offset, value);
        }
        public void Reset() { masterMode = 0; timer.Reset(); }
        public GPIO IRQ { get { return timer.IRQ; } }
        public long Size { get { return timer.Size; } }
        private uint masterMode;
        private readonly STM32_Timer timer;
    }
}
