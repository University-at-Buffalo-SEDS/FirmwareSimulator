using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Time;
using Antmicro.Renode.Peripherals.Timers;

namespace Antmicro.Renode.Peripherals.DMA
{
    // The upstream G0 model services memory-to-peripheral blocks eagerly.
    // USART1/2 DMA uses byte-paced TX and RXNE-gated RX, including DMAMUX
    // selection, CNDTR, circular reload and HT/TC interrupts. Other channels
    // retain the upstream model. No firmware RAM is populated out of band.
    public sealed class SedsStm32Dma : IDoubleWordPeripheral, IKnownSize, IGPIOReceiver, INumberedGPIOOutput, IDMA
    {
        public SedsStm32Dma(IMachine machine, int numberOfChannels, uint frequency = 170000000, int muxChannelOffset = 0)
        {
            this.machine = machine;
            this.muxChannelOffset = muxChannelOffset;
            fallback = new STM32G0DMA(machine, numberOfChannels);
            channels = new Channel[numberOfChannels];
            outputs = new Dictionary<int, IGPIO>();
            for(var i = 0; i < numberOfChannels; i++)
            {
                var index = i;
                channels[i] = new Channel();
                outputs[i] = new GPIO();
                fallback.Connections[i].Connect(this, 100 + i);
                channels[i].Timer = new LimitTimer(machine.ClockSource, frequency, this,
                    "uartDma" + i, limit: 1, direction: Direction.Ascending, enabled: false, eventEnabled: true);
                channels[i].Timer.LimitReached += () => Transfer(index);
            }
        }
        public uint ReadDoubleWord(long offset)
        {
            if(offset == 0)
            {
                uint flags = fallback.ReadDoubleWord(offset);
                for(var i = 0; i < channels.Length; i++) flags |= channels[i].Flags << (4 * i);
                return flags;
            }
            int index; long register;
            if(Decode(offset, out index, out register) && channels[index].Paced)
            {
                var channel = channels[index];
                switch(register)
                {
                    case 0: return channel.Control;
                    case 4: return channel.Count;
                    case 8: return channel.Peripheral;
                    case 12: return channel.Memory;
                }
            }
            return fallback.ReadDoubleWord(offset);
        }
        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 4)
            {
                fallback.WriteDoubleWord(offset, value);
                for(var i = 0; i < channels.Length; i++)
                {
                    var clear = (value >> (4 * i)) & 15;
                    channels[i].Flags &= (clear & 1) != 0 ? 0u : ~clear;
                    if((channels[i].Flags & 14) == 0) channels[i].Flags = 0;
                    UpdateInterrupt(i);
                }
                return;
            }
            int index; long register;
            if(!Decode(offset, out index, out register)) { fallback.WriteDoubleWord(offset, value); return; }
            var channel = channels[index];
            switch(register)
            {
                case 4: channel.Count = value & 0xffff; break;
                case 8: channel.Peripheral = value; break;
                case 12: channel.Memory = value; break;
                case 0:
                    // HAL changes interrupt-enable bits while EN remains set
                    // (notably disabling HTIE at half transfer). This is not
                    // a new transfer: preserve the address and remaining count.
                    var wasEnabled = (channel.Control & 1) != 0;
                    channel.Control = value;
                    var registerOffset = channel.Peripheral & 0x3ff;
                    var uartBase = channel.Peripheral - registerOffset;
                    channel.Receive = registerOffset == 0x24 && (value & 16) == 0;
                    channel.Paced = (uartBase == Uart1Base || uartBase == Uart2Base)
                        && (value & (1u << 14)) == 0
                        && (channel.Receive || (registerOffset == 0x28 && (value & 16) != 0));
                    channel.UartBase = uartBase;
                    if(!wasEnabled || (value & 1) == 0) channel.Timer.Enabled = false;
                    if(channel.Paced)
                    {
                        // Clear fallback enable: it must never copy a whole
                        // UART frame into a bounded FIFO in one bus operation.
                        fallback.WriteDoubleWord(offset, value & ~1u);
                        if((value & 1) != 0 && !wasEnabled)
                        {
                            if((value & 0x0f40) != 0 || channel.Count == 0)
                            {
                                channel.Flags |= 9; // transfer error
                                channel.Control &= ~1u;
                                UpdateInterrupt(index);
                                return;
                            }
                            channel.StartCount = channel.Count;
                            channel.CurrentMemory = channel.Memory;
                            var divisor = machine.GetSystemBus(this).ReadDoubleWord(channel.UartBase + 0x0c, this);
                            channel.WireTicks = Math.Max(1ul, (ulong)divisor * 10);
                            channel.Timer.Limit = channel.Receive ? Math.Max(1ul, channel.WireTicks / 10) : channel.WireTicks + 1;
                            channel.Timer.Value = 0;
                            channel.Timer.Enabled = true;
                        }
                        UpdateInterrupt(index);
                        return;
                    }
                    break;
            }
            fallback.WriteDoubleWord(offset, value);
        }
        private void Transfer(int index)
        {
            var channel = channels[index];
            if(!channel.Paced || (channel.Control & 1) == 0) { channel.Timer.Enabled = false; return; }
            var bus = machine.GetSystemBus(this);
            var request = bus.ReadDoubleWord(DmamuxBase + (uint)((muxChannelOffset + index) * 4), this) & 0x7f;
            var expected = (channel.UartBase == Uart1Base ? 24u : 26u) + (channel.Receive ? 0u : 1u);
            var dmaEnable = channel.Receive ? 64u : 128u;
            var ready = channel.Receive ? 32u : 128u;
            if(request != expected || (bus.ReadDoubleWord(channel.UartBase + 8, this) & dmaEnable) == 0
                || (bus.ReadDoubleWord(channel.UartBase + 0x1c, this) & ready) == 0)
            {
                channel.Timer.Limit = Math.Max(1ul, channel.WireTicks / 10);
                return;
            }
            channel.Timer.Limit = channel.Receive ? Math.Max(1ul, channel.WireTicks / 10) : channel.WireTicks + 1;
            if(channel.Receive)
                bus.WriteByte(channel.CurrentMemory, bus.ReadByte(channel.Peripheral, this), this);
            else
                bus.WriteByte(channel.Peripheral, bus.ReadByte(channel.CurrentMemory, this), this);
            if((channel.Control & 128) != 0) channel.CurrentMemory++;
            channel.Count--;
            if(channel.Count == channel.StartCount / 2) channel.Flags |= 5;
            if(channel.Count == 0)
            {
                channel.Flags |= 3;
                if((channel.Control & 32) != 0)
                {
                    channel.Count = channel.StartCount;
                    channel.CurrentMemory = channel.Memory;
                }
                else
                {
                    channel.Control &= ~1u;
                    channel.Timer.Enabled = false;
                }
            }
            UpdateInterrupt(index);
        }
        public void OnGPIO(int number, bool value)
        {
            if(number >= 100 && number < 100 + channels.Length)
            {
                channels[number - 100].FallbackIRQ = value;
                UpdateInterrupt(number - 100);
            }
            else fallback.OnGPIO(number, value);
        }
        public void RequestTransfer(int channel)
        {
            if(channel > 0 && channel <= channels.Length && channels[channel - 1].Paced) return;
            fallback.RequestTransfer(channel);
        }
        private void UpdateInterrupt(int index)
        {
            var c = channels[index];
            outputs[index].Set(c.FallbackIRQ || ((c.Flags & 2) != 0 && (c.Control & 2) != 0)
                || ((c.Flags & 4) != 0 && (c.Control & 4) != 0)
                || ((c.Flags & 8) != 0 && (c.Control & 8) != 0));
        }
        private bool Decode(long offset, out int index, out long register)
        {
            index = (int)((offset - 8) / 20);
            register = (offset - 8) % 20;
            return offset >= 8 && index < channels.Length && register <= 12 && register % 4 == 0;
        }
        public void Reset()
        {
            fallback.Reset();
            foreach(var c in channels)
            {
                c.Timer.Reset(); c.Timer.Enabled = false;
                c.Control = c.Count = c.Peripheral = c.Memory = c.Flags = c.StartCount = c.CurrentMemory = 0;
                c.Paced = c.FallbackIRQ = false;
            }
            foreach(var output in outputs.Values) output.Set(false);
        }
        public IReadOnlyDictionary<int, IGPIO> Connections { get { return outputs; } }
        public int NumberOfChannels { get { return channels.Length; } }
        public long Size { get { return fallback.Size; } }
        private sealed class Channel
        {
            public uint Control, Count, Peripheral, Memory, Flags, StartCount, CurrentMemory;
            public bool Paced, Receive, FallbackIRQ;
            public uint UartBase;
            public LimitTimer Timer;
            public ulong WireTicks;
        }
        private const uint Uart1Base = 0x40013800, Uart2Base = 0x40004400, DmamuxBase = 0x40020800;
        private readonly int muxChannelOffset;
        private readonly IMachine machine;
        private readonly STM32G0DMA fallback;
        private readonly Channel[] channels;
        private readonly Dictionary<int, IGPIO> outputs;
    }
}
