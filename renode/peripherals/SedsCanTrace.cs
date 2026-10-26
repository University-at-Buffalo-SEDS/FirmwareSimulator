using System;
using System.Globalization;
using System.IO;
using System.Text;

namespace Antmicro.Renode.Utilities
{
    // Passive diagnostic only. I/O failure marks the capture invalid and must
    // never interrupt delivery to the CAN hub or change controller state.
    public sealed class SedsCanTrace
    {
        public const ulong DefaultMaxBytes = 64UL * 1024 * 1024;
        public const int HeaderBytes = 32;
        public const int RecordBytes = 80;
        public SedsCanTrace(string path, ulong untilUs, ulong maxBytes = DefaultMaxBytes)
        {
            if(untilUs == 0 || maxBytes < HeaderBytes + RecordBytes || maxBytes > DefaultMaxBytes)
                throw new ArgumentOutOfRangeException();
            this.untilUs = untilUs; this.maxBytes = maxBytes; statusPath = path + ".status.json";
            try {
                stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read, 4096);
                ownsPath = true;
                writer = new BinaryWriter(stream, Encoding.ASCII);
                writer.Write(Encoding.ASCII.GetBytes("SEDSCAN1"));
                writer.Write((uint)1); writer.Write((uint)RecordBytes);
                writer.Write(untilUs); writer.Write(maxBytes);
                FlushStatus();
            } catch(Exception error) { Fault(error); }
        }
        public bool Finished { get; private set; }
        public bool Capped { get; private set; }
        public bool Faulted { get; private set; }
        public ulong Records { get; private set; }
        public void Record(ulong now, uint id, byte flags, byte[] data)
        {
            lock(sync) {
                if(Faulted || Finished) return;
                try {
                    if(now > untilUs) { Finished = true; FlushStatus(); writer.Dispose(); return; }
                    if(data == null || data.Length > 64 || (flags & 0xf0) != 0)
                        throw new ArgumentException("Invalid CAN trace frame");
                    if(HeaderBytes + (Records + 1) * RecordBytes > maxBytes) {
                        if(!Capped) { Capped = true; FlushStatus(); }
                        return;
                    }
                    writer.Write(now); writer.Write(id); writer.Write(flags);
                    writer.Write((byte)data.Length); writer.Write((ushort)0);
                    writer.Write(data);
                    for(int i = data.Length; i < 64; i++) writer.Write((byte)0);
                    Records++; lastRecordUs = now;
                    if(now >= lastFlushUs + 100000) { lastFlushUs = now; FlushStatus(); }
                } catch(Exception error) { Fault(error); }
            }
        }
        private void FlushStatus()
        {
            writer.Flush();
            File.WriteAllText(statusPath, "{\"version\":1,\"record_bytes\":80,\"records\":"
                + Records.ToString(CultureInfo.InvariantCulture) + ",\"until_us\":" + untilUs
                + ",\"last_record_us\":" + lastRecordUs + ",\"max_bytes\":" + maxBytes
                + ",\"finished\":" + (Finished ? "true" : "false")
                + ",\"capped\":" + (Capped ? "true" : "false") + ",\"faulted\":false}");
        }
        private void Fault(Exception error)
        {
            Faulted = true; Finished = false;
            try { if(writer != null) writer.Dispose(); else if(stream != null) stream.Dispose(); } catch(Exception) { }
            // Never overwrite an earlier capture's status when CreateNew failed.
            if(ownsPath) try { File.WriteAllText(statusPath, "{\"version\":1,\"faulted\":true,\"finished\":false,\"error_type\":\""
                + error.GetType().Name + "\"}"); } catch(Exception) { }
        }
        private readonly object sync = new object();
        private readonly string statusPath;
        private readonly ulong untilUs, maxBytes;
        private FileStream stream;
        private BinaryWriter writer;
        private ulong lastRecordUs, lastFlushUs;
        private bool ownsPath;
    }
}
