using System;
using System.IO;
using System.Text;
using Antmicro.Renode.Utilities;

class Program
{
    static void Check(bool value, string name) { if(!value) throw new Exception(name); }
    static int Main()
    {
        string root = Path.Combine(Path.GetTempPath(), "seds-can-trace-" + Guid.NewGuid());
        Directory.CreateDirectory(root);
        try {
            string path = Path.Combine(root, "frame.bin");
            var trace = new SedsCanTrace(path, 1000000);
            Check(!trace.Finished && !trace.Faulted, "initial capture is incomplete");
            byte[] payload = new byte[64]; for(int i=0;i<64;i++) payload[i]=(byte)i;
            trace.Record(999999, 0x1234567, 11, payload);
            trace.Record(1000000, 0x321, 0, new byte[]{7});
            trace.Record(1000001, 0, 0, new byte[0]);
            Check(trace.Finished && !trace.Capped && !trace.Faulted && trace.Records==2, "exact horizon");
            using(var reader = new BinaryReader(File.OpenRead(path))) {
                Check(Encoding.ASCII.GetString(reader.ReadBytes(8))=="SEDSCAN1", "magic");
                Check(reader.ReadUInt32()==1 && reader.ReadUInt32()==80 && reader.ReadUInt64()==1000000, "header");
                Check(reader.ReadUInt64()==SedsCanTrace.DefaultMaxBytes, "bounded file");
                Check(reader.ReadUInt64()==999999 && reader.ReadUInt32()==0x1234567 && reader.ReadByte()==11 && reader.ReadByte()==64 && reader.ReadUInt16()==0, "frame metadata");
                for(int i=0;i<64;i++) Check(reader.ReadByte()==i, "payload preserved");
                Check(reader.ReadUInt64()==1000000 && reader.ReadUInt32()==0x321 && reader.ReadByte()==0 && reader.ReadByte()==1 && reader.ReadUInt16()==0, "short frame metadata");
                Check(reader.ReadByte()==7, "short payload");for(int i=1;i<64;i++) Check(reader.ReadByte()==0, "zero padding");
                Check(reader.BaseStream.Position==reader.BaseStream.Length, "no post-horizon frame");
            }
            string status = File.ReadAllText(path+".status.json");
            var duplicate = new SedsCanTrace(path, 1000000);
            Check(duplicate.Faulted && File.ReadAllText(path+".status.json")==status, "old evidence never overwritten");
            var cap = new SedsCanTrace(Path.Combine(root,"cap.bin"), 100, 112);
            cap.Record(1,1,0,new byte[0]);cap.Record(2,1,0,new byte[0]);cap.Record(101,1,0,new byte[0]);
            Check(cap.Capped && cap.Finished && cap.Records==1 && new FileInfo(Path.Combine(root,"cap.bin")).Length==112, "cap explicit");
            var invalid = new SedsCanTrace(Path.Combine(root,"invalid.bin"), 100);
            invalid.Record(1,1,0,new byte[65]);
            Check(invalid.Faulted && !invalid.Finished, "bad frame invalidates evidence without throwing into CAN");
            var missing = new SedsCanTrace(Path.Combine(root,"absent","frame.bin"), 100);
            missing.Record(1,1,0,new byte[0]);Check(missing.Faulted, "I/O fault does not propagate");
            Console.WriteLine("CAN_TRACE_BINARY_BOUNDARY_CAP_FAILURE_CONTRACTS_PASS");
            return 0;
        } finally { Directory.Delete(root,true); }
    }
}
