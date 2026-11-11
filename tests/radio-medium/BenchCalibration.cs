using System;
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.UART;

// Replays GroundStation26 scripts/rfd_link_test.py (GS 84a8c13) against the
// medium and compares each result with the 2026-11-10 bench measurement of
// the RFD900x2 ASYNC 4.03 pair (GS docs/backend/rfd-modem-check.md, S5=0).
// Frames are the tool's: A5 5A | kind u8 | seq u32 | sent_ns u64 | pad_len
// u16 | pad | crc32, so a frame is pad + 21 bytes. `ping` writes `burst`
// frames back to back `rate` times a second for `duration` s and waits
// `drain` s for late PONGs; `echo` answers each PING with a PONG of
// min(pad, pong_size) pad bytes, or only counts PINGs when silent.
static class BenchCalibration
{
    public sealed class Row
    {
        public string Name;
        public int S5 = 0, S6 = 10, Lbt = 0, AirKbps = 64;
        public double Rate; public int Burst = 1, Size = 120, PongSize = 120;
        public bool Silent;
        public double Duration = 20, Drain = 5;
        // Measured (bench); NaN where the bench reported no value.
        public double LossPct, RttP50 = double.NaN, RttMax = double.NaN;
        // Tolerances: absolute percentage points for loss, relative for RTT.
        public double LossTol, RttTol = 0.2;
        public string Source;
        // A known residual is reported, not asserted (docs/rfd-radio-model-audit.md).
        public string Residual;
    }
    public sealed class Result { public int Sent, Answered; public double LossPct, RttP50, RttMax; }

    public static readonly Row[] Rows =
    {
        // docs/backend/rfd-modem-check.md "S5=0" table (ping/echo, 120 B, both directions).
        new Row{Name="S5=1 2/s",  S5=1, Rate=2,  LossPct=5,   RttP50=262,  RttMax=1427, LossTol=4,  Residual="S5>=1 mechanism provisional (bench request 2)", Source="doc table"},
        new Row{Name="S5=1 10/s", S5=1, Rate=10, LossPct=49,  RttP50=1044, RttMax=2614, LossTol=12, RttTol=0.35, Residual="S5>=1 mechanism provisional (bench request 2); loss matches, queue 3x too long", Source="doc table, coordinator row 1"},
        new Row{Name="S5=0 2/s",  S5=0, Rate=2,  LossPct=2.5, RttP50=120,  RttMax=133,  LossTol=3,  Source="doc table"},
        new Row{Name="S5=0 10/s", S5=0, Rate=10, LossPct=5.5, RttP50=120,  RttMax=137,  LossTol=4,  Source="doc table, coordinator row 1"},
        // Coordinator row 2: fixed cost per air packet.
        new Row{Name="S5=0 1/s 32B",  S5=0, Rate=1, Size=32,  PongSize=32,  LossPct=0, RttP50=94,  LossTol=5, RttTol=0.1, Residual="RTT slope residual: sim +46 ms for +88 B, bench +26 ms (unexplained)", Source="coordinator row 2"},
        new Row{Name="S5=0 1/s 120B", S5=0, Rate=1, Size=120, PongSize=120, LossPct=0, RttP50=120, LossTol=5, RttTol=0.1, Source="coordinator row 2"},
        // Coordinator row 3: one way to a silent receiver, 145 B frames (pad 124).
        new Row{Name="one-way 20/s 145B", S5=0, Rate=20, Size=124, Silent=true, LossPct=0.5, LossTol=2, Source="coordinator row 3, doc"},
        new Row{Name="one-way 40/s 145B", S5=0, Rate=40, Size=124, Silent=true, LossPct=6,   LossTol=4, Source="coordinator row 3, doc"},
        // Coordinator row 4: both ends transmitting.
        new Row{Name="bidir 20/s x1",      S5=0, Rate=20, LossPct=98, LossTol=6, Source="coordinator row 4"},
        new Row{Name="bidir 5/s x8",       S5=0, Rate=5,  Burst=8, LossPct=98, LossTol=6, Residual="just short of the collapse band (87.6% vs 92%+)", Source="coordinator row 4"},
        new Row{Name="bidir 10/s x4",      S5=0, Rate=10, Burst=4, LossPct=98, LossTol=6, Source="coordinator row 4"},
        new Row{Name="bidir 5/s x4",       S5=0, Rate=5,  Burst=4, LossPct=7,  RttP50=265, LossTol=5, RttTol=0.25, Residual="loss 1.8% vs 5-9%; RTT within tolerance", Source="coordinator row 4"},
        new Row{Name="bidir 5/s x4 S6=20", S5=0, S6=20, Rate=5, Burst=4, LossPct=28, LossTol=10, Residual="right direction (S6=20 worse), 46% vs 28%", Source="coordinator row 4 (S6=20 worse: 28% vs 5%)"},
    };

    static int FrameLen(int pad) => pad + 21;

    // One rfd_link_test run on two modem stations of a fresh medium.
    // Host read batching: the bench hosts read the modem through USB serial,
    // which hands received bytes over in batches (latency timer, 16 ms by
    // default on FTDI-class adapters); the tool parses and answers a batch at
    // once, so PONGs for one batch are written back to back.
    public static ulong HostReadUs = 16000;
    public static int HostChunk = 62;
    public static ulong TurnUs = 1000; // the rfd-faithful profile turn_us
    public static int TxFifo = 4096;
    // Host write timing: the tool's paced writes land up to this late
    // (Python select timeouts and OS scheduling on the Pi and the Mac).
    public static ulong HostJitterUs = 4000;
    public static Result Run(Row row, Func<SedsRadioMedium> makeMedium, Action<ulong> advanceRaw, Func<ulong> now)
    {
        var pingIn = new List<byte>(); var echoIn = new List<byte>();
        ulong pingSince = 0, echoSince = 0;
        Action flush = null;
        // USB serial: a full 62-byte chunk goes to the host at once, a partial
        // one when the latency timer expires after its first byte.
        bool Due(List<byte> buf, ulong since) => buf.Count >= HostChunk || (buf.Count > 0 && now() >= since + HostReadUs);
        Action<ulong> advance = target => {
            while(now() < target)
            {
                advanceRaw(Math.Min(target, now() + 100));
                if(Due(pingIn, pingSince) || Due(echoIn, echoSince)) flush();
            }
        };
        var m = new Machine();
        var pinger = new SedsRadioHostPort(m); var echo = new SedsRadioHostPort(m);
        var medium = makeMedium();
        var regs = $"2:AIR_SPEED={row.AirKbps},5:GLOBAL_RETRIES={row.S5},6:SER_BRK_DETMS={row.S6},18:LBT_RSSI={row.Lbt}";
        medium.ConfigureModem(0, regs); medium.ConfigureModem(1, regs);
        medium.AttachHost(0, pinger); medium.AttachHost(1, echo);
        var pingParse = new Parser(); var echoParse = new Parser();
        var sentAt = new Dictionary<uint, ulong>(); var rtts = new List<double>();
        echo.CharReceived += v => { if(echoIn.Count == 0) echoSince = now(); echoIn.Add(v); };
        pinger.CharReceived += v => { if(pingIn.Count == 0) pingSince = now(); pingIn.Add(v); };
        flush = () => {
            var e = echoIn.ToArray(); echoIn.Clear();
            var replies = new List<byte>();
            foreach(var v in e)
                foreach(var f in echoParse.Feed(v))
                    if(f.Kind == 1 && !row.Silent) replies.AddRange(Frame(2, f.Seq, f.SentUs, Math.Min(f.Pad, row.PongSize)));
            foreach(var b in replies) echo.WriteChar(b);
            var p = pingIn.ToArray(); pingIn.Clear();
            foreach(var v in p)
                foreach(var f in pingParse.Feed(v))
                    if(f.Kind == 2 && sentAt.Remove(f.Seq)) rtts.Add((now() - f.SentUs) / 1000.0);
        };
        var pings = 0; var echoed = new HashSet<uint>();
        if(row.Silent) echo.CharReceived += v => { };
        var start = now(); var interval = (ulong)(1e6 / row.Rate); var end = start + (ulong)(row.Duration * 1e6);
        uint seq = 0; var next = start; var rng = new Random(7);
        while(next < end)
        {
            advance(next + (ulong)rng.Next((int)HostJitterUs + 1));
            for(var k = 0; k < row.Burst; k++)
            {
                foreach(var b in Frame(1, seq, now(), row.Size)) pinger.WriteChar(b);
                sentAt[seq] = now(); seq++;
            }
            next += interval;
        }
        advance(end + (ulong)(row.Drain * 1e6));
        if(Environment.GetEnvironmentVariable("RADIO_CALIBRATION_DEBUG") == row.Name.Replace(" ", "_"))
        {
            foreach(var st in new[]{0,1})
                Console.WriteLine($"    station {st}: airPackets={medium.ReadCounter(st,9)} copies={medium.ReadCounter(st,11)} airTxB={medium.ReadCounter(st,7)} refusedTx={medium.ReadCounter(st,4)} deliveredB={medium.ReadCounter(st,8)} collidedRx={medium.ReadCounter(st,12)} airLostB={medium.ReadCounter(st,10)} droppedRx={medium.ReadCounter(st,5)} lbt={medium.ReadCounter(st,13)}");
            Console.WriteLine($"    echo parsed pings={echoParse.Pings} rtts={rtts.Count}");
        }
        int answered;
        if(row.Silent) { answered = echoParse.Pings; }
        else answered = rtts.Count;
        rtts.Sort();
        double Pct(double q) => rtts.Count == 0 ? double.NaN : rtts[Math.Min(rtts.Count - 1, (int)(q * rtts.Count))];
        _ = pings; _ = echoed;
        return new Result { Sent = (int)seq, Answered = answered, LossPct = 100.0 * (seq - answered) / Math.Max(seq, 1),
            RttP50 = Pct(.5), RttMax = Pct(1.0) };
    }

    static byte[] Frame(byte kind, uint seq, ulong sentUs, int pad)
    {
        var body = new List<byte> { kind };
        body.AddRange(BitConverter.GetBytes(seq)); body.AddRange(BitConverter.GetBytes(sentUs));
        body.AddRange(BitConverter.GetBytes((ushort)pad));
        for(var i = 0; i < pad; i++) body.Add((byte)i);
        var crc = Crc32(body);
        var frame = new List<byte> { 0xA5, 0x5A }; frame.AddRange(body); frame.AddRange(BitConverter.GetBytes(crc));
        return frame.ToArray();
    }
    static uint Crc32(List<byte> data)
    {
        var crc = 0xFFFFFFFFu;
        foreach(var b in data) { crc ^= b; for(var k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & (uint)-(int)(crc & 1)); }
        return ~crc;
    }
    struct Parsed { public byte Kind; public uint Seq; public ulong SentUs; public int Pad; }
    sealed class Parser
    {
        readonly List<byte> buf = new List<byte>();
        public int Pings;
        public IEnumerable<Parsed> Feed(byte v)
        {
            buf.Add(v);
            var outp = new List<Parsed>();
            while(true)
            {
                var at = -1;
                for(var i = 0; i + 1 < buf.Count; i++) if(buf[i] == 0xA5 && buf[i + 1] == 0x5A) { at = i; break; }
                if(at < 0) { if(buf.Count > 1) buf.RemoveRange(0, buf.Count - (buf[buf.Count - 1] == 0xA5 ? 1 : 0)); return outp; }
                buf.RemoveRange(0, at);
                if(buf.Count < 17) return outp;
                var kind = buf[2]; var pad = BitConverter.ToUInt16(buf.ToArray(), 15);
                if((kind != 1 && kind != 2) || pad > 4000) { buf.RemoveAt(0); continue; }
                var total = 17 + pad + 4;
                if(buf.Count < total) return outp;
                var body = buf.GetRange(2, total - 6);
                var crc = BitConverter.ToUInt32(buf.ToArray(), total - 4);
                if(Crc32(body) != crc) { buf.RemoveAt(0); continue; }
                var arr = buf.ToArray();
                var p = new Parsed { Kind = kind, Seq = BitConverter.ToUInt32(arr, 3), SentUs = BitConverter.ToUInt64(arr, 7), Pad = pad };
                buf.RemoveRange(0, total);
                if(kind == 1) Pings++;
                outp.Add(p);
            }
        }
    }

    public static bool Check(Row row, Result r, out string detail)
    {
        var ok = Math.Abs(r.LossPct - row.LossPct) <= row.LossTol;
        if(!double.IsNaN(row.RttP50)) ok &= !double.IsNaN(r.RttP50) && Math.Abs(r.RttP50 - row.RttP50) <= row.RttTol * row.RttP50;
        // The maximum is one extreme sample of a 20 s run: within a factor of 2.
        if(!double.IsNaN(row.RttMax)) ok &= !double.IsNaN(r.RttMax) && r.RttMax >= row.RttMax / 2 && r.RttMax <= row.RttMax * 2;
        detail = $"loss {r.LossPct,5:F1}% (bench {row.LossPct}±{row.LossTol})  p50 {r.RttP50,6:F0} ms (bench {row.RttP50})  max {r.RttMax,6:F0} ms (bench {row.RttMax})  sent {r.Sent}";
        return ok;
    }
}
