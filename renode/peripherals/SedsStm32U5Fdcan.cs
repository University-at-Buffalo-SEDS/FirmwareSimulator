using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Memory;

namespace Antmicro.Renode.Peripherals.CAN
{
    // STM32U5 implements ST's reduced M_CAN integration with the same fixed
    // 0x350-byte, three-element message-RAM geometry as L5. Renode 1.16.1
    // exposes that engine only through its L5 selector, so contain the
    // compatibility alias in a U5-named peripheral.
    public sealed class SedsStm32U5Fdcan : STM32_FDCAN
    {
        public SedsStm32U5Fdcan(IMachine machine, ArrayMemory messageRam)
            : base(machine, STM32Series.L5, messageRam)
        {
        }
    }
}
