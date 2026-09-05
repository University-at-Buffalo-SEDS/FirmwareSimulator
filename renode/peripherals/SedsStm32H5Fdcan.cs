using System;

using Antmicro.Renode.Core;
using Antmicro.Renode.Core.CAN;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Memory;

namespace Antmicro.Renode.Peripherals.CAN
{
    // Renode's FDCAN model completes every transmission, even when the CAN hub
    // has no other controller to acknowledge it. This wrapper can retain the
    // real three H5 TX slots so firmware observes the same full-FIFO failure as
    // an isolated physical board. Normal linked-bay execution remains ACKed.
    public sealed class SedsFixedFdcan : IDoubleWordPeripheral, ICAN, IKnownSize
    {
        public SedsFixedFdcan(IMachine machine, ArrayMemory messageRam)
        {
            inner = new STM32_FDCAN(machine, STM32Series.L5, messageRam);
        }

        public bool Acknowledged { get; set; } = true;
        public long Size => inner.Size;
        public GPIO Int0 => inner.Int0;
        public GPIO Int1 => inner.Int1;

        public event Action<CANMessageFrame> FrameSent
        {
            add => inner.FrameSent += value;
            remove => inner.FrameSent -= value;
        }

        public void OnFrameReceived(CANMessageFrame message) => inner.OnFrameReceived(message);

        public uint ReadDoubleWord(long offset)
        {
            if(Acknowledged) return inner.ReadDoubleWord(offset);
            switch(offset)
            {
            case 0x40:
                return Math.Min(transmitErrors, 255u);
            case 0x44:
                return 3u | (transmitErrors >= 128 ? 1u << 5 : 0) | (transmitErrors >= 256 ? 1u << 7 : 0);
            case 0xC4:
                var free = 3u - PopCount(pending);
                return free | (FirstFreeSlot(pending) << 16) | (free == 0 ? 1u << 21 : 0);
            case 0xC8:
                return pending;
            case 0xD4:
                return 0;
            default:
                return inner.ReadDoubleWord(offset);
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(!Acknowledged && offset == 0xCC)
            {
                pending |= value & 0x7u;
                transmitErrors = Math.Min(transmitErrors + 8u, 256u);
                return;
            }
            if(!Acknowledged && offset == 0xD0)
            {
                pending &= ~(value & 0x7u);
                return;
            }
            inner.WriteDoubleWord(offset, value);
        }

        public void Reset()
        {
            pending = 0;
            transmitErrors = 0;
            inner.Reset();
        }

        private static uint PopCount(uint value)
        {
            uint count = 0;
            for(; value != 0; value >>= 1) count += value & 1u;
            return count;
        }

        private static uint FirstFreeSlot(uint value)
        {
            for(uint slot = 0; slot < 3; slot++)
            {
                if((value & (1u << (int)slot)) == 0) return slot;
            }
            return 0;
        }

        private readonly STM32_FDCAN inner;
        private uint pending;
        private uint transmitErrors;
    }
}
