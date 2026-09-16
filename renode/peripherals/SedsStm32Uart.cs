using System;
using System.Collections.Concurrent;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.UART;
using Antmicro.Renode.Time;
using Antmicro.Renode.Peripherals.Timers;

namespace Antmicro.Renode.Peripherals.UART
{
    // STM32G4 USART register model. Renode's F7 model exposes a one-byte RX
    // holding register when G4 FIFO mode is enabled, which drops host bursts
    // before the firmware's ReceiveToIdle DMA/parser can observe them.
    public sealed class SedsStm32Uart : IDoubleWordPeripheral, IKnownSize, IUART
    {
        public SedsStm32Uart(IMachine machine, uint frequency = 170000000)
        {
            this.frequency = frequency;
            IRQ = new GPIO();
            transmitTimer = new LimitTimer(machine.ClockSource, frequency, this,
                "uartTx", limit: 1, direction: Direction.Ascending, enabled: false, eventEnabled: true);
            transmitTimer.LimitReached += () => {
                if(transmitFifo.TryDequeue(out var value))
                {
                    transmittedBytes++;
                    CharReceived?.Invoke(value);
                }
                if(transmitFifo.IsEmpty)
                {
                    // A CPU can enqueue after the empty check but before the
                    // clock is disabled. Recheck after disabling so that its
                    // wake-up cannot be lost. No lock may span clock calls.
                    transmitTimer.Enabled = false;
                    if(!transmitFifo.IsEmpty) transmitTimer.Enabled = true;
                }
                UpdateInterrupt();
            };
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
                return (TxAvailable ? 1u << 7 : 0u)
                    | (transmitFifo.Count == 0 ? 1u << 6 : 0u)
                    | (1u << 21) | (1u << 22)
                    | (receiveFifo.Count > 0 ? 1u << 5 : 0u);
            case 0x24:
                if(!receiveFifo.TryDequeue(out var value)) return 0;
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
                // Do not hold a model lock across LimitTimer access: Renode
                // invokes timer callbacks while holding its clock-source lock.
                if((control1 & 9) == 9 && TxAvailable && baudRate != 0)
                {
                    transmitFifo.Enqueue((byte)value);
                    if(!transmitTimer.Enabled)
                    {
                        // Model the configured 8N1 wire time in virtual time.
                        // Instant TXE previously collapsed a burst into one
                        // Pico mailbox poll and caused artificial overwrites.
                        transmitTimer.Limit = Math.Max(1ul, (ulong)baudRate * 10);
                        transmitTimer.Value = 0;
                        transmitTimer.Enabled = true;
                    }
                }
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
            transmitFifo.Clear();
            transmitTimer.Reset();
            transmitTimer.Enabled = false;
            transmittedBytes = 0;
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
            IRQ.Set((receiveFifo.Count > 0 && (control1 & (1u << 5)) != 0)
                || (TxAvailable && (control1 & (1u << 7)) != 0)
                || (transmitFifo.Count == 0 && (control1 & (1u << 6)) != 0));
        }

        public event Action<byte> CharReceived;
        public GPIO IRQ { get; private set; }
        public long Size { get { return 0x400; } }
        public uint BaudRate { get { return baudRate == 0 ? 0 : frequency / baudRate; } }
        public Bits StopBits { get { return Bits.One; } }
        public Parity ParityBit { get { return Parity.None; } }
        public uint TransmittedBytes { get { return transmittedBytes; } }
        private bool TxAvailable { get { return transmitFifo.Count < ((control1 & (1u << 29)) != 0 ? 16 : 1); } }

        private readonly uint frequency;
        private readonly LimitTimer transmitTimer;
        // CPU register accesses, UART input and timer callbacks can execute
        // on different Renode threads. Queue<T> loses head/count updates in
        // that case, replacing valid firmware bytes with stale FIFO contents.
        private readonly ConcurrentQueue<byte> transmitFifo = new ConcurrentQueue<byte>();
        private uint transmittedBytes;
        private readonly ConcurrentQueue<byte> receiveFifo = new ConcurrentQueue<byte>();
        private uint control1;
        private uint control2;
        private uint control3;
        private uint baudRate;
        private uint prescaler;
    }
}
