using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Sensors
{
    // Small STM32 ADC register model. It preserves configuration registers and
    // completes conversions immediately with deterministic channel samples.
    public sealed class SedsStm32Adc : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Adc(uint bits = 12, uint channels = 1, string samples = "", uint noiseLsb = 0, ulong failureEvery = 0, ulong disconnectAfter = ulong.MaxValue)
        {
            this.bits = bits;
            this.channels = channels;
            this.noiseLsb = noiseLsb;
            channelSamples = new uint[channels];
            var configured = samples.Split(new[] { ',' }, System.StringSplitOptions.RemoveEmptyEntries);
            for(var i = 0; i < channelSamples.Length; i++)
                channelSamples[i] = i < configured.Length ? uint.Parse(configured[i]) : (1u << (int)bits) / 2u;
            this.failureEvery = failureEvery;
            this.disconnectAfter = disconnectAfter;
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset == 0x00) return status;
            if(offset == 0x40)
            {
                status &= ~(1u << 2); // EOC is cleared by reading DR.
                var selected = SelectedChannel;
                var baseSample = channelSamples[selected];
                if(noiseLsb == 0) return baseSample;
                random = random * 1664525u + 1013904223u;
                var span = noiseLsb * 2u + 1u;
                var signedNoise = (int)(random % span) - (int)noiseLsb;
                return (uint)System.Math.Max(0, System.Math.Min((1 << (int)bits) - 1, (int)baseSample + signedNoise));
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
                if((value & (1u << 2)) != 0)
                {
                    conversions++;
                    if(conversions > disconnectAfter) status &= ~((1u << 2) | (1u << 3));
                    else if(failureEvery != 0 && conversions % failureEvery == 0) status |= 1u << 4;
                    else status |= (1u << 2) | (1u << 3);
                }
            }
            registers[offset] = value;
        }

        public void Reset()
        {
            registers.Clear();
            status = 0;
            sample = 2048;
            random = 1;
            conversions = 0;
        }

        // Each STM32 ADC instance occupies 0x100 bytes. Keeping instances
        // separate preserves independent CR/ISR/DR state on multi-ADC parts.
        public long Size { get { return 0x100; } }

        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
        private uint status;
        private uint sample;
        private uint random;
        private ulong conversions;
        private readonly uint bits;
        private readonly uint channels;
        private readonly uint[] channelSamples;
        private readonly uint noiseLsb;
        private readonly ulong failureEvery;
        private readonly ulong disconnectAfter;

        private int SelectedChannel
        {
            get
            {
                uint sequence;
                registers.TryGetValue(0x30, out sequence);
                var channel = (int)((sequence >> 6) & 0x1f);
                return channel < channelSamples.Length ? channel : 0;
            }
        }
    }
}
