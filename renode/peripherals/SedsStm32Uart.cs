using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.UART;

namespace Antmicro.Renode.Peripherals.UART
{
    // STM32G4 USART register model. Renode's F7 model exposes a one-byte RX
    // holding register when G4 FIFO mode is enabled, which drops host bursts
    // before the firmware's ReceiveToIdle DMA/parser can observe them.
    public sealed class SedsStm32Uart : IDoubleWordPeripheral, IKnownSize, IUART
    {
        public SedsStm32Uart(uint frequency = 170000000)
        {
            this.frequency = frequency;
            IRQ = new GPIO();
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case 0x00: return control1;
            case 0x04: return control2;
            case 0x08: return control3;
            case 0x0c: return baudRate;
            case 0x1c:
                // TXE/TXFNF, TC, TEACK and REACK are immediately available.
                return (1u << 7) | (1u << 6) | (1u << 21) | (1u << 22)
                    | (receiveFifo.Count > 0 ? 1u << 5 : 0u);
            case 0x24:
                if(receiveFifo.Count == 0) return 0;
                var value = receiveFifo.Dequeue();
                UpdateInterrupt();
                return value;
            case 0x2c: return prescaler;
            default: return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case 0x00: control1 = value; break;
            case 0x04: control2 = value; break;
            case 0x08: control3 = value; break;
            case 0x0c: baudRate = value; break;
            case 0x18:
                if((value & (1u << 3)) != 0) receiveFifo.Clear(); // RXFRQ
                break;
            case 0x28:
                CharReceived?.Invoke((byte)value);
                break;
            case 0x2c: prescaler = value; break;
            }
            UpdateInterrupt();
        }

        public void WriteChar(byte value)
        {
            receiveFifo.Enqueue(value);
            UpdateInterrupt();
        }

        public void Reset()
        {
            receiveFifo.Clear();
            control1 = 0;
            control2 = 0;
            control3 = 0;
            baudRate = 0;
            prescaler = 0;
            IRQ.Set(false);
        }

        private void UpdateInterrupt()
        {
            // RXNEIE/RXFNEIE is bit 5 in CR1 on STM32G4.
            IRQ.Set(receiveFifo.Count > 0 && (control1 & (1u << 5)) != 0);
        }

        public event Action<byte> CharReceived;
        public GPIO IRQ { get; private set; }
        public long Size { get { return 0x400; } }
        public uint BaudRate { get { return baudRate == 0 ? 0 : frequency / baudRate; } }
        public Bits StopBits { get { return Bits.One; } }
        public Parity ParityBit { get { return Parity.None; } }

        private readonly uint frequency;
        private readonly Queue<byte> receiveFifo = new Queue<byte>();
        private uint control1;
        private uint control2;
        private uint control3;
        private uint baudRate;
        private uint prescaler;
    }
}
