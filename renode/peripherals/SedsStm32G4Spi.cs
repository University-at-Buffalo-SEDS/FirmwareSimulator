using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.SPI
{
    // G4 DR is a 16-bit FIFO window: a halfword access packs two 8-bit
    // frames. The upstream byte-only STM32SPI model stalls Cube's block reads
    // at the two-byte RXNE threshold after transmitting only one frame.
    public sealed class SedsStm32G4Spi : NullRegistrationPointPeripheralContainer<ISPIPeripheral>,
        IDoubleWordPeripheral, IWordPeripheral, IBytePeripheral, IKnownSize
    {
        public SedsStm32G4Spi(IMachine machine) : base(machine)
        {
            IRQ = new GPIO(); DMAReceive = new GPIO(); DMASend = new GPIO();
            Reset();
        }
        public long Size { get { return 0x400; } }
        public GPIO IRQ { get; private set; }
        public GPIO DMAReceive { get; private set; }
        public GPIO DMASend { get; private set; }
        public byte ReadByte(long offset) { return (byte)Read(offset, 1); }
        public ushort ReadWord(long offset) { return (ushort)Read(offset, 2); }
        public uint ReadDoubleWord(long offset) { return Read(offset, 2); }
        private uint Read(long offset, int bytes)
        {
            switch(offset)
            {
            case 0: return control1;
            case 4: return control2;
            case 8:
                // RXNE follows FRXTH for 8-bit frames. TX is synchronous and
                // empty after each bus access; BSY and FTLVL therefore clear.
                return 2u | (fifo.Count >= Threshold ? 1u : 0u)
                    | (overrun ? 64u : 0u)
                    | (uint)(fifo.Count == 0 ? 0 : fifo.Count == 1 ? 1 : fifo.Count < 4 ? 2 : 3) << 9;
            case 12:
                uint result = 0;
                for(int i = 0; i < bytes && fifo.Count != 0; i++) result |= (uint)fifo.Dequeue() << (8 * i);
                Update(); return result;
            case 16: return crcPolynomial;
            default: return 0;
            }
        }
        public void WriteByte(long offset, byte value) { Write(offset, value, 1); }
        public void WriteWord(long offset, ushort value) { Write(offset, value, 2); }
        public void WriteDoubleWord(long offset, uint value) { Write(offset, value, 2); }
        private void Write(long offset, uint value, int bytes)
        {
            switch(offset)
            {
            case 0: control1 = value; break;
            case 4: control2 = value; break;
            case 12:
                if((control1 & 64u) == 0) return;
                for(int i = 0; i < bytes; i++)
                {
                    var peripheral = RegisteredPeripheral;
                    byte response = peripheral == null ? (byte)0 : peripheral.Transmit((byte)(value >> (8 * i)));
                    if(fifo.Count == 4) overrun = true;
                    else fifo.Enqueue(response);
                    if((control2 & 1u) != 0) DMAReceive.Blink();
                }
                break;
            case 16: crcPolynomial = value; break;
            }
            Update();
        }
        private int Threshold { get { return (control2 & 0x1000u) != 0 ? 1 : 2; } }
        private void Update()
        {
            bool enabled = (control1 & 64u) != 0;
            IRQ.Set(enabled && (((control2 & 0x80u) != 0)
                || ((control2 & 0x40u) != 0 && fifo.Count >= Threshold)
                || ((control2 & 0x20u) != 0 && overrun)));
            DMASend.Set(enabled && (control2 & 2u) != 0);
        }
        public override void Reset()
        {
            control1 = control2 = 0; crcPolynomial = 7; overrun = false;
            fifo.Clear(); IRQ.Unset(); DMAReceive.Unset(); DMASend.Unset();
        }
        private readonly Queue<byte> fifo = new Queue<byte>(4);
        private uint control1, control2, crcPolynomial;
        private bool overrun;
    }
}
