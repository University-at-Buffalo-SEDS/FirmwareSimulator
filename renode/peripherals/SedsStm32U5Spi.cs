using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.SPI
{
    // Reuse the upstream SPI register/FIFO model; provide the transmit DMA
    // request when an enabled master starts a transfer. RX uses upstream's
    // per-packet DMARecieve output (the spelling is part of its API).
    public sealed class SedsStm32U5Spi : STM32H7_SPI, IDoubleWordPeripheral
    {
        public SedsStm32U5Spi(IMachine machine) : base(machine)
        {
            DMATransmit = new GPIO();
        }

        public new void WriteDoubleWord(long offset, uint value)
        {
            base.WriteDoubleWord(offset, value);
            if(offset == 0 && (base.ReadDoubleWord(0) & 0x201) == 0x201
                && (base.ReadDoubleWord(8) & 0x8000) != 0)
            {
                DMATransmit.Blink();
            }
        }

        public override void Reset()
        {
            base.Reset();
            if(DMATransmit != null) DMATransmit.Unset();
        }

        public GPIO DMATransmit { get; private set; }
    }
}
