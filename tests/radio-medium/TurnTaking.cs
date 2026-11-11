using System;
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.UART;

// Air scheduling study on the bench-calibrated medium (docs/rfd-radio-model-audit.md,
// "Turn-taking"). Synthetic SEDSnet-like traffic sized from the faithful runs (pyroH2
// relay window and direct phase): the RF modem carries the AV-bay downlink, 28
// frames/s of which 8 are reliable and need an end-to-end ACK from the GS (the AV
// bay resends after 550 ms, at most 8 times); the GS sends its ACKs, a heartbeat at
// 2/s, a time sync announce at 1/s, recovery queries and an operator command every
// 5 s that the AV bay confirms reliably; the RRLS sends a small presence frame.
//
// Two schedules are compared. Uncoordinated: every node writes each frame as it is
// produced (today's behaviour). Turn-taking: the RF coalesces its frames into one
// write every 200 ms (5 air packets/s); the GS and the RRLS stay quiet and write
// only right after hearing an RF packet, everything pending coalesced into one write
// (the GS at most once per RF packet, the RRLS at most once per second).
static class TurnTaking
{
    public enum Schedule { Uncoordinated, TurnTaking }
    // The GS writes once the RF packet it heard has ended: no RF frame for this long.
    public static ulong QuietUs = 20000;
    public static ulong RfPeriodUs = 200000;
    public sealed class Outcome
    {
        public double AckCompletePct, AckMeanAttempts, AnnounceLatencyP95Ms, AnnounceLatencyMaxMs, AnnounceDeliveredPct;
        public double ConfirmP50Ms, ConfirmP95Ms, ConfirmMaxMs; public int ConfirmMissing, Commands;
        public double[] BytesPerS = new double[3], PacketsPerS = new double[3]; public double TotalBytesPerS; public string Counters;
    }

    const byte Data = 1, Reliable = 2, Ack = 3, Heartbeat = 4, Announce = 5, Command = 6, Confirm = 7, Query = 8, Presence = 9;
    sealed class Frame { public byte Kind; public uint Id; public byte[] Bytes; public ulong Created; public int Attempts; public ulong NextTry; }

    static byte[] Encode(byte kind, byte src, uint id, ulong stamp, int pad)
    {
        var body = new List<byte> { kind, src };
        body.AddRange(BitConverter.GetBytes(id)); body.AddRange(BitConverter.GetBytes(stamp));
        body.AddRange(BitConverter.GetBytes((ushort)pad));
        for(var i = 0; i < pad; i++) body.Add((byte)(i * 7));
        var crc = Crc(body);
        var f = new List<byte> { 0xC3, 0x3C }; f.AddRange(body); f.AddRange(BitConverter.GetBytes(crc));
        return f.ToArray();
    }
    static uint Crc(List<byte> d) { var c = 0xFFFFFFFFu; foreach(var b in d) { c ^= b; for(var k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & (uint)-(int)(c & 1)); } return ~c; }
    struct Got { public byte Kind, Src; public uint Id; public ulong Stamp; }
    sealed class Parser
    {
        readonly List<byte> buf = new List<byte>();
        public List<Got> Feed(byte v)
        {
            buf.Add(v); var outp = new List<Got>();
            while(true)
            {
                var at = -1;
                for(var i = 0; i + 1 < buf.Count; i++) if(buf[i] == 0xC3 && buf[i + 1] == 0x3C) { at = i; break; }
                if(at < 0) { if(buf.Count > 1) buf.RemoveRange(0, buf.Count - 1); return outp; }
                buf.RemoveRange(0, at);
                if(buf.Count < 18) return outp;
                var arr = buf.ToArray(); var pad = BitConverter.ToUInt16(arr, 16);
                if(pad > 2000 || arr[2] == 0 || arr[2] > 9) { buf.RemoveAt(0); continue; }
                var total = 18 + pad + 4; if(buf.Count < total) return outp;
                if(Crc(buf.GetRange(2, total - 6)) != BitConverter.ToUInt32(arr, total - 4)) { buf.RemoveAt(0); continue; }
                outp.Add(new Got { Kind = arr[2], Src = arr[3], Id = BitConverter.ToUInt32(arr, 4), Stamp = BitConverter.ToUInt64(arr, 8) });
                buf.RemoveRange(0, total);
            }
        }
    }

    public static Outcome Run(Schedule schedule, Func<SedsRadioMedium> makeMedium, Action<ulong> advanceRaw, Func<ulong> now, double seconds = 60)
    {
        var m = new Machine(); var ports = new[] { new SedsRadioHostPort(m), new SedsRadioHostPort(m), new SedsRadioHostPort(m) };
        var medium = makeMedium();
        for(var i = 0; i < 3; i++) medium.ConfigureModem(i, "5:GLOBAL_RETRIES=0,6:SER_BRK_DETMS=10");
        for(var i = 0; i < 3; i++) medium.AttachHost(i, ports[i]);
        const int RF = 0, GS = 1, RR = 2;
        var rng = new Random(11);
        var parsers = new[] { new Parser(), new Parser(), new Parser() };
        var inbox = new[] { new List<Got>(), new List<Got>(), new List<Got>() };
        var lastRfPacketAt = new ulong[3]; // when each node last finished receiving bytes from RF
        var rxBytes = new[] { new List<byte>(), new List<byte>(), new List<byte>() }; var rxSince = new ulong[3];
        for(var i = 0; i < 3; i++) { var k = i; ports[i].CharReceived += v => { if(rxBytes[k].Count == 0) rxSince[k] = now(); rxBytes[k].Add(v); }; }
        var pending = new[] { new List<Frame>(), new List<Frame>(), new List<Frame>() };
        var reliableOut = new Dictionary<uint, Frame>(); // RF reliable frames awaiting ACK
        var acked = new HashSet<uint>(); uint nextId = 1;
        var announceSent = new Dictionary<uint, ulong>(); var announceLat = new List<double>();
        var commandSent = new Dictionary<uint, ulong>(); var confirmLat = new List<double>(); var gsReliable = new Dictionary<uint, Frame>();
        var confirmedCmd = new HashSet<uint>();
        var txBytes = new double[3]; var txWrites = new double[3];
        ulong t0 = now(), end = t0 + (ulong)(seconds * 1e6), lastTurnRf = 0, lastTurnRr = 0;
        ulong nextRfData = t0, nextRfRel = t0, nextHb = t0, nextAnn = t0 + 300000, nextCmd = t0 + 2000000, nextQuery = t0 + 700000, nextPres = t0 + 500000;
        var gsServedRfPacket = 0UL; var rrServed = 0UL;
        Action<int, List<Frame>> write = (node, frames) => {
            if(frames.Count == 0) return;
            var bytes = frames.SelectMany(f => f.Bytes).ToArray();
            foreach(var b in bytes) ports[node].WriteChar(b);
            txBytes[node] += bytes.Length; txWrites[node] += 1;
        };
        Action<int, Frame> send = (node, f) => {
            if(schedule == Schedule.Uncoordinated) write(node, new List<Frame> { f });
            else pending[node].Add(f);
        };
        while(now() < end)
        {
            advanceRaw(now() + 1000);
            var t = now();
            // Host receive: USB serial chunks (62 B or 16 ms), as in the bench harness.
            for(var i = 0; i < 3; i++)
                if(rxBytes[i].Count >= BenchCalibration.HostChunk || (rxBytes[i].Count > 0 && t >= rxSince[i] + BenchCalibration.HostReadUs))
                {
                    var bs = rxBytes[i].ToArray(); rxBytes[i].Clear();
                    foreach(var b in bs) foreach(var g in parsers[i].Feed(b)) { inbox[i].Add(g); if(g.Src == RF) lastRfPacketAt[i] = t; }
                }
            // RF: telemetry 20/s unreliable + 8/s reliable; answers commands; handles ACKs.
            if(t >= nextRfData) { nextRfData += 50000; send(RF, new Frame { Kind = Data, Bytes = Encode(Data, RF, nextId++, t, 22 + rng.Next(20)), Created = t }); }
            if(t >= nextRfRel)
            {
                nextRfRel += 125000;
                var f = new Frame { Kind = Reliable, Id = nextId++, Created = t, Attempts = 1, NextTry = t + 550000 };
                f.Bytes = Encode(Reliable, RF, f.Id, t, 30); reliableOut[f.Id] = f; send(RF, f);
            }
            foreach(var f in reliableOut.Values.ToList())
                if(!acked.Contains(f.Id) && t >= f.NextTry && f.Attempts < 8) { f.Attempts++; f.NextTry = t + 550000; send(RF, f); }
            foreach(var g in inbox[RF])
            {
                if(g.Kind == Ack) acked.Add(g.Id);
                else if(g.Kind == Announce && g.Src == GS && announceSent.ContainsKey(g.Id) && !announceSent.ContainsKey(g.Id | 0x80000000u))
                { announceLat.Add((t - announceSent[g.Id]) / 1000.0); announceSent[g.Id | 0x80000000u] = 0; }
                else if(g.Kind == Command)
                {
                    send(RF, new Frame { Kind = Ack, Bytes = Encode(Ack, RF, g.Id, t, 0) });
                    if(!confirmedCmd.Contains(g.Id))
                    {
                        confirmedCmd.Add(g.Id);
                        var c = new Frame { Kind = Confirm, Id = g.Id, Created = t, Attempts = 1, NextTry = t + 550000 }; c.Bytes = Encode(Confirm, RF, g.Id, t, 16);
                        reliableOut[g.Id | 0x40000000u] = c; send(RF, c);
                    }
                }
            }
            inbox[RF].Clear();
            // GS: ACK every RF reliable/confirm copy; heartbeat, announce, query, command.
            foreach(var g in inbox[GS])
            {
                if(g.Kind == Reliable || g.Kind == Confirm) send(GS, new Frame { Kind = Ack, Bytes = Encode(Ack, GS, g.Kind == Confirm ? (g.Id | 0x40000000u) : g.Id, t, 0) });
                if(g.Kind == Confirm && commandSent.ContainsKey(g.Id)) { confirmLat.Add((t - commandSent[g.Id]) / 1000.0); commandSent.Remove(g.Id); }
                if(g.Kind == Ack && gsReliable.ContainsKey(g.Id)) gsReliable.Remove(g.Id);
            }
            inbox[GS].Clear();
            if(t >= nextHb) { nextHb += 500000; send(GS, new Frame { Kind = Heartbeat, Bytes = Encode(Heartbeat, GS, nextId++, t, 0) }); }
            if(t >= nextAnn) { nextAnn += 1000000; var id = nextId++; announceSent[id] = t; send(GS, new Frame { Kind = Announce, Bytes = Encode(Announce, GS, id, t, 12) }); }
            if(t >= nextQuery) { nextQuery += 1400000; send(GS, new Frame { Kind = Query, Bytes = Encode(Query, GS, nextId++, t, 32) }); }
            if(t >= nextCmd)
            {
                nextCmd += 5000000; var id = nextId++; commandSent[id] = t;
                var c = new Frame { Kind = Command, Id = id, Created = t, Attempts = 1, NextTry = t + 550000 }; c.Bytes = Encode(Command, GS, id, t, 8);
                gsReliable[id] = c; send(GS, c);
            }
            foreach(var f in gsReliable.Values.ToList())
                if(t >= f.NextTry && f.Attempts < 8) { f.Attempts++; f.NextTry = t + 550000; send(GS, f); }
            inbox[RR].Clear();
            if(t >= nextPres) { nextPres += 1000000; send(RR, new Frame { Kind = Presence, Bytes = Encode(Presence, RR, nextId++, t, 9) }); }
            if(schedule == Schedule.TurnTaking)
            {
                // RF owns the air: one coalesced write every 200 ms.
                if(t >= lastTurnRf + RfPeriodUs && pending[RF].Count > 0) { lastTurnRf = t; write(RF, pending[RF]); pending[RF].Clear(); }
                // GS answers in the gap after each RF packet it hears, everything coalesced.
                if(lastRfPacketAt[GS] > gsServedRfPacket && t >= lastRfPacketAt[GS] + QuietUs && pending[GS].Count > 0)
                { gsServedRfPacket = lastRfPacketAt[GS]; write(GS, pending[GS]); pending[GS].Clear(); }
                // RRLS: at most once a second, after an RF packet, and after the GS's turn.
                if(lastRfPacketAt[RR] > rrServed && t >= lastRfPacketAt[RR] + 60000 && t >= lastTurnRr + 1000000 && pending[RR].Count > 0)
                { rrServed = lastRfPacketAt[RR]; lastTurnRr = t; write(RR, pending[RR]); pending[RR].Clear(); }
            }
        }
        var o = new Outcome();
        o.Counters = string.Join(" ", Enumerable.Range(0, 3).Select(i => $"[{i}: refused={medium.ReadCounter(i,4)} collidedRx={medium.ReadCounter(i,12)} lostB={medium.ReadCounter(i,10)} deliveredB={medium.ReadCounter(i,8)}]"));
        var rel = reliableOut.Where(p => (p.Key & 0x40000000u) == 0 && p.Value.Created + 3000000 < end).ToList();
        o.AckCompletePct = 100.0 * rel.Count(p => acked.Contains(p.Key)) / Math.Max(1, rel.Count);
        o.AckMeanAttempts = rel.Count == 0 ? 0 : rel.Average(p => p.Value.Attempts);
        announceLat.Sort(); confirmLat.Sort();
        double P(List<double> l, double q) => l.Count == 0 ? double.NaN : l[Math.Min(l.Count - 1, (int)(q * l.Count))];
        o.AnnounceLatencyP95Ms = P(announceLat, .95); o.AnnounceLatencyMaxMs = P(announceLat, 1);
        o.AnnounceDeliveredPct = 100.0 * announceLat.Count / Math.Max(1, announceSent.Keys.Count(k => (k & 0x80000000u) == 0));
        o.Commands = confirmLat.Count + commandSent.Count; o.ConfirmMissing = commandSent.Count;
        o.ConfirmP50Ms = P(confirmLat, .5); o.ConfirmP95Ms = P(confirmLat, .95); o.ConfirmMaxMs = P(confirmLat, 1);
        var dur = (end - t0) / 1e6;
        for(var i = 0; i < 3; i++) { o.BytesPerS[i] = txBytes[i] / dur; o.PacketsPerS[i] = medium.ReadCounter(i, 9) / dur; }
        o.TotalBytesPerS = o.BytesPerS.Sum();
        return o;
    }
}
