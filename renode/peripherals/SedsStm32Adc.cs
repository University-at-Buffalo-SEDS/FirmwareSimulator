using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Sensors
{
    // Small STM32 ADC register model. It preserves configuration registers and
    // completes conversions immediately with deterministic channel samples.
    public sealed class SedsStm32Adc : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Adc()
        {
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset == 0x00) return status;
            if(offset == 0x40)
            {
                status &= ~(1u << 2); // EOC is cleared by reading DR.
                sample = (sample + 73u) & 0xfffu;
                return sample;
            }
            uint value;
            return registers.TryGetValue(offset, out value) ? value : 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0x00)
            {
                status &= ~value; // STM32 ADC ISR is write-one-to-clear.
                return;
            }
            if(offset == 0x08)
            {
                // Calibration completes synchronously. Enabling sets ADRDY;
                // starting a conversion sets EOC/EOS and exposes a new sample.
                value &= ~(1u << 31);
                if((value & 1u) != 0) status |= 1u;
                if((value & (1u << 2)) != 0) status |= (1u << 2) | (1u << 3);
            }
            registers[offset] = value;
        }

        public void Reset()
        {
            registers.Clear();
            status = 0;
            sample = 2048;
        }

        // Each STM32 ADC instance occupies 0x100 bytes. Keeping instances
        // separate preserves independent CR/ISR/DR state on multi-ADC parts.
        public long Size { get { return 0x100; } }

        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
        private uint status;
        private uint sample;
    }
}
