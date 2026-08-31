using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Memory;

namespace Antmicro.Renode.Peripherals.CAN
{
    // STM32H5 implements ST's reduced M_CAN integration: its message RAM has
    // a fixed three-element layout rather than the configurable Bosch layout.
    // Renode 1.16.1 only exposes that geometry under the otherwise identical
    // L5 series selector, so keep the compatibility detail inside this H5
    // peripheral instead of describing H5 silicon as L5 in the platform.
    public sealed class SedsStm32H5Fdcan : STM32_FDCAN
    {
        public SedsStm32H5Fdcan(IMachine machine, ArrayMemory messageRam)
            : base(machine, STM32Series.L5, messageRam)
        {
        }
    }
}
