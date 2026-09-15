using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.SPI
{
    // Renode's H7 SPI has RX DMA signalling but no TX request output.
    // Its unbounded TX FIFO can accept the programmed DMA block when enabled.
    public sealed class SedsStm32Spi : STM32H7_SPI, IDoubleWordPeripheral, IWordPeripheral, IBytePeripheral
    {
        public SedsStm32Spi(IMachine machine) : base(machine) { DMATransmit = new GPIO(); }
        public GPIO DMATransmit { get; private set; }
        public new void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0x20) { WriteData(value, 4); return; }
            if(offset == 0x0c && (base.ReadDoubleWord(0) & 1) == 0)
                communication = value & 0x60000;
            base.WriteDoubleWord(offset, value);
            var ready = (base.ReadDoubleWord(0) & 1) != 0
                && (base.ReadDoubleWord(8) & 0x8000) != 0;
            if(ready && !txReady) DMATransmit.Blink();
            txReady = ready;
            if(offset == 0 && (value & 0x201) == 0x201
                && communication == 0x40000)
            {
                // In master receive-only mode the controller generates SCK
                // itself. HAL_SPI_Receive never writes dummy bytes to TXDR.
                var frames = base.ReadDoubleWord(4) & 0xffff;
                for(uint i = 0; i < frames; i++) base.WriteDoubleWord(0x20, 0xff);
            }
        }
        public new void WriteWord(long offset, ushort value)
        {
            if(offset == 0x20) WriteData(value, 2);
            else WriteDoubleWord(offset, value);
        }
        public new void WriteByte(long offset, byte value)
        {
            if(offset == 0x20) WriteData(value, 1);
            else WriteDoubleWord(offset, value);
        }
        public new uint ReadDoubleWord(long offset)
        {
            if(offset == 0x30) return ReadData(4);
            if(offset == 0x0c) return (base.ReadDoubleWord(offset) & ~0x60000u) | communication;
            return base.ReadDoubleWord(offset);
        }
        public new ushort ReadWord(long offset)
        { return (ushort)(offset == 0x30 ? ReadData(2) : ReadDoubleWord(offset)); }
        public new byte ReadByte(long offset)
        { return (byte)(offset == 0x30 ? ReadData(1) : ReadDoubleWord(offset)); }
        private int FrameBytes
        {
            get { var bits = (base.ReadDoubleWord(8) & 31) + 1;
                return bits <= 8 ? 1 : bits <= 16 ? 2 : 4; }
        }
        private void WriteData(uint value, int accessBytes)
        {
            // HAL packs two 8-bit frames into a halfword TXDR store. The
            // base H7 model otherwise silently transmits just the first.
            var stride = FrameBytes;
            for(var i = 0; i < accessBytes; i += stride)
                base.WriteDoubleWord(0x20, value >> (8 * i));
        }
        private uint ReadData(int accessBytes)
        {
            uint value = 0;
            var stride = FrameBytes;
            for(var i = 0; i < accessBytes && (base.ReadDoubleWord(0x14) & 1) != 0; i += stride)
                value |= base.ReadDoubleWord(0x30) << (8 * i);
            return value;
        }
        public override void Reset()
        {
            base.Reset();
            txReady = false;
            communication = 0;
            if(DMATransmit != null) DMATransmit.Unset();
        }
        private bool txReady;
        private uint communication;
    }
}
