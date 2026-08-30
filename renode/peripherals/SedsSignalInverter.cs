using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.GPIOPort;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // Board-level active-low wire adapter. It deliberately has no MMIO range;
    // a board overlay connects a producer to input 0 and Output to the sink.
    public sealed class SedsSignalInverter : IPeripheral, IGPIOReceiver
    {
        public SedsSignalInverter()
        {
            Output = new GPIO();
            Reset();
        }

        public GPIO Output { get; private set; }

        public void OnGPIO(int number, bool value)
        {
            if(number != 0) return;
            Output.Set(!value);
        }

        public void Reset()
        {
            Output.Set(true);
        }
    }
}
