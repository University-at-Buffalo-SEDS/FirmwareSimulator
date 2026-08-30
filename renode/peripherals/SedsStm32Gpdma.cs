using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.DMA
{
    // STM32 GPDMA v1 linear-transfer model used by H5/U5. Linked-list state is
    // retained in registers; linear blocks execute on enable or a routed DMA
    // request and expose per-channel completion/error flags and IRQs.
    public sealed class SedsStm32Gpdma : IDoubleWordPeripheral, IKnownSize, IGPIOReceiver, INumberedGPIOOutput
    {
        public SedsStm32Gpdma(IMachine machine, int channels = 16)
        {
            if(channels < 1 || channels > 16) throw new ArgumentOutOfRangeException("channels");
            this.machine = machine;
            this.channels = new Channel[channels];
            connections = new Dictionary<int, IGPIO>();
            for(var i = 0; i < channels; i++)
            {
                this.channels[i] = new Channel();
                connections[i] = new GPIO();
            }
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset == 0x0) return GlobalStatus;
            int index; long channelOffset;
            if(!Decode(offset, out index, out channelOffset)) return 0;
            var channel = channels[index];
            switch(channelOffset)
            {
            case 0x00: return channel.Control;
            case 0x04: return channel.Status;
            case 0x08: return channel.Status;
            case 0x0c: return 0;
            case 0x10: return channel.Transfer1;
            case 0x14: return channel.Transfer2;
            case 0x18: return channel.Block;
            case 0x1c: return channel.Source;
            case 0x20: return channel.Destination;
            case 0x24: return channel.Repeat;
            case 0x28: return channel.Link;
            default: return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            int index; long channelOffset;
            if(!Decode(offset, out index, out channelOffset)) return;
            var channel = channels[index];
            switch(channelOffset)
            {
            case 0x00:
                channel.Control = value;
                if((value & Enable) != 0 && IsMemoryToMemory(channel)) Transfer(index);
                break;
            case 0x08: channel.Status &= ~value; break;
            case 0x0c: channel.Status &= ~value; break;
            case 0x10: channel.Transfer1 = value; break;
            case 0x14: channel.Transfer2 = value; break;
            case 0x18: channel.Block = value; break;
            case 0x1c: channel.Source = value; break;
            case 0x20: channel.Destination = value; break;
            case 0x24: channel.Repeat = value; break;
            case 0x28: channel.Link = value; break;
            }
            UpdateInterrupt(index);
        }

        public void OnGPIO(int number, bool value)
        {
            if(value && number >= 0 && number < channels.Length && (channels[number].Control & Enable) != 0)
                Transfer(number);
        }

        public void TriggerChannel(int channel) { OnGPIO(channel, true); }
        public ulong GetBytesTransferred() { return bytesTransferred; }
        public ulong GetCompletedTransfers() { return completedTransfers; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get { return connections; } }
        public long Size { get { return 0x1000; } }

        public void Reset()
        {
            foreach(var channel in channels) channel.Reset();
            foreach(var output in connections.Values) output.Set(false);
            bytesTransferred = 0;
            completedTransfers = 0;
        }

        private void Transfer(int index)
        {
            var channel = channels[index];
            var count = channel.Block & 0xffff;
            if(count == 0) count = 0x10000;
            var sourceWidth = Width((channel.Transfer1 >> 4) & 3);
            var destinationWidth = Width((channel.Transfer1 >> 12) & 3);
            if(sourceWidth != destinationWidth || count > MaximumTransferBytes)
            {
                channel.Status |= DataTransferError;
                channel.Control &= ~Enable;
                UpdateInterrupt(index);
                return;
            }
            var sourceIncrement = (channel.Transfer1 & (1u << 6)) != 0;
            var destinationIncrement = (channel.Transfer1 & (1u << 14)) != 0;
            var bus = machine.GetSystemBus(this);
            try
            {
                for(uint position = 0; position < count; position += sourceWidth)
                {
                    for(uint byteIndex = 0; byteIndex < sourceWidth && position + byteIndex < count; byteIndex++)
                    {
                        var source = channel.Source + (sourceIncrement ? position + byteIndex : byteIndex);
                        var destination = channel.Destination + (destinationIncrement ? position + byteIndex : byteIndex);
                        bus.WriteByte(destination, bus.ReadByte(source, this), this);
                    }
                }
                bytesTransferred += count;
                completedTransfers++;
                channel.Block &= 0xffff0000;
                channel.Status |= TransferComplete;
            }
            catch(Exception)
            {
                channel.Status |= DataTransferError;
            }
            channel.Control &= ~Enable;
            UpdateInterrupt(index);
        }

        private void UpdateInterrupt(int index)
        {
            var channel = channels[index];
            var enabled = ((channel.Status & TransferComplete) != 0 && (channel.Control & TransferCompleteInterrupt) != 0)
                || ((channel.Status & DataTransferError) != 0 && (channel.Control & ErrorInterrupt) != 0);
            connections[index].Set(enabled);
        }

        private bool Decode(long offset, out int index, out long channelOffset)
        {
            index = (int)((offset - 0x100) / 0x80);
            channelOffset = (offset - 0x100) % 0x80;
            return offset >= 0x100 && index >= 0 && index < channels.Length;
        }

        private static uint Width(uint encoded) { return 1u << (int)encoded; }
        private static bool IsMemoryToMemory(Channel channel) { return ((channel.Transfer2 >> 16) & 0x7f) == 0; }
        private uint GlobalStatus
        {
            get
            {
                uint value = 0;
                for(var i = 0; i < channels.Length; i++) if(channels[i].Status != 0) value |= 1u << i;
                return value;
            }
        }

        private readonly IMachine machine;
        private readonly Channel[] channels;
        private readonly Dictionary<int, IGPIO> connections;
        private ulong bytesTransferred;
        private ulong completedTransfers;

        private const uint Enable = 1u;
        private const uint TransferCompleteInterrupt = 1u << 8;
        private const uint ErrorInterrupt = 1u << 9;
        private const uint TransferComplete = 1u << 8;
        private const uint DataTransferError = 1u << 1;
        private const uint MaximumTransferBytes = 16u * 1024u * 1024u;

        private sealed class Channel
        {
            public void Reset() { Control = Status = Transfer1 = Transfer2 = Block = Source = Destination = Repeat = Link = 0; }
            public uint Control, Status, Transfer1, Transfer2, Block, Source, Destination, Repeat, Link;
        }
    }
}
