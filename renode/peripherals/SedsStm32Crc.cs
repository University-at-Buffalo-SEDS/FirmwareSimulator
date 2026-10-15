using System;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // STM32G4 programmable 32-bit CRC used by gateway_crc32.c. Bus writes
    // consume their actual access width; this exercises production word/byte
    // feeds, POL/INIT, reversal, and incremental reseeding. Peripheral-cycle
    // timing and non-32-bit polynomial modes are not modeled.
    public sealed class SedsStm32Crc : IBytePeripheral, IWordPeripheral, IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Crc() { Reset(); }
        public long Size => 0x400;
        public ulong BytesWritten { get; private set; }
        public byte ReadByte(long offset) => (byte)ReadDoubleWord(offset);
        public ushort ReadWord(long offset) => (ushort)ReadDoubleWord(offset);
        public uint ReadDoubleWord(long offset)
        {
            switch(offset) {
            case 0: return (control & 0x80) != 0 ? Reverse(state, 32) : state;
            case 4: return independent;
            case 8: return control;
            case 0x10: return initial;
            case 0x14: return polynomial;
            default: return 0;
            }
        }
        public void WriteByte(long offset, byte value)
        {
            if(offset == 0) Feed(value, 8); else WriteDoubleWord(offset, value);
        }
        public void WriteWord(long offset, ushort value)
        {
            if(offset == 0) Feed(value, 16); else WriteDoubleWord(offset, value);
        }
        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset) {
            case 0: Feed(value, 32); break;
            case 4: independent = value & 0xff; break;
            case 8:
                if((value & 0x18) != 0) throw new ArgumentException("CRC polynomial widths other than 32 are unsupported");
                control = value & 0xf8;
                if((value & 1) != 0) state = initial;
                break;
            case 0x10: initial = value; break;
            case 0x14: polynomial = value; break;
            }
        }
        private void Feed(uint value, int width)
        {
            var mode = (control >> 5) & 3;
            if(mode != 0) {
                var group = mode == 1 ? 8 : mode == 2 ? 16 : 32;
                if(group > width) throw new ArgumentException("CRC reversal group exceeds data access width");
                uint reversed = 0;
                for(var shift = 0; shift < width; shift += group) {
                    var mask = group == 32 ? uint.MaxValue : (1u << group) - 1;
                    reversed |= Reverse((value >> shift) & mask, group) << shift;
                }
                value = reversed;
            }
            for(var bit = width - 1; bit >= 0; --bit) {
                var feedback = ((state >> 31) ^ (value >> bit)) & 1;
                state <<= 1;
                if(feedback != 0) state ^= polynomial;
            }
            BytesWritten += (ulong)(width / 8);
        }
        private static uint Reverse(uint value, int width)
        {
            uint result = 0;
            for(var bit = 0; bit < width; ++bit) { result = (result << 1) | (value & 1); value >>= 1; }
            return result;
        }
        public void Reset()
        {
            initial = state = uint.MaxValue; polynomial = 0x04c11db7;
            control = independent = 0; BytesWritten = 0;
        }
        private uint state, initial, polynomial, control, independent;
    }
}
