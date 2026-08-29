using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Storage
{
    // Minimal STM32 SDMMC host model for a configured but absent card. CMD0
    // completes, while commands requiring a card report command timeout. This
    // exercises the firmware's real optional-media failure path without a long
    // instruction-counted HAL polling delay.
    public sealed class SedsStm32Sdmmc : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Sdmmc()
        {
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset == 0x34)
            {
                var commandIndex = command & 0x3fu;
                return commandIndex == 0 ? 1u << 7 : 1u << 2;
            }
            uint value;
            return registers.TryGetValue(offset, out value) ? value : 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0x0c) command = value;
            if(offset != 0x38) registers[offset] = value;
        }

        public void Reset()
        {
            registers.Clear();
            command = 0;
        }

        public long Size { get { return 0x400; } }

        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
        private uint command;
    }
}
