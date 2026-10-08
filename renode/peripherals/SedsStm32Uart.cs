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
    public sealed class SedsStm32Uart : IDoubleWordPeripheral, IBytePeripheral, IKnownSize, IUART
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
            receiveTimer = new LimitTimer(machine.ClockSource, frequency, this,
                "uartRx", limit: 1, direction: Direction.Ascending, enabled: false, eventEnabled: true);
            receiveTimer.LimitReached += ReceiveWireByte;
            idleTimer = new LimitTimer(machine.ClockSource, frequency, this,
                "uartIdle", limit: 1, direction: Direction.Ascending, enabled: false, eventEnabled: true);
            idleTimer.LimitReached += () => {
                idleTimer.Enabled = false;
                idle = true;
                UpdateInterrupt();
            };
            Reset();
        }

        public byte ReadByte(long offset)
        {
            // RDR consumes exactly one byte. Do not perform a read/modify/write
            // of data registers when a byte-sized DMA beat accesses them.
            return (byte)(ReadDoubleWord(offset & ~3L) >> (8 * (int)(offset & 3)));
        }

        public void WriteByte(long offset, byte value)
        {
            if(offset == 0x28) WriteDoubleWord(offset, value);
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
                    | (receiveFifo.Count > 0 ? 1u << 5 : 0u)
                    | (idle ? 1u << 4 : 0u) | (overrun ? 1u << 3 : 0u);
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
            case 0x20: // ICR: write-one-to-clear IDLECF and ORECF.
                if((value & 16) != 0) idle = false;
                if((value & 8) != 0) overrun = false;
                break;
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
            // Host PTYs deliver bursts in wall time. Put bytes onto the
            // configured virtual wire before exposing RXNE or DMA requests.
            if((control1 & 5) != 5 || baudRate == 0) return;
            receiveWire.Enqueue(value);
            if(!receiveTimer.Enabled)
            {
                receiveTimer.Limit = CharacterTicks;
                receiveTimer.Value = 0;
                receiveTimer.Enabled = true;
            }
        }

        private ulong CharacterTicks { get { return Math.Max(1ul, (ulong)baudRate * 10); } }
        private void ReceiveWireByte()
        {
            if(receiveWire.TryDequeue(out var value) && (control1 & 5) == 5)
            {
                var capacity = (control1 & (1u << 29)) != 0 ? 16 : 1;
                if(receiveFifo.Count < capacity) receiveFifo.Enqueue(value);
                else if((control3 & (1u << 12)) == 0) overrun = true;
                idleTimer.Enabled = false;
                // One frame of inactivity after the final received frame.
                // The extra tick prevents an IDLE edge between contiguous bytes.
                idleTimer.Limit = CharacterTicks + 1;
                idleTimer.Value = 0;
                idleTimer.Enabled = true;
                UpdateInterrupt();
            }
            if(receiveWire.IsEmpty)
            {
                receiveTimer.Enabled = false;
                if(!receiveWire.IsEmpty) receiveTimer.Enabled = true;
            }
        }

        public void Reset()
        {
            receiveFifo.Clear();
            receiveWire.Clear();
            transmitFifo.Clear();
            receiveTimer.Reset(); receiveTimer.Enabled = false;
            idleTimer.Reset(); idleTimer.Enabled = false;
            idle = overrun = false;
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
                || (transmitFifo.Count == 0 && (control1 & (1u << 6)) != 0)
                || (idle && (control1 & 16) != 0)
                || (overrun && (control3 & 1) != 0));
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
        private readonly LimitTimer transmitTimer, receiveTimer, idleTimer;
        private readonly ConcurrentQueue<byte> receiveWire = new ConcurrentQueue<byte>();
        private bool idle, overrun;
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
