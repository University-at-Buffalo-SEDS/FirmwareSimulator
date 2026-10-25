using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Sensors
{
    // Small STM32 ADC register model. It preserves configuration registers and
    // completes conversions immediately with deterministic channel samples.
    public sealed class SedsStm32Adc : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Adc(uint bits = 12, uint channels = 1, string samples = "", uint noiseLsb = 0, ulong failureEvery = 0, ulong disconnectAfter = ulong.MaxValue, string stimulusPath = "")
        {
            this.bits = bits;
            this.stimulusPath = stimulusPath;
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
                PollStimulus();
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

        // Explicit external sensor input; never changes conversion timing or
        // DMA/IRQ behavior. Bounds apply before changing any channel sample.
        public void SetChannelSample(uint channel, uint value)
        {
            if(channel >= channelSamples.Length || value >= (1ul << (int)bits))
                throw new System.ArgumentOutOfRangeException("sample");
            channelSamples[channel] = value;
        }
        public ulong StimulusRejected { get; private set; }
        private void PollStimulus()
        {
            // No-file deployments do no additional filesystem operations.
            if(stimulusPath.Length == 0 || stimulusReads++ % 10 != 0) return;
            try
            {
                // Ten pressure DR reads =20ms at the unchanged500Hz ADC rate.
                // Bound the complete fixture read and validate atomically.
                var text = new char[1025];
                int count;
                using(var stream = new System.IO.StreamReader(stimulusPath))
                    count = stream.ReadBlock(text, 0, text.Length);
                if(count > 1024) throw new System.FormatException("oversized sensor fixture");
                var parts = new string(text, 0, count).Trim().Split(',');
                if(parts.Length != channelSamples.Length) throw new System.FormatException("channel count");
                var next = new uint[parts.Length];
                for(var i = 0; i < next.Length; i++)
                    if(!uint.TryParse(parts[i], out next[i]) || next[i] >= (1ul << (int)bits))
                        throw new System.FormatException("invalid sensor sample");
                for(var i = 0; i < next.Length; i++) SetChannelSample((uint)i, next[i]);
            }
            catch(System.IO.IOException) { RejectStimulus(); }
            catch(System.UnauthorizedAccessException) { RejectStimulus(); }
            catch(System.FormatException) { RejectStimulus(); }
        }
        private void RejectStimulus()
        {
            if(StimulusRejected != ulong.MaxValue) StimulusRejected++;
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
                uint previous;
                registers.TryGetValue(offset, out previous);
                var regularStart = (value & 4u) != 0 && (previous & 4u) == 0;
                // ADEN/ADSTART/JADSTART are read-set, not ordinary RW bits.
                // HAL masks these command properties when setting another:
                // writing ADSTART alone must retain the latched enable bit.
                value |= previous & 0xdu;
                // Calibration completes synchronously. Enabling sets ADRDY;
                // starting a conversion sets EOC/EOS and exposes a new sample.
                value &= ~(1u << 31);
                // Hardware self-clears disable/stop commands and clears the
                // corresponding start bits once an immediate modeled
                // conversion has completed. Retaining ADSTP made the U5 HAL
                // reject the next channel in a polling sequence.
                if((value & (1u << 1)) != 0)
                    value &= ~((1u << 1) | 1u);
                if((value & (1u << 4)) != 0)
                    value &= ~((1u << 4) | (1u << 2));
                if((value & (1u << 5)) != 0)
                    value &= ~((1u << 5) | (1u << 3));
                if((value & 1u) != 0) status |= 1u;
                if(regularStart && (value & (1u << 2)) != 0)
                {
                    conversions++;
                    if(conversions > disconnectAfter) status &= ~((1u << 2) | (1u << 3));
                    else if(failureEvery != 0 && conversions % failureEvery == 0) status |= 1u << 4;
                    else status |= (1u << 2) | (1u << 3);
                    uint config;
                    registers.TryGetValue(0x0c, out config);
                    // External-triggered conversions remain armed between
                    // timer requests. Software-triggered ones self-clear.
                    if((config & 0xc00) == 0) value &= ~(1u << 2);
                }
            }
            registers[offset] = value;
        }

        public void Reset()
        {
            registers.Clear();
            status = 0;
            random = 1;
            conversions = 0;
        }

        // Each STM32 ADC instance occupies 0x100 bytes. Keeping instances
        // separate preserves independent CR/ISR/DR state on multi-ADC parts.
        public long Size { get { return 0x100; } }

        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
        private uint status;
        private uint random;
        private ulong conversions;
        private readonly string stimulusPath;
        private ulong stimulusReads;
        private readonly uint bits;
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
