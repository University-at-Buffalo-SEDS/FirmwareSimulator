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
    // A station with a modelled RFD modem (ConfigureModem) takes its packet
    // size (MAX_DATA), packet idle cutoff (SER_BRK_DETMS), broadcast copies
    // (GLOBAL_RETRIES) and DUTY_CYCLE from its own active registers, because
    // each RFD900x ASYNC modem applies them to its own transmissions:
    // docs/rfd-radio-model-audit.md has the sources and the assumptions.
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
            // Opt-in RFD900x ASYNC air model: every packet to the broadcast
            // DESTID goes on air 1 + GLOBAL_RETRIES times (receivers keep the
            // first copy), and DUTY_CYCLE (percent) holds the sender off the air
            // for duration*(100/duty-1) after each packet. Packetization stays
            // the scenario's SER_BRK_DETMS (idleUs) and MAX_DATA (maxBurst).
            globalRetries = EnvBounded("FIRMWARE_SIM_RFD_GLOBAL_RETRIES", 0, 0, 100);
            dutyCycle = EnvBounded("FIRMWARE_SIM_RFD_DUTY_CYCLE", 100, 1, 100);
            // GLOBAL_RETRIES semantics are not documented beyond "maximum number
            // of retries for broadcast messages" (manual V1.1 p. 7), so both
            // readings are selectable (docs/rfd-radio-model-audit.md, open
            // assumption A1):
            //   on_loss (default): a resend happens only while some other
            //            attached station still lacks the packet, at most S5
            //            resends (an acknowledgement from every station is
            //            assumed; release notes 2.45 "with acknowledgement");
            //   blind:   every broadcast goes on air 1 + S5 times.
            // The default follows the 2026-10-28 bench strace: the RF modem (S5
            // at its factory 10; no RF code wrote S5 before 2026-11-08) sent
            // 880 B/s of payload to the GS on 64 kbit/s air, and 11 blind
            // copies would need 880*8*11 = 77 kbit/s.
            // FIRMWARE_SIM_RFD_AIR_LOSS_PPM is an independent per-copy,
            // per-receiver loss (seeded, deterministic); 0 = clean air.
            var mode = Environment.GetEnvironmentVariable("FIRMWARE_SIM_RFD_BROADCAST_RETRY");
            var benchRetry = string.IsNullOrEmpty(mode) && Environment.GetEnvironmentVariable("FIRMWARE_SIM_RFD_CALIBRATION") != "none";
            if(benchRetry || mode == "ack") { retryOnLoss = true; ackRetries = true; }
            else if(string.IsNullOrEmpty(mode) || mode == "on_loss") retryOnLoss = true;
            else if(mode == "blind") retryOnLoss = false;
            else throw new RecoverableException("FIRMWARE_SIM_RFD_BROADCAST_RETRY must be ack, blind or on_loss");
            airLossPpm = EnvBounded("FIRMWARE_SIM_RFD_AIR_LOSS_PPM", 0, 0, 1000000);
            random = new Random((int)EnvBounded("FIRMWARE_SIM_RFD_AIR_LOSS_SEED", 1, 0, int.MaxValue));
            // Bench timing of the RFD900x2 ASYNC 4.03 pair (2026-11-10, GS
            // scripts/rfd_link_test.py; docs/rfd-radio-model-audit.md):
            // a modelled modem spends TX_SETUP (+0..JITTER) after a packet
            // closes before it is on air, deaf to the air throughout (it
            // cannot hear, and listen-before-talk sampled before it began);
            // the receiver outputs a packet RX_DELAY after it ends on air;
            // S5 copies are COPY_GAP apart, also deaf. FIRMWARE_SIM_RFD_CALIBRATION
            // =none restores the uncalibrated mechanics (zero delays).
            var calibration = Environment.GetEnvironmentVariable("FIRMWARE_SIM_RFD_CALIBRATION");
            if(string.IsNullOrEmpty(calibration)) calibration = "bench-20261110";
            if(calibration != "bench-20261110" && calibration != "none")
                throw new RecoverableException("FIRMWARE_SIM_RFD_CALIBRATION must be bench-20261110 or none");
            var bench = calibration == "bench-20261110";
            txSetupUs = EnvBounded("FIRMWARE_SIM_RFD_TX_SETUP_US", bench ? 4000U : 0U, 0, 500000);
            txJitterUs = EnvBounded("FIRMWARE_SIM_RFD_TX_JITTER_US", bench ? 3000U : 0U, 0, 500000);
            rxDelayUs = EnvBounded("FIRMWARE_SIM_RFD_RX_DELAY_US", 0U, 0, 500000);
            copyGapUs = EnvBounded("FIRMWARE_SIM_RFD_COPY_GAP_US", 0U, 0, 1000000);
            streamRx = bench;
            inputBufferBytes = EnvBounded("FIRMWARE_SIM_RFD_INPUT_BUFFER_BYTES", bench ? 2048U : 0U, 0, 1048576);
            setupDeaf = EnvBounded("FIRMWARE_SIM_RFD_SETUP_DEAF", 0, 0, 1) == 1;
            // Provisional (docs/rfd-radio-model-audit.md): S5 >= 1 as
            // acknowledged retries; not yet separated from blind copies.
            ackDelayUs = EnvBounded("FIRMWARE_SIM_RFD_ACK_DELAY_US", bench ? 60000U : 0U, 0, 1000000);
            ackTimeoutUs = EnvBounded("FIRMWARE_SIM_RFD_ACK_TIMEOUT_US", bench ? 80000U : 50000U, 1000, 5000000);
            if(bench && Environment.GetEnvironmentVariable("FIRMWARE_SIM_RFD_AIR_LOSS_PPM") == null) airLossPpm = 5000;
            var mac = Environment.GetEnvironmentVariable("FIRMWARE_SIM_RFD_MAC");
            if(string.IsNullOrEmpty(mac)) asyncMac = !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("FIRMWARE_SIM_RFD_MODEM"));
            else if(mac == "async" || mac == "arbiter") asyncMac = mac == "async";
            else throw new RecoverableException("FIRMWARE_SIM_RFD_MAC must be async or arbiter");
            carrierSense = Environment.GetEnvironmentVariable("FIRMWARE_SIM_RFD_CARRIER_SENSE");
            if(carrierSense == "" || carrierSense == "register") carrierSense = null;
            else if(carrierSense != null && carrierSense != "always" && carrierSense != "never")
                throw new RecoverableException("FIRMWARE_SIM_RFD_CARRIER_SENSE must be register, always or never");
            // Opt-in RFD900x ASYNC command-mode model per station, for example
            // FIRMWARE_SIM_RFD_MODEM="0=6:SER_BRK_DETMS=20,19:RTSCTS=1". Stations
            // without an entry stay transparent, exactly as before.
            var modems = Environment.GetEnvironmentVariable("FIRMWARE_SIM_RFD_MODEM");
            if(!string.IsNullOrEmpty(modems))
                foreach(var entry in modems.Split(';'))
                {
                    if(entry.Trim().Length == 0) continue;
                    var eq = entry.IndexOf('=');
                    int station;
                    if(eq < 1 || !int.TryParse(entry.Substring(0, eq), NumberStyles.None, CultureInfo.InvariantCulture, out station))
                        throw new RecoverableException("FIRMWARE_SIM_RFD_MODEM entries are <station>=<overrides>");
                    ConfigureModem(station, entry.Substring(eq + 1));
                }
        }

        // Models the RFD900x ASYNC command mode for one station: "+++" after
        // one second of host silence, OK after a further second of silence,
        // echoed characters, ATI/ATI5/ATSn?/ATSn=/AT&W/ATZ/ATO/AT+F?. The
        // register layout is the field ASYNC 4.03 RFD900x2 one; `overrides`
        // ("n:NAME=value,..." or "n=value,...") sets EEPROM values or names.
        // While in command mode or rebooting the station passes no air data.
        public void ConfigureModem(int index, string overrides)
        {
            ValidateIndex(index);
            var modem = new Modem();
            if(!string.IsNullOrEmpty(overrides))
                foreach(var item in overrides.Split(','))
                {
                    var text = item.Trim();
                    if(text.Length == 0) continue;
                    var eq = text.LastIndexOf('=');
                    var colon = text.IndexOf(':');
                    int number, value;
                    var numberText = colon > 0 ? text.Substring(0, colon) : (eq > 0 ? text.Substring(0, eq) : "");
                    if(eq < 1 || !int.TryParse(numberText, NumberStyles.None, CultureInfo.InvariantCulture, out number)
                        || number < 0 || number > 255
                        || !int.TryParse(text.Substring(eq + 1), NumberStyles.None, CultureInfo.InvariantCulture, out value))
                        throw new RecoverableException("Invalid RFD modem override: " + text);
                    var name = colon > 0 ? text.Substring(colon + 1, eq - colon - 1) : null;
                    modem.Set(number, name, value);
                }
            lock(sync) { modemConfig[index] = modem; if(nodes[index] != null) nodes[index].Modem = modem; }
        }

        private static uint EnvBounded(string name, uint fallback, uint min, uint max)
        {
            var text = Environment.GetEnvironmentVariable(name);
            if(string.IsNullOrEmpty(text)) return fallback;
            uint value;
            if(!uint.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out value) || value < min || value > max)
                throw new RecoverableException(name + " must be " + min + ".." + max);
            return value;
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
                    node.Cur = null;
                    nodes[i] = null;
                    return;
                }
                throw new RecoverableException("Radio UART is not attached");
            }
        }
        // Partial loss on one path (sender -> receiver), per air copy, on top
        // of the base FIRMWARE_SIM_RFD_AIR_LOSS_PPM: a weak link rather than a
        // cut. 0..1000000 ppm; schedulable like SetLink.
        public void SetLinkLoss(int senderIndex, int receiverIndex, uint lossPpm)
        {
            ValidateIndex(senderIndex); ValidateIndex(receiverIndex);
            if(lossPpm > 1000000) throw new RecoverableException("Link loss must be 0..1000000 ppm");
            lock(sync) { linkLossPpm[senderIndex,receiverIndex] = lossPpm; }
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
        // 5 dropped RX bytes,6 lost-link bytes,7 air TX bytes,8 delivered bytes,
        // 9 air packets (each counted once, however many broadcast copies),
        // 10 bytes lost on air (every copy lost; FIRMWARE_SIM_RFD_AIR_LOSS_PPM),
        // 11 broadcast copies sent (11/9 is the mean copies per packet),
        // 12 copy receptions corrupted by overlap (collision or half duplex),
        // 13 ticks a ready packet waited for listen-before-talk (async MAC).
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
                    case 9: return n.AirPackets;
                    case 10: return n.AirLost;
                    case 11: return n.AirCopies;
                    case 12: return n.Collided;
                    case 13: return n.LbtDeferrals;
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
                    for(var c = 0; c < 14; c++) { if(c != 0) b.Append(','); b.Append(ReadCounter(i,c)); }
                    b.Append("],\"global_retries\":").Append(GlobalRetries(nodes[i]))
                        .Append(",\"packet_idle_us\":").Append(IdleUs(nodes[i]))
                        .Append(",\"max_data\":").Append(MaxData(nodes[i]));
                    if(nodes[i].Modem != null) b.Append(",\"modem\":").Append(nodes[i].Modem.Json());
                    b.Append('}');
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
                var node = new Node { Uart = uart, Machine = machine, Receive = receive, Host = host, Modem = modemConfig[index] };
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
                    }
                    // Never call into a UART while holding the medium lock: a
                    // UART takes its machine's clock-source lock, and that
                    // machine's TX callback holds it while it calls Enqueue.
                    node.Receive(value);
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
                try { if(n.Modem != null && ModemInput(n, value, now)) return; }
                catch(Exception e) { DebugLog("ModemInput exception " + e); return; }
                EnqueueAir(n, value, now);
            }
        }
        private void EnqueueAir(Node n, byte value, ulong now)
        {
            // A modelled modem drops serial input once INPUT_BUFFER bytes wait
            // (no RTS/CTS): this bounds the bench backlog under overload.
            if(n.PendingTx >= txFifo || (IsModelled(n) && inputBufferBytes != 0 && n.PendingTx >= (int)inputBufferBytes)) { n.RefusedTx++; return; }
            // PTY writes are instantaneous. Serialize their ingress at
            // 8N1 before air admission. Guest UARTs are already paced; this
            // adds at most one character of pipeline latency, not a second
            // serialization rate or an unbounded staging allocation.
            var inputStart = Math.Max(now, n.InputReady);
            n.InputReady = inputStart + UartByteUs;
            // UART packet boundaries belong to each modem's ingress,
            // independently of which modem currently owns airtime. Retain
            // a completed idle gap even if a later write arrives before
            // this station gets its next turn. Only lengths/times are
            // staged here; payload bytes retain their single FIFO owner.
            if(n.PacketTail == null || n.PacketTail.Count >= MaxData(n)
                || inputStart >= n.PacketTail.LastReadyUs + IdleUs(n))
            {
                n.PacketTail = new TxPacket();
                n.Packets.Enqueue(n.PacketTail);
            }
            n.PacketTail.Count++;
            n.PacketTail.LastReadyUs = n.InputReady;
            n.Tx.Enqueue(new TimedByte(value, n.InputReady));
            n.PendingTx++;
            n.PeakTx = Math.Max(n.PeakTx, (ulong)n.PendingTx);
        }
        private void Tick()
        {
            var now = NowUs();
            var stamp = TimeDomainsManager.Instance.VirtualTimeStamp;
            List<Delivery> deliveries = null;
            lock(sync)
            {
                if(asyncMac) AsyncAir(now);
                else if(active != null && now >= active.EndUs)
                {
                    var sender = nodes[active.Sender];
                    if(sender != null) { sender.PendingTx -= active.Bytes.Length; sender.AirTx += (ulong)active.Bytes.Length; sender.AirPackets++; sender.HoldUs = active.EndUs + active.HoldUs; }
                    completedAirBits += active.Bits;
                    for(var i = 0; i < MaxNodes; i++)
                    {
                        var n = nodes[i];
                        if(n == null || n != active.Recipients[i] || i == active.Sender) continue;
                        if(!links[active.Sender,i]) { n.LostLink += (ulong)active.Bytes.Length; continue; }
                        if(!active.Received[i]) { n.AirLost += (ulong)active.Bytes.Length; continue; }
                        DeliverAir(n, active.Bytes, active.EndUs);
                    }
                    availableUs = active.EndUs + turnUs;
                    active = null;
                }
                try {
                for(var i = 0; i < MaxNodes; i++)
                    if(nodes[i] != null && nodes[i].Modem != null) ModemTick(nodes[i], now);
                } catch(Exception e) { DebugLog("ModemTick exception " + e); }
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
                if(!asyncMac && active == null && now >= availableUs)
                {
                    for(var step = 0; step < MaxNodes; step++)
                    {
                        var i = (cursor + step) % MaxNodes;
                        var n = nodes[i];
                        if(n == null || n.Packets.Count == 0 || now < n.HoldUs) continue;
                        var packet = n.Packets.Peek();
                        if(now < packet.LastReadyUs
                            || (packet.Count < MaxData(n) && now < packet.LastReadyUs + IdleUs(n))) continue;
                        n.Packets.Dequeue();
                        if(n.PacketTail == packet) n.PacketTail = null;
                        var ready = packet.Count;
                        var bytes = new byte[ready];
                        for(var j = 0; j < ready; j++) bytes[j] = n.Tx.Dequeue().Value;
                        var recipients = (Node[])nodes.Clone();
                        var received = new bool[MaxNodes];
                        var copies = SendCopies(i, recipients, received, GlobalRetries(n));
                        n.AirCopies += copies;
                        var bits = ((ulong)ready * 8 + overheadBits) * copies;
                        var duration = (bits * 1000000 + airRate - 1) / airRate;
                        var duty = DutyCycle(n);
                        var hold = duty >= 100 ? 0UL : duration * (100 - duty) / duty;
                        active = new Burst { Sender = i, Bytes = bytes, Bits = bits, EndUs = now + duration, HoldUs = hold,
                            Recipients = recipients, Received = received };
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
        // The receiving modem outputs a whole packet over its UART after the
        // packet (copy) has ended on air.
        private void DeliverAir(Node n, byte[] bytes, ulong endUs)
        {
            // A modem in command mode or rebooting does not output air data.
            if(n.Modem != null && n.Modem.State != ModemState.Data) { n.Modem.DroppedAir += (ulong)bytes.Length; return; }
            foreach(var value in bytes)
            {
                if(n.Rx.Count >= rxFifo) { n.DroppedRx++; continue; }
                n.OutputReady = Math.Max(endUs, n.OutputReady) + UartByteUs;
                n.Rx.Enqueue(new TimedByte(value,n.OutputReady));
                n.PeakRx = Math.Max(n.PeakRx,(ulong)n.Rx.Count);
            }
        }

        // --- RFD900x ASYNC uncoordinated access ---------------------------
        // FIRMWARE_SIM_RFD_MAC=async (the default once FIRMWARE_SIM_RFD_MODEM
        // is set): no central arbiter. "There is no transmission coordination
        // or synchronisation of the network" (X series datasheet 2.1 s5.3).
        // Every station transmits on its own; a copy is lost at a receiver
        // that hears another station during it (no capture effect), or that
        // is transmitting itself or within turnUs after (single half-duplex
        // radio). Listen-before-talk follows each modem's LBT_RSSI register
        // (0 = off, the SiK meaning; async merged SiK 3.09, release notes
        // 2.60) unless FIRMWARE_SIM_RFD_CARRIER_SENSE=always|never. With LBT
        // the channel must be clear for 5 ms plus a random 0-5 ms, redrawn
        // after each busy period (SiK tdm.c LBT_MIN_TIME_USEC 5000 and
        // lbt_rand; ETSI EN 300 220 9.2.2.2). docs/rfd-radio-model-audit.md.
        private void AsyncAir(ulong now)
        {
            for(var s = 0; s < MaxNodes; s++)
            {
                var n = nodes[s];
                if(n == null || n.Cur == null) continue;
                if(n.Cur.Streams != null && !n.Cur.CopyDone && now >= n.Cur.CopyStart) Stream(s, n.Cur, Math.Min(now, n.Cur.CopyEnd));
                if(now < n.Cur.CopyEnd) continue;
                var c = n.Cur;
                if(!c.CopyDone)
                {
                    c.CopyDone = true;
                    c.Copies++; n.AirCopies++;
                    completedAirBits += c.CopyBits;
                    if(c.IsAck)
                    {
                        // A link acknowledgement: received intact, it settles the
                        // acknowledged packet for this station. Either way the
                        // acknowledging modem now outputs the data it held.
                        var t = c.AckTo; var target = nodes[t];
                        if(target != null && target.Cur == c.AckOf && links[s,t] && !Overlapped(s, t, c.CopyStart, c.CopyEnd) && !Lost(s, t))
                            c.AckOf.Received[s] = true;
                        if(c.Hold != null) DeliverAir(n, c.Hold, c.CopyEnd + rxDelayUs);
                        n.HoldUs = c.CopyEnd + turnUs;
                        n.Cur = null;
                        continue;
                    }
                    var acked = AckMode(n);
                    if(c.Streams != null)
                    {
                        // Streamed copies already output what arrived intact.
                        foreach(var st in c.Streams)
                            if(!st.Failed && nodes[st.R] == c.Recipients[st.R]) c.Received[st.R] = c.Heard[st.R] = true;
                        c.Streams = null;
                    }
                    else for(var r = 0; r < MaxNodes; r++)
                    {
                        var rx = nodes[r];
                        if(r == s || rx == null || rx != c.Recipients[r] || c.Received[r] || !links[s,r]) continue;
                        if(Overlapped(s, r, c.CopyStart, c.CopyEnd)) { rx.Collided++; continue; }
                        if(Lost(s, r)) continue;
                        if(acked)
                        {
                            // The receiver acknowledges every intact copy and outputs
                            // the data once, after its acknowledgement has gone out.
                            rx.Acks.Add(new AckJob { To = s, For = c, ReadyUs = c.CopyEnd + ackDelayUs, Hold = c.Heard[r] ? null : c.Bytes });
                            c.Heard[r] = true;
                            continue;
                        }
                        c.Received[r] = c.Heard[r] = true;
                        if(streamRx && IsModelled(n)) DeliverStreamed(rx, c.Bytes, c.CopyEnd);
                        else DeliverAir(rx, c.Bytes, c.CopyEnd);
                    }
                    if(acked) { c.WaitUntil = c.CopyEnd + ackTimeoutUs; continue; }
                }
                var missing = false;
                for(var r = 0; r < MaxNodes; r++)
                    if(r != s && nodes[r] != null && nodes[r] == c.Recipients[r] && !c.Received[r]) missing = true;
                if(AckMode(n))
                {
                    if(missing && now < c.WaitUntil) continue;
                    if(missing && c.Copies < c.MaxCopies) { StartCopy(s, n, c, now); continue; }
                }
                else if(c.Copies < c.MaxCopies && (!retryOnLoss || missing))
                {
                    var deafFrom = c.CopyEnd;
                    c.CopyDone = false;
                    c.CopyStart = c.CopyEnd + (IsModelled(n) ? copyGapUs : 0); c.CopyEnd = c.CopyStart + c.CopyUs;
                    airLog.Add(new AirTx { Sender = s, DeafStart = deafFrom, Start = c.CopyStart, End = c.CopyEnd });
                    continue;
                }
                for(var r = 0; r < MaxNodes; r++)
                {
                    var rx = nodes[r];
                    if(r == s || rx == null || rx != c.Recipients[r] || c.Heard[r]) continue;
                    if(!links[s,r]) rx.LostLink += (ulong)c.Bytes.Length; else rx.AirLost += (ulong)c.Bytes.Length;
                }
                n.PendingTx -= c.Bytes.Length; n.AirTx += (ulong)c.Bytes.Length; n.AirPackets++;
                var duty = DutyCycle(n); var total = c.Copies * c.CopyUs;
                n.HoldUs = c.CopyEnd + turnUs + (duty >= 100 ? 0UL : total * (100 - duty) / duty);
                n.Cur = null;
            }
            airLog.RemoveAll(t => t.End + 2000000 < now);
            for(var i = 0; i < MaxNodes; i++)
            {
                var n = nodes[i];
                if(n == null) continue;
                var busy = false;
                // Carrier sense hears only what is on air, not a modem's setup.
                for(var j = 0; j < MaxNodes; j++)
                    if(j != i && nodes[j] != null && nodes[j].Cur != null && now >= nodes[j].Cur.CopyStart && now < nodes[j].Cur.CopyEnd && links[j,i]) busy = true;
                if(busy) { n.ClearSinceUs = now; n.ListenDrawn = false; }
                else if(!n.ListenDrawn) { n.ListenUs = 5000UL + (ulong)random.Next(5001); n.ListenDrawn = true; }
                if(n.Cur != null || now < n.HoldUs) continue;
                var ack = n.Acks.Count == 0 ? null : n.Acks.Find(a => a.ReadyUs <= now);
                if(ack == null && n.Packets.Count == 0) continue;
                TxPacket packet = null;
                if(ack == null)
                {
                    packet = n.Packets.Peek();
                    if(now < packet.LastReadyUs
                        || (packet.Count < MaxData(n) && now < packet.LastReadyUs + IdleUs(n))) continue;
                }
                if(ListenBeforeTalk(n) && (busy || now < n.ClearSinceUs + n.ListenUs)) { n.LbtDeferrals++; continue; }
                if(ack != null)
                {
                    n.Acks.Remove(ack);
                    var ackBits = (ulong)AckBytes * 8 + overheadBits;
                    var ackRate = AirRate(n);
                    var ackPacket = new AirPacket { Bytes = new byte[0], Recipients = (Node[])nodes.Clone(), Received = new bool[MaxNodes],
                        Heard = new bool[MaxNodes], MaxCopies = 1, CopyBits = ackBits, CopyUs = (ackBits * 1000000 + ackRate - 1) / ackRate,
                        IsAck = true, AckTo = ack.To, AckOf = ack.For, Hold = ack.Hold };
                    StartCopy(i, n, ackPacket, now);
                    n.Cur = ackPacket;
                    continue;
                }
                n.Packets.Dequeue();
                if(n.PacketTail == packet) n.PacketTail = null;
                var bytes = new byte[packet.Count];
                for(var j = 0; j < bytes.Length; j++) bytes[j] = n.Tx.Dequeue().Value;
                var bits = (ulong)bytes.Length * 8 + overheadBits;
                var rate = AirRate(n);
                var cur = new AirPacket { Bytes = bytes, Recipients = (Node[])nodes.Clone(), Received = new bool[MaxNodes], Heard = new bool[MaxNodes],
                    MaxCopies = 1UL + GlobalRetries(n), CopyBits = bits, CopyUs = (bits * 1000000 + rate - 1) / rate };
                StartCopy(i, n, cur, now);
                n.Cur = cur;
            }
        }
        // A modelled modem spends TX_SETUP (+0..JITTER) before each copy is on
        // air. It keeps receiving during setup (SETUP_DEAF=1 makes it deaf);
        // on air it hears nothing, and transmitting pre-empts a reception.
        private void StartCopy(int index, Node n, AirPacket c, ulong now)
        {
            var setup = IsModelled(n) ? txSetupUs + (txJitterUs == 0 ? 0UL : (ulong)random.Next((int)txJitterUs + 1)) : 0UL;
            c.CopyDone = false;
            c.CopyStart = now + setup; c.CopyEnd = c.CopyStart + c.CopyUs;
            // A modelled receiver outputs a data packet while it arrives (bench
            // RTT slope and burst echo timing, docs/rfd-radio-model-audit.md).
            c.Streams = null;
            if(streamRx && IsModelled(n) && !c.IsAck && !AckMode(n))
            {
                c.Streams = new List<RxStream>();
                for(var r = 0; r < MaxNodes; r++)
                {
                    var rx = nodes[r];
                    if(r == index || rx == null || rx != c.Recipients[r] || c.Received[r] || !links[index,r]) continue;
                    if(Lost(index, r)) continue;
                    c.Streams.Add(new RxStream { R = r });
                }
            }
            airLog.Add(new AirTx { Sender = index, DeafStart = setupDeaf ? now : c.CopyStart, Start = c.CopyStart, End = c.CopyEnd });
        }
        // S5 with acknowledgement (release notes 2.45): each receiver confirms a
        // broadcast; the sender resends after ACK_TIMEOUT, at most S5 times.
        private bool AckMode(Node n) { return ackRetries && IsModelled(n) && GlobalRetries(n) > 0; }
        // A modelled receiver streams a packet to its UART while it arrives on
        // air, so the last byte leaves RX_DELAY plus one character after the
        // packet ends (bench RTT slope, docs/rfd-radio-model-audit.md).
        private void DeliverStreamed(Node n, byte[] bytes, ulong endUs)
        {
            var last = endUs + rxDelayUs + UartByteUs;
            var span = (ulong)Math.Max(bytes.Length - 1, 0) * UartByteUs;
            n.OutputReady = Math.Max(n.OutputReady, last > span ? last - span - UartByteUs : 0);
            DeliverAir(n, bytes, n.OutputReady);
        }
        // Outputs the bytes of a streamed copy that have arrived by `until`;
        // the first overlap at a receiver truncates its stream (the rest of a
        // corrupted packet is never output).
        private void Stream(int s, AirPacket c, ulong until)
        {
            var rate = AirRate(nodes[s]);
            var headerUs = (ulong)overheadBits * 1000000UL / rate;
            foreach(var st in c.Streams)
            {
                var rx = nodes[st.R];
                if(st.Failed || rx == null || rx != c.Recipients[st.R]) continue;
                if(Overlapped(s, st.R, c.CopyStart, until)) { st.Failed = true; rx.Collided++; rx.AirLost += (ulong)(c.Bytes.Length - st.Next); continue; }
                while(st.Next < c.Bytes.Length)
                {
                    var at = c.CopyStart + headerUs + (ulong)(st.Next + 1) * 8000000UL / rate;
                    if(at > until) break;
                    DeliverAir(rx, new[] { c.Bytes[st.Next] }, at + rxDelayUs);
                    st.Next++;
                }
            }
        }
        private sealed class RxStream { public int R, Next; public bool Failed; }
        private const int AckBytes = 8;
        private sealed class AckJob { public int To; public AirPacket For; public ulong ReadyUs; public byte[] Hold; }
        // A copy from `sender` is lost at `receiver` if anything else the
        // receiver can hear overlaps it, or the receiver itself transmits
        // during it or less than turnUs before it.
        private bool Overlapped(int sender, int receiver, ulong start, ulong end)
        {
            foreach(var t in airLog)
            {
                if(t.Sender == sender) continue;
                if(t.Sender == receiver ? (t.DeafStart < end && t.End + turnUs > start)
                    : (links[t.Sender, receiver] && t.Start < end && t.End > start)) return true;
            }
            return false;
        }
        private bool IsModelled(Node n) { return n.Modem != null; }
        private bool Lost(int sender, int receiver)
        {
            var ppm = (ulong)airLossPpm + linkLossPpm[sender,receiver];
            return ppm != 0 && (ulong)random.Next(1000000) < Math.Min(ppm, 1000000UL);
        }
        // AIR_SPEED (S2, kbit/s) of a modelled modem; the profile otherwise.
        private uint AirRate(Node n)
        {
            int v;
            if(n.Modem != null && n.Modem.TryActive("AIR_SPEED", null, out v) && v >= 4 && v <= 1000) return (uint)v * 1000U;
            return airRate;
        }
        private bool ListenBeforeTalk(Node n)
        {
            if(carrierSense != null) return carrierSense == "always";
            int v;
            return n.Modem != null && n.Modem.TryActive("LBT_RSSI", null, out v) && v != 0;
        }

        // --- RFD900x ASYNC command-mode model -----------------------------
        private bool ModemInput(Node n, byte value, ulong now)
        {
            var m = n.Modem;
            var silent = now >= m.LastHostUs + 1000000;
            var handled = true;
            switch(m.State)
            {
            case ModemState.Rebooting:
                break;
            case ModemState.Command:
                Say(n, now, new[] { value });
                if(value == (byte)'\r')
                {
                    var line = m.Line.ToString().Trim();
                    m.Line.Clear();
                    if(line.Length != 0) ModemExecute(n, line, now);
                }
                else if(m.Line.Length < 128) m.Line.Append((char)value);
                break;
            default:
                if(value == (byte)'+' && (m.Pluses > 0 || silent) && m.Pluses < 3)
                {
                    m.Pluses++;
                    m.PlusUs = now;
                }
                else
                {
                    // Held '+' bytes were ordinary data after all.
                    for(var k = 0; k < m.Pluses; k++) EnqueueAir(n, (byte)'+', now);
                    m.Pluses = 0;
                    handled = false;
                }
                break;
            }
            m.LastHostUs = now;
            return handled;
        }
        private void ModemTick(Node n, ulong now)
        {
            var m = n.Modem;
            if(m.State == ModemState.Rebooting && now >= m.RebootUntilUs)
            {
                m.State = ModemState.Data;
                foreach(var r in m.Regs.Values) r.Ram = r.Active = r.Eeprom;
            }
            if(m.Pluses == 3 && now >= m.PlusUs + 1000000 && m.LastHostUs <= m.PlusUs)
            {
                m.Pluses = 0;
                m.State = ModemState.Command;
                m.CommandEntries++;
                Say(n, now, Encoding.ASCII.GetBytes("OK\r\n"));
            }
        }
        private void Say(Node n, ulong now, byte[] bytes)
        {
            foreach(var value in bytes)
            {
                if(n.Rx.Count >= rxFifo) { n.DroppedRx++; continue; }
                n.OutputReady = Math.Max(now + 2000, n.OutputReady) + UartByteUs;
                n.Rx.Enqueue(new TimedByte(value, n.OutputReady));
            }
        }
        private void ModemExecute(Node n, string cmd, ulong now)
        {
            var m = n.Modem;
            m.Commands++;
            DebugLog(now + " cmd " + cmd);
            var reply = new StringBuilder();
            int number, value;
            if(cmd == "AT") reply.Append("OK\r\n");
            else if(cmd == "ATI") reply.Append("RFD ASYNC 4.03 on RFD900X2\r\n");
            else if(cmd == "ATI5")
                foreach(var pair in m.Regs) reply.Append('S').Append(pair.Key).Append(':').Append(pair.Value.Name).Append('=').Append(pair.Value.Ram).Append("\r\n");
            else if(cmd == "AT+F?")
            {
                // ASYNC 4.03 format: ten empty "slot:src->dst" entries.
                for(var slot = 0; slot < 10; slot++) reply.Append(slot).Append(":0->0,");
                reply.Append(" | FwdBuff=13320\r\n");
            }
            else if(cmd == "AT&W")
            {
                foreach(var r in m.Regs.Values) r.Eeprom = r.Ram;
                m.Saves++;
                reply.Append("OK\r\n");
            }
            else if(cmd == "ATZ")
            {
                m.State = ModemState.Rebooting;
                m.RebootUntilUs = now + 1500000;
                m.Reboots++;
            }
            else if(cmd == "ATO") m.State = ModemState.Data;
            else if(cmd.StartsWith("ATS", StringComparison.Ordinal) && cmd.EndsWith("?", StringComparison.Ordinal)
                && int.TryParse(cmd.Substring(3, cmd.Length - 4), NumberStyles.None, CultureInfo.InvariantCulture, out number)
                && m.Regs.ContainsKey(number))
                reply.Append(m.Regs[number].Ram).Append("\r\n");
            else if(cmd.StartsWith("ATS", StringComparison.Ordinal) && cmd.IndexOf('=') > 3
                && int.TryParse(cmd.Substring(3, cmd.IndexOf('=') - 3), NumberStyles.None, CultureInfo.InvariantCulture, out number)
                && int.TryParse(cmd.Substring(cmd.IndexOf('=') + 1), NumberStyles.None, CultureInfo.InvariantCulture, out value)
                && m.Regs.ContainsKey(number))
            {
                m.Regs[number].Ram = value;
                m.Writes.Append(m.Writes.Length == 0 ? "" : ",").Append(number).Append('=').Append(value);
                reply.Append("OK\r\n");
            }
            else reply.Append("ERROR\r\n");
            if(reply.Length != 0) Say(n, now, Encoding.ASCII.GetBytes(reply.ToString()));
        }
        private static void DebugLog(string line)
        {
            var path = Environment.GetEnvironmentVariable("FIRMWARE_SIM_RFD_MODEM_LOG");
            if(!string.IsNullOrEmpty(path)) File.AppendAllText(path, line + "\n");
        }
        private enum ModemState { Data, Command, Rebooting }
        // Active is what the radio runs: ATSn= changes RAM, and only AT&W then
        // ATZ (a reboot) makes it the running value (manual V1.1 p. 5).
        private sealed class ModemReg { public string Name; public int Ram, Eeprom, Active; }
        private sealed class Modem
        {
            public Modem()
            {
                // RFD ASYNC 4.03 on RFD900x2 as read in the field (S17 DUTY_CYCLE
                // shifts LBT_RSSI to S18 and RTSCTS to S19), with the deployed
                // rfd-async-64k-compatible values.
                Set(0, "FORMAT", 27); Set(1, "SERIAL_SPEED", 115); Set(2, "AIR_SPEED", 64);
                Set(3, "MAX_DATA", 3968); Set(4, "MAX_RETRIES", 10); Set(5, "GLOBAL_RETRIES", 10);
                Set(6, "SER_BRK_DETMS", 50); Set(7, "TX_ENCAP_METHOD", 0); Set(8, "RX_ENCAP_METHOD", 0);
                Set(9, "NETID", 25); Set(10, "NODEID", 2); Set(11, "DESTID", 32768);
                Set(12, "TXPOWER", 30); Set(13, "MAVLINK1", 0); Set(14, "MIN_FREQ", 915000);
                Set(15, "MAX_FREQ", 928000); Set(16, "NUM_CHANNELS", 20); Set(17, "DUTY_CYCLE", 100);
                Set(18, "LBT_RSSI", 0); Set(19, "RTSCTS", 0); Set(20, "MAX_WINDOW", 131); Set(22, "RESV", 0);
            }
            public void Set(int number, string name, int value)
            {
                ModemReg r;
                if(!Regs.TryGetValue(number, out r)) { r = new ModemReg { Name = name ?? ("S" + number) }; Regs[number] = r; }
                if(!string.IsNullOrEmpty(name)) r.Name = name;
                r.Ram = r.Eeprom = r.Active = value;
            }
            // Registers are found by the name ATI5 reports: the 4.03 numbering
            // differs from the V1.1 manual above S5. Returns false if absent.
            public bool TryActive(string name, string alias, out int value)
            {
                foreach(var r in Regs.Values)
                    if(r.Name == name || r.Name == alias) { value = r.Active; return true; }
                value = 0; return false;
            }
            public string Json()
            {
                var b = new StringBuilder("{\"state\":\"").Append(State.ToString().ToLowerInvariant())
                    .Append("\",\"command_entries\":").Append(CommandEntries).Append(",\"commands\":").Append(Commands)
                    .Append(",\"saves\":").Append(Saves).Append(",\"reboots\":").Append(Reboots)
                    .Append(",\"dropped_air\":").Append(DroppedAir)
                    .Append(",\"writes\":\"").Append(Writes).Append("\",\"eeprom\":{");
                var first = true;
                foreach(var pair in Regs)
                {
                    if(!first) b.Append(','); first = false;
                    b.Append("\"S").Append(pair.Key).Append(':').Append(pair.Value.Name).Append("\":").Append(pair.Value.Eeprom);
                }
                return b.Append("}}").ToString();
            }
            public readonly SortedDictionary<int, ModemReg> Regs = new SortedDictionary<int, ModemReg>();
            public readonly StringBuilder Line = new StringBuilder();
            public readonly StringBuilder Writes = new StringBuilder();
            public ModemState State;
            public int Pluses;
            public ulong PlusUs, LastHostUs, RebootUntilUs, DroppedAir;
            public ulong CommandEntries, Commands, Saves, Reboots;
        }

        // --- Per-station air parameters --------------------------------------
        // Stations without a modelled modem keep the scenario profile and the
        // global FIRMWARE_SIM_RFD_* values, exactly as before.
        private int MaxData(Node n)
        {
            int v;
            // MAX_DATA (S3): 1..4068, manual V1.1 Table 3-2; a packet never
            // exceeds the modem's own TX buffer.
            if(n.Modem != null && n.Modem.TryActive("MAX_DATA", null, out v) && v >= 1)
                return Math.Min(Math.Min(v, 4068), txFifo);
            return maxBurst;
        }
        private ulong IdleUs(Node n)
        {
            int v;
            // SER_BRK_DETMS (S6): ms of serial silence that closes a packet,
            // 0..500, local to each node (manual V1.1 p. 7). 0 turns break
            // detection off; modelled as one character time (send at once).
            if(n.Modem != null && n.Modem.TryActive("SER_BRK_DETMS", "SER_BREAK_DETECT_MS", out v) && v >= 0)
                return v == 0 ? UartByteUs : (ulong)Math.Min(v, 500) * 1000UL;
            return idleUs;
        }
        // GLOBAL_RETRIES (S5), 0..100 (manual V1.1 Table 3-2). Applied by the
        // sending modem to its own broadcasts.
        private uint GlobalRetries(Node n)
        {
            int v;
            if(n.Modem != null && n.Modem.TryActive("GLOBAL_RETRIES", null, out v) && v >= 0)
                return (uint)Math.Min(v, 100);
            return globalRetries;
        }
        // Decides, at the start of a packet, how many copies go on air and
        // which receivers get at least one. Receivers keep the first copy.
        private ulong SendCopies(int sender, Node[] recipients, bool[] received, uint retries)
        {
            ulong copies = 0;
            while(copies < 1UL + retries)
            {
                copies++;
                var missing = false;
                for(var i = 0; i < MaxNodes; i++)
                {
                    if(i == sender || recipients[i] == null || received[i]) continue;
                    if(links[sender,i] && !Lost(sender, i)) received[i] = true;
                    else missing = true;
                }
                if(retryOnLoss && !missing) break;
            }
            return copies;
        }
        private uint DutyCycle(Node n)
        {
            int v;
            if(n.Modem != null && n.Modem.TryActive("DUTY_CYCLE", null, out v) && v >= 1 && v <= 100)
                return (uint)v;
            return dutyCycle;
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
            public Modem Modem;
            public readonly Queue<TimedByte> Tx = new Queue<TimedByte>();
            // At least one accepted FIFO byte owns every descriptor; the TX
            // FIFO limit also bounds this metadata, including while air busy.
            public readonly Queue<TxPacket> Packets = new Queue<TxPacket>();
            public TxPacket PacketTail;
            public readonly Queue<TimedByte> Rx = new Queue<TimedByte>();
            public int PendingTx;
            public ulong InputReady, OutputReady, PeakTx, PeakRx, RefusedTx, DroppedRx, LostLink, AirTx, Delivered, AirPackets, HoldUs, AirLost, AirCopies, Collided, LbtDeferrals;
            // Async access state (FIRMWARE_SIM_RFD_MAC=async).
            public AirPacket Cur;
            public readonly List<AckJob> Acks = new List<AckJob>();
            public ulong ClearSinceUs, ListenUs;
            public bool ListenDrawn;
        }
        private sealed class TxPacket { public int Count; public ulong LastReadyUs; }
        private sealed class AirTx { public int Sender; public ulong DeafStart, Start, End; }
        private sealed class AirPacket
        {
            public byte[] Bytes; public Node[] Recipients; public bool[] Received;
            public ulong Copies, MaxCopies, CopyBits, CopyUs, CopyStart, CopyEnd, WaitUntil;
            public bool CopyDone, IsAck; public bool[] Heard; public int AckTo; public AirPacket AckOf; public byte[] Hold;
            public List<RxStream> Streams;
        }
        private sealed class Burst { public int Sender; public byte[] Bytes; public ulong Bits, EndUs, HoldUs; public Node[] Recipients; public bool[] Received; }
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
            // FIRMWARE_SIM_RADIO_TRACE_MAX_MB (4..256, default 4) bounds each
            // stream; a long linked run needs more than the default.
            private static readonly ulong MaxBytes = TraceMaxBytes();
            private static ulong TraceMaxBytes()
            {
                var text = Environment.GetEnvironmentVariable("FIRMWARE_SIM_RADIO_TRACE_MAX_MB");
                ulong mb;
                if(string.IsNullOrEmpty(text)) return 4UL * 1024 * 1024;
                if(!ulong.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out mb) || mb < 4 || mb > 256)
                    throw new RecoverableException("FIRMWARE_SIM_RADIO_TRACE_MAX_MB must be 4..256");
                return mb * 1024 * 1024;
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
            private static readonly ulong MaxRecords = (MaxBytes - 32) / 9;
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
        private readonly Modem[] modemConfig = new Modem[MaxNodes];
        private readonly TracePair[] traces = new TracePair[MaxNodes];
        private readonly string traceDirectory, traceName;
        private readonly ulong traceUntilUs;
        private readonly bool[,] links = new bool[MaxNodes,MaxNodes];
        private readonly uint airRate, uartBaud, overheadBits;
        private readonly int txFifo, rxFifo, maxBurst;
        private readonly ulong idleUs, turnUs;
        private readonly uint globalRetries, dutyCycle;
        private uint airLossPpm;
        private readonly ulong txSetupUs, txJitterUs, rxDelayUs, copyGapUs, ackDelayUs, ackTimeoutUs;
        private readonly bool ackRetries, setupDeaf, streamRx;
        private readonly uint inputBufferBytes;
        private readonly uint[,] linkLossPpm = new uint[MaxNodes,MaxNodes];
        private readonly bool retryOnLoss, asyncMac;
        private readonly string carrierSense;
        private readonly List<AirTx> airLog = new List<AirTx>();
        private readonly Random random;
        private LimitTimer timer;
        private Burst active;
        private int cursor;
        private ulong availableUs, completedAirBits;
    }
}
