using System;
using System.Collections.Generic;
using System.Text;
using System.IO;
using System.Globalization;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.UART;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Time;
using Antmicro.Renode.Utilities;
using Antmicro.Renode.Peripherals.Timers;

namespace Antmicro.Renode.Peripherals.UART
{
    public static class SedsRadioMediumExtensions
    {
        public static void CreateSedsRadioMedium(this Emulation emulation, string name,
            uint airRate, uint uartBaud, int txFifo, int rxFifo, int maxBurst,
            ulong idleUs, uint overheadBits, ulong turnUs)
        {
            emulation.ExternalsManager.AddExternal(new SedsRadioMedium(airRate,
                uartBaud, txFifo, rxFifo, maxBurst, idleUs, overheadBits, turnUs, name), name);
        }
    }

    // A host serial terminal and a medium must not share one UART event: doing
    // that echoes host TX back to itself and can retransmit received air bytes.
    // Only the terminal subscribes CharReceived. The medium uses HostInput.
    public sealed class SedsRadioHostPort : IUART, IDoubleWordPeripheral, IKnownSize
    {
        public SedsRadioHostPort(IMachine machine, uint baudRate = 115200)
        {
            Machine = machine;
            BaudRate = baudRate;
        }
        // An inert emulator-only bus anchor registers this proxy in the
        // machine tree so terminal backends can resolve its time domain. No
        // firmware uses the anchor and it does not emulate a physical device.
        public uint ReadDoubleWord(long offset) { return 0; }
        public void WriteDoubleWord(long offset, uint value) { }
        public long Size { get { return 0x100; } }
        public void WriteChar(byte value) { HostInput?.Invoke(value); }
        public void EmitReceived(byte value) { CharReceived?.Invoke(value); }
        public void Reset() { }
        public uint BaudRate { get; private set; }
        public Bits StopBits { get { return Bits.One; } }
        public Parity ParityBit { get { return Parity.None; } }
        public event Action<byte> CharReceived;
        public event Action<byte> HostInput;
        public IMachine Machine { get; private set; }
    }

    // Capacity/recovery model, not an implementation of the RFD proprietary
    // MAC. FIFO sizes, byte burst grouping, idle cutoff, overhead and turn time
    // are explicit scenario parameters. No SDT/frame parsing occurs here.
    public sealed class SedsRadioMedium : IExternal, IConnectable<IUART>, IPeripheral
    {
        public SedsRadioMedium(uint airRate, uint uartBaud, int txFifo, int rxFifo,
            int maxBurst, ulong idleUs, uint overheadBits, ulong turnUs, string traceName = "radio")
        {
            if(airRate == 0 || airRate > 10000000 || uartBaud == 0 || uartBaud > 5000000 || maxBurst < 1 || maxBurst > 65536
                || txFifo < maxBurst || rxFifo < maxBurst
                || txFifo > 1048576 || rxFifo > 1048576 || idleUs < 1 || idleUs > 1000000
                || turnUs > 1000000 || overheadBits > 65536)
                throw new RecoverableException("Invalid bounded radio profile");
            this.airRate = airRate;
            this.uartBaud = uartBaud;
            this.txFifo = txFifo;
            this.rxFifo = rxFifo;
            this.maxBurst = maxBurst;
            this.idleUs = idleUs;
            this.overheadBits = overheadBits;
            this.turnUs = turnUs;
            traceDirectory = Environment.GetEnvironmentVariable("FIRMWARE_SIM_RADIO_TRACE_DIR");
            if(!string.IsNullOrEmpty(traceDirectory))
            {
                if(string.IsNullOrEmpty(traceName) || traceName.Length > 160)
                    throw new RecoverableException("Invalid radio trace name");
                foreach(var c in traceName)
                    if(!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) && c != '_' && c != '-')
                        throw new RecoverableException("Invalid radio trace name");
                this.traceName = traceName;
                var until = Environment.GetEnvironmentVariable("FIRMWARE_SIM_RADIO_TRACE_UNTIL_MS");
                ulong untilMs;
                if(string.IsNullOrEmpty(until)) untilMs = 8000;
                else if(!ulong.TryParse(until, NumberStyles.None, CultureInfo.InvariantCulture, out untilMs)
                    || untilMs < 1 || untilMs > 3600000)
                    throw new RecoverableException("Radio trace window must be1..3600000 milliseconds");
                traceUntilUs = untilMs * 1000;
                Directory.CreateDirectory(traceDirectory);
            }
            for(var i = 0; i < MaxNodes; i++)
                for(var j = 0; j < MaxNodes; j++) links[i,j] = i != j;
        }

        // Resetting one MCU does not power-cycle this external radio medium.
        public void Reset() { }

        public void AttachUartByName(int index, string machineName, string peripheralPath)
        {
            AttachUart(index, ResolveUart(machineName, peripheralPath));
        }
        public void AttachHostByName(int index, string machineName, string peripheralPath)
        {
            var port = ResolveUart(machineName, peripheralPath) as SedsRadioHostPort;
            if(port == null) throw new RecoverableException("Named radio host is not a SedsRadioHostPort");
            AttachHost(index, port);
        }
        private static IUART ResolveUart(string machineName, string peripheralPath)
        {
            IMachine machine;
            if(!EmulationManager.Instance.CurrentEmulation.TryGetMachineByName(machineName, out machine))
                throw new RecoverableException("Unknown radio station machine");
            IUART uart;
            if(!machine.TryGetByName<IUART>(peripheralPath, out uart))
                throw new RecoverableException("Unknown radio station UART");
            return uart;
        }

        public void AttachUart(int index, IUART uart)
        {
            if(uart is SedsRadioHostPort)
                throw new RecoverableException("Use AttachHost for radio host ports");
            Attach(index, uart, uart.GetMachine(), uart.WriteChar, false);
        }
        public void AttachHost(int index, SedsRadioHostPort port)
        {
            Attach(index, port, port.Machine, port.EmitReceived, true);
        }
        // The renderer uses explicit indexes. The connector interface remains
        // available for a simple topology, with attachment order defining IDs.
        public void AttachTo(IUART uart)
        {
            var index = 0;
            lock(sync) { while(index < MaxNodes && nodes[index] != null) index++; }
            AttachUart(index, uart);
        }
        public void DetachFrom(IUART uart)
        {
            lock(sync)
            {
                for(var i = 0; i < MaxNodes; i++)
                {
                    var node = nodes[i];
                    if(node == null || node.Uart != uart) continue;
                    if(node.Host) ((SedsRadioHostPort)uart).HostInput -= node.Input;
                    else uart.CharReceived -= node.Input;
                    // Physical disconnect drops queued UART/air data belonging
                    // to that station. Other stations remain live.
                    if(active != null && active.Sender == i) active = null;
                    nodes[i] = null;
                    return;
                }
                throw new RecoverableException("Radio UART is not attached");
            }
        }
        public void SetLink(int senderIndex, int receiverIndex, bool available)
        {
            ValidateIndex(senderIndex); ValidateIndex(receiverIndex);
            if(senderIndex == receiverIndex && available)
                throw new RecoverableException("Radio loopback is not supported");
            lock(sync) { links[senderIndex,receiverIndex] = available; }
        }
        // Counters are monotonic for one attachment. Queue lengths and peak
        // lengths include an in-flight TX burst and scheduled UART RX bytes.
        // 0 TX depth,1 RX depth,2 TX peak,3 RX peak,4 refused TX bytes,
        // 5 dropped RX bytes,6 lost-link bytes,7 air TX bytes,8 delivered bytes.
        public ulong ReadCounter(int index, int counter)
        {
            lock(sync)
            {
                var n = GetNode(index);
                switch(counter)
                {
                    case 0: return (ulong)n.PendingTx;
                    case 1: return (ulong)n.Rx.Count;
                    case 2: return n.PeakTx;
                    case 3: return n.PeakRx;
                    case 4: return n.RefusedTx;
                    case 5: return n.DroppedRx;
                    case 6: return n.LostLink;
                    case 7: return n.AirTx;
                    case 8: return n.Delivered;
                    default: throw new RecoverableException("Unknown radio counter");
                }
            }
        }
        public string Snapshot()
        {
            lock(sync)
            {
                var b = new StringBuilder("{\"air_bits\":");
                b.Append(completedAirBits).Append(",\"nodes\":[");
                var first = true;
                for(var i = 0; i < MaxNodes; i++)
                {
                    if(nodes[i] == null) continue;
                    if(!first) b.Append(','); first = false;
                    b.Append("{\"index\":").Append(i).Append(",\"counters\":[");
                    for(var c = 0; c < 9; c++) { if(c != 0) b.Append(','); b.Append(ReadCounter(i,c)); }
                    b.Append("]}");
                }
                return b.Append("]}").ToString();
            }
        }
        public ulong CompletedAirBits { get { lock(sync) { return completedAirBits; } } }

        private void Attach(int index, IUART uart, IMachine machine, Action<byte> receive, bool host)
        {
            ValidateIndex(index);
            if(uart == null || machine == null) throw new RecoverableException("Radio station needs a machine");
            lock(sync)
            {
                if(nodes[index] != null) throw new RecoverableException("Radio index is already attached");
                for(var i = 0; i < MaxNodes; i++)
                    if(nodes[i] != null && nodes[i].Uart == uart)
                        throw new RecoverableException("Radio UART is already attached");
                var node = new Node { Uart = uart, Machine = machine, Receive = receive, Host = host };
                if(!string.IsNullOrEmpty(traceDirectory))
                {
                    if(traces[index] == null)
                        traces[index] = new TracePair(traceDirectory, traceName, index, traceUntilUs);
                    node.Trace = traces[index];
                }
                node.Deliver = value => {
                    lock(sync)
                    {
                        // A target-machine event can remain pending after a
                        // physical detach. Attachment identity, not UART or
                        // index equality, owns that delivery.
                        if(nodes[index] != node) return;
                        node.Delivered++;
                        if(node.Trace != null) node.Trace.Rx.Record(NowUs(), value);
                        node.Receive(value);
                    }
                };
                node.Input = value => Enqueue(index, node, value);
                nodes[index] = node;
                if(host) ((SedsRadioHostPort)uart).HostInput += node.Input;
                else uart.CharReceived += node.Input;
            }
            // Clock registration can enter Renode scheduling machinery; never
            // hold the medium state lock across those calls.
            if(timer == null)
            {
                timer = new LimitTimer(machine.ClockSource, 1000000, this, "radioAir",
                    limit: QuantumUs, direction: Direction.Ascending, enabled: true, eventEnabled: true);
                timer.LimitReached += Tick;
            }
        }
        private void Enqueue(int index, Node attachment, byte value)
        {
            var now = NowUs();
            lock(sync)
            {
                var n = nodes[index];
                if(n != attachment) return;
                if(n.Trace != null) n.Trace.Tx.Record(now, value);
                if(n.PendingTx >= txFifo) { n.RefusedTx++; return; }
                // PTY writes are instantaneous. Serialize their ingress at
                // 8N1 before air admission. Guest UARTs are already paced; this
                // adds at most one character of pipeline latency, not a second
                // serialization rate or an unbounded staging allocation.
                n.InputReady = Math.Max(now, n.InputReady) + UartByteUs;
                n.Tx.Enqueue(new TimedByte(value, n.InputReady));
                n.PendingTx++;
                n.PeakTx = Math.Max(n.PeakTx, (ulong)n.PendingTx);
            }
        }
        private void Tick()
        {
            var now = NowUs();
            var stamp = TimeDomainsManager.Instance.VirtualTimeStamp;
            List<Delivery> deliveries = null;
            lock(sync)
            {
                if(active != null && now >= active.EndUs)
                {
                    var sender = nodes[active.Sender];
                    if(sender != null) { sender.PendingTx -= active.Bytes.Length; sender.AirTx += (ulong)active.Bytes.Length; }
                    completedAirBits += active.Bits;
                    for(var i = 0; i < MaxNodes; i++)
                    {
                        var n = nodes[i];
                        if(n == null || n != active.Recipients[i] || i == active.Sender) continue;
                        if(!links[active.Sender,i]) { n.LostLink += (ulong)active.Bytes.Length; continue; }
                        foreach(var value in active.Bytes)
                        {
                            if(n.Rx.Count >= rxFifo) { n.DroppedRx++; continue; }
                            n.OutputReady = Math.Max(active.EndUs, n.OutputReady) + UartByteUs;
                            n.Rx.Enqueue(new TimedByte(value,n.OutputReady));
                            n.PeakRx = Math.Max(n.PeakRx,(ulong)n.Rx.Count);
                        }
                    }
                    availableUs = active.EndUs + turnUs;
                    active = null;
                }
                for(var i = 0; i < MaxNodes; i++)
                {
                    var n = nodes[i];
                    if(n == null) continue;
                    while(n.Rx.Count != 0 && n.Rx.Peek().ReadyUs <= now)
                    {
                        var value = n.Rx.Dequeue().Value;
                        if(deliveries == null) deliveries = new List<Delivery>();
                        deliveries.Add(new Delivery(n, value));
                    }
                }
                if(active == null && now >= availableUs)
                {
                    for(var step = 0; step < MaxNodes; step++)
                    {
                        var i = (cursor + step) % MaxNodes;
                        var n = nodes[i];
                        if(n == null || n.Tx.Count == 0) continue;
                        var ready = 0;
                        foreach(var value in n.Tx)
                        {
                            if(value.ReadyUs > now || ready == maxBurst) break;
                            ready++;
                        }
                        if(ready == 0 || (ready < maxBurst && now < n.InputReady + idleUs)) continue;
                        var bytes = new byte[ready];
                        for(var j = 0; j < ready; j++) bytes[j] = n.Tx.Dequeue().Value;
                        var bits = (ulong)ready * 8 + overheadBits;
                        var duration = (bits * 1000000 + airRate - 1) / airRate;
                        active = new Burst { Sender = i, Bytes = bytes, Bits = bits, EndUs = now + duration,
                            Recipients = (Node[])nodes.Clone() };
                        cursor = (i + 1) % MaxNodes;
                        break;
                    }
                }
            }
            foreach(var pair in traces)
                if(pair != null) pair.Flush(now);
            if(deliveries == null) return;
            foreach(var delivery in deliveries)
                delivery.Node.Machine.HandleTimeDomainEvent(delivery.Node.Deliver, delivery.Value, stamp);
        }
        private Node GetNode(int index)
        {
            ValidateIndex(index);
            if(nodes[index] == null) throw new RecoverableException("Radio index is not attached");
            return nodes[index];
        }
        private static void ValidateIndex(int index)
        {
            if(index < 0 || index >= MaxNodes) throw new RecoverableException("Radio index must be0..15");
        }
        private static ulong NowUs()
        {
            return (ulong)TimeDomainsManager.Instance.GetEffectiveVirtualTimeStamp().TimeElapsed.TotalMicroseconds;
        }
        private ulong UartByteUs { get { return (10000000UL + uartBaud - 1) / uartBaud; } }
        private struct TimedByte
        {
            public TimedByte(byte value, ulong readyUs) { Value = value; ReadyUs = readyUs; }
            public readonly byte Value;
            public readonly ulong ReadyUs;
        }
        private sealed class Node
        {
            public IUART Uart;
            public IMachine Machine;
            public Action<byte> Input, Receive, Deliver;
            public TracePair Trace;
            public bool Host;
            public readonly Queue<TimedByte> Tx = new Queue<TimedByte>();
            public readonly Queue<TimedByte> Rx = new Queue<TimedByte>();
            public int PendingTx;
            public ulong InputReady, OutputReady, PeakTx, PeakRx, RefusedTx, DroppedRx, LostLink, AirTx, Delivered;
        }
        private sealed class Burst { public int Sender; public byte[] Bytes; public ulong Bits, EndUs; public Node[] Recipients; }
        private struct Delivery
        {
            public Delivery(Node node, byte value) { Node = node; Value = value; }
            public Node Node; public byte Value;
        }
        private sealed class TracePair
        {
            public TracePair(string directory, string name, int index, ulong untilUs)
            {
                Tx = new RawTrace(directory, name, index, false, untilUs);
                Rx = new RawTrace(directory, name, index, true, untilUs);
            }
            public void Flush(ulong now) { Tx.Flush(now); Rx.Flush(now); }
            public readonly RawTrace Tx, Rx;
        }
        private sealed class RawTrace
        {
            // 32-byte header: ASCII SEDSRBT1; version u32LE; record_size u32LE;
            // station u32LE; direction u32LE(0TX/1RX); until_us u64LE.
            // Records: virtual_timestamp_us u64LE, then one raw UART byte.
            public RawTrace(string directory, string name, int index, bool receive, ulong untilUs)
            {
                this.untilUs = untilUs;
                var path = Path.Combine(directory, name + "-station-" + index.ToString(CultureInfo.InvariantCulture)
                    + (receive ? "-rx.bin" : "-tx.bin"));
                statusPath = path + ".status.json";
                stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read, 4096);
                foreach(var c in Encoding.ASCII.GetBytes("SEDSRBT1")) stream.WriteByte(c);
                WriteNumber(1,4); WriteNumber(9,4); WriteNumber((ulong)index,4);
                WriteNumber(receive ? 1UL : 0UL,4); WriteNumber(untilUs,8);
                stream.Flush(); WriteStatus();
            }
            public void Record(ulong now, byte value)
            {
                lock(sync)
                {
                    if(now > untilUs) { Finish(); return; }
                    if(finished) return;
                    if(records >= MaxRecords)
                    {
                        if(!capped) { capped = true; stream.Flush(); WriteStatus(); }
                        return;
                    }
                    WriteNumber(now,8); stream.WriteByte(value); records++; lastRecordUs = now;
                }
            }
            public void Flush(ulong now)
            {
                lock(sync)
                {
                    if(finished) return;
                    if(now > untilUs) { Finish(); return; }
                    if(now >= lastFlushUs + 100000)
                    {
                        stream.Flush(); lastFlushUs = now; WriteStatus();
                    }
                }
            }
            private void Finish()
            {
                if(finished) return;
                stream.Flush(); stream.Dispose(); finished = true; WriteStatus();
            }
            private void WriteStatus()
            {
                // This bounded sidecar is mandatory: a capped or prematurely
                // stopped capture must not be mistaken for a complete stream.
                var text = "{\"version\":1,\"max_bytes\":" + MaxBytes
                    + ",\"header_bytes\":32,\"record_bytes\":9,\"records\":" + records
                    + ",\"until_us\":" + untilUs + ",\"last_record_us\":" + lastRecordUs
                    + ",\"capped\":" + (capped ? "true" : "false")
                    + ",\"finished\":" + (finished ? "true" : "false") + "}";
                File.WriteAllText(statusPath,text);
            }
            private void WriteNumber(ulong value,int width)
            {
                for(var i=0;i<width;i++) { stream.WriteByte((byte)value); value >>= 8; }
            }
            private const ulong MaxBytes = 4 * 1024 * 1024;
            private const ulong MaxRecords = (MaxBytes - 32) / 9;
            private readonly object sync = new object();
            private readonly string statusPath;
            private readonly FileStream stream;
            private readonly ulong untilUs;
            private ulong records, lastRecordUs, lastFlushUs;
            private bool capped, finished;
        }
        private const int MaxNodes = 16;
        private const ulong QuantumUs = 100;
        private readonly object sync = new object();
        private readonly Node[] nodes = new Node[MaxNodes];
        private readonly TracePair[] traces = new TracePair[MaxNodes];
        private readonly string traceDirectory, traceName;
        private readonly ulong traceUntilUs;
        private readonly bool[,] links = new bool[MaxNodes,MaxNodes];
        private readonly uint airRate, uartBaud, overheadBits;
        private readonly int txFifo, rxFifo, maxBurst;
        private readonly ulong idleUs, turnUs;
        private LimitTimer timer;
        private Burst active;
        private int cursor;
        private ulong availableUs, completedAirBits;
    }
}
