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
    //
    // Lock order (outermost first): SystemBus peripheral access lock ->
    // machine clock-source lock -> stateLock. A CPU register access holds
    // this UART's bus access lock and then takes the clock lock for its
    // LimitTimer calls. Timer callbacks, and SedsStm32Dma servicing this
    // UART from a timer callback, already hold the clock lock, so they call
    // this model directly and never take the bus access lock (doing so
    // deadlocked the bay: see SedsStm32Dma). stateLock is a leaf: it
    // makes register read-modify-write and FIFO/flag updates atomic between
    // the CPU and the DMA, and is never held across LimitTimer, IRQ (the
    // NVIC enables SysTick, a clock call), CharReceived or file I/O.
    public sealed class SedsStm32Uart : IDoubleWordPeripheral, IBytePeripheral, IKnownSize, IUART
    {
        public SedsStm32Uart(IMachine machine, uint frequency = 170000000)
        {
            this.frequency = frequency;
            this.machine = machine;
            IRQ = new GPIO();
            transmitTimer = new LimitTimer(machine.ClockSource, frequency, this,
                "uartTx", limit: 1, direction: Direction.Ascending, enabled: false, eventEnabled: true);
            transmitTimer.LimitReached += () => {
                byte value;
                bool dequeued;
                lock(stateLock)
                {
                    dequeued = transmitFifo.TryDequeue(out value);
                    if(dequeued) { transmittedBytes++; stateVersion++; }
                }
                if(dequeued)
                {
                    // A cut link still clocks the byte out of the MCU; it is
                    // lost on the wire, so the firmware cannot tell.
                    if(LinkCut()) droppedTransmit++;
                    else CharReceived?.Invoke(value);
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
                lock(stateLock) { idle = true; stateVersion++; }
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
            lock(stateLock)
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
                    break;
                case 0x2c: return prescaler;
                default: return 0;
                }
            }
            // RDR: exactly one consumer (CPU or DMA) receives each byte.
            byte data;
            lock(stateLock)
            {
                if(!receiveFifo.TryDequeue(out data)) return 0;
                stateVersion++;
            }
            UpdateInterrupt();
            return data;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            var startTransmit = false;
            ulong transmitTicks = 1;
            lock(stateLock)
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
                    // TXE check and enqueue are one step, so the CPU and a
                    // DMA channel cannot both take the last FIFO slot.
                    if((control1 & 9) == 9 && TxAvailable && baudRate != 0)
                    {
                        transmitFifo.Enqueue((byte)value);
                        startTransmit = true;
                        transmitTicks = Math.Max(1ul, (ulong)baudRate * 10);
                    }
                    break;
                case 0x2c: prescaler = value; break;
                }
                stateVersion++;
            }
            // LimitTimer calls take the clock lock; stateLock is released.
            if(startTransmit && !transmitTimer.Enabled)
            {
                // Model the configured 8N1 wire time in virtual time.
                // Instant TXE previously collapsed a burst into one
                // Pico mailbox poll and caused artificial overwrites.
                transmitTimer.Limit = transmitTicks;
                transmitTimer.Value = 0;
                transmitTimer.Enabled = true;
            }
            UpdateInterrupt();
        }

        public void WriteChar(byte value)
        {
            // Host PTYs deliver bursts in wall time. Put bytes onto the
            // configured virtual wire before exposing RXNE or DMA requests.
            lock(stateLock) { if((control1 & 5) != 5 || baudRate == 0) return; }
            if(LinkCut()) { droppedReceive++; return; }
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
            bool accepted = false;
            if(receiveWire.TryDequeue(out var value))
            {
                lock(stateLock)
                {
                    if((control1 & 5) == 5)
                    {
                        accepted = true;
                        var capacity = (control1 & (1u << 29)) != 0 ? 8 : 1;
                        if(receiveFifo.Count < capacity) receiveFifo.Enqueue(value);
                        else if((control3 & (1u << 12)) == 0) overrun = true;
                        stateVersion++;
                    }
                }
            }
            if(accepted)
            {
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
            receiveTimer.Reset(); receiveTimer.Enabled = false;
            idleTimer.Reset(); idleTimer.Enabled = false;
            transmitTimer.Reset();
            transmitTimer.Enabled = false;
            lock(stateLock)
            {
                receiveFifo.Clear();
                receiveWire.Clear();
                transmitFifo.Clear();
                idle = overrun = false;
                transmittedBytes = 0;
                control1 = 0;
                control2 = 0;
                control3 = 0;
                baudRate = 0;
                prescaler = 0;
                stateVersion++;
            }
            IRQ.Set(false);
        }

        private void UpdateInterrupt()
        {
            // The level is computed under stateLock but set outside it (the
            // NVIC takes the clock lock). If another thread changed the state
            // meanwhile, set again so the last write reflects the latest state.
            while(true)
            {
                bool level;
                ulong version;
                lock(stateLock)
                {
                    // RXNEIE/RXFNEIE is bit 5 in CR1 on STM32G4.
                    level = (receiveFifo.Count > 0 && (control1 & (1u << 5)) != 0)
                        || (TxAvailable && (control1 & (1u << 7)) != 0)
                        || (transmitFifo.Count == 0 && (control1 & (1u << 6)) != 0)
                        || (idle && (control1 & 16) != 0)
                        || (overrun && (control3 & 1) != 0);
                    version = stateVersion;
                }
                IRQ.Set(level);
                lock(stateLock) { if(version == stateVersion) return; }
            }
        }

        // ---- Optional external link gate ----------------------------------
        // SEDS_UART_GATE="<machine>:<peripheral>:<file>[;...]" selects UARTs. While
        // the file contains "1" the wire is cut in both directions (bytes the
        // MCU transmits and host bytes towards it are discarded); "0" or a
        // missing file restores it. The file is polled at most every 50 ms of
        // host time. A "<file>.status" sidecar records cut transitions and
        // dropped byte counts.
        private bool LinkCut()
        {
            if(GateSpec.Length == 0) return false;
            if(!gateResolved)
            {
                string machineName, localName;
                if(!Antmicro.Renode.Core.EmulationManager.Instance.CurrentEmulation.TryGetMachineName(machine, out machineName)
                    || !machine.TryGetLocalName(this, out localName)) return false;
                gateResolved = true;
                // Several gates may be listed, separated by ';'.
                foreach(var entry in GateSpec.Split(';'))
                {
                    var parts = entry.Split(new[] { ':' }, 3);
                    if(parts.Length == 3 && machineName == parts[0] && localName == parts[1]) { gatePath = parts[2]; break; }
                }
            }
            if(gatePath == null) return false;
            var nowMs = gateWatch.ElapsedMilliseconds;
            if(nowMs >= gateNextPollMs)
            {
                gateNextPollMs = nowMs + 50;
                var cut = false;
                try { cut = System.IO.File.Exists(gatePath) && System.IO.File.ReadAllText(gatePath).Trim() == "1"; }
                catch(System.IO.IOException) { cut = gateCut; }
                catch(UnauthorizedAccessException) { cut = gateCut; }
                if(cut != gateCut)
                {
                    gateCut = cut;
                    gateTransitions++;
                    WriteGateStatus();
                }
                else if(nowMs >= gateNextStatusMs) WriteGateStatus();
            }
            return gateCut;
        }

        private void WriteGateStatus()
        {
            gateNextStatusMs = gateWatch.ElapsedMilliseconds + 1000;
            try
            {
                var next = gatePath + ".status.next";
                System.IO.File.WriteAllText(next, string.Format("cut={0} transitions={1} dropped_tx={2} dropped_rx={3} virtual_ms={4}\n",
                    gateCut ? 1 : 0, gateTransitions, droppedTransmit, droppedReceive,
                    (long)machine.LocalTimeSource.ElapsedVirtualTime.TotalMilliseconds));
                System.IO.File.Copy(next, gatePath + ".status", true);
                System.IO.File.Delete(next);
            }
            catch(System.IO.IOException) { }
            catch(UnauthorizedAccessException) { }
        }

        private static readonly string GateSpec = Environment.GetEnvironmentVariable("SEDS_UART_GATE") ?? "";
        private readonly IMachine machine;
        private readonly System.Diagnostics.Stopwatch gateWatch = System.Diagnostics.Stopwatch.StartNew();
        private bool gateResolved, gateCut;
        private string gatePath;
        private long gateNextPollMs, gateNextStatusMs;
        private ulong gateTransitions, droppedTransmit, droppedReceive;

        public event Action<byte> CharReceived;
        public GPIO IRQ { get; private set; }
        public long Size { get { return 0x400; } }
        public uint BaudRate { get { return baudRate == 0 ? 0 : frequency / baudRate; } }
        public Bits StopBits { get { return Bits.One; } }
        public Parity ParityBit { get { return Parity.None; } }
        public uint TransmittedBytes { get { return transmittedBytes; } }
        private bool TxAvailable { get { return transmitFifo.Count < ((control1 & (1u << 29)) != 0 ? 8 : 1); } }

        private readonly uint frequency;
        private readonly LimitTimer transmitTimer, receiveTimer, idleTimer;
        private readonly ConcurrentQueue<byte> receiveWire = new ConcurrentQueue<byte>();
        private bool idle, overrun;
        private readonly object stateLock = new object();
        private ulong stateVersion;
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
