using System;
using System.IO;
using System.Diagnostics;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Storage;
class Program
{
    static void Require(bool ok,string message) { if(!ok)throw new Exception(message); }
    static void Command(SedsStm32Sdmmc sd,uint command,uint argument=0) {
        sd.WriteDoubleWord(0x38,UInt32.MaxValue);sd.WriteDoubleWord(0x08,argument);
        sd.WriteDoubleWord(0x0c,(1u<<12)|command);
    }
    static void Select(SedsStm32Sdmmc sd) { Command(sd,7,1u<<16); }
    static byte Pattern(int i) => (byte)(i*73+(i>>5));
    static void Write(SedsStm32Sdmmc sd,uint block,int length) {
        sd.WriteDoubleWord(0x28,(uint)length);Command(sd,25,block);
        for(int i=0;i<length;i+=4)sd.WriteDoubleWord(0x80,(uint)(Pattern(i)|(Pattern(i+1)<<8)|(Pattern(i+2)<<16)|(Pattern(i+3)<<24)));
        Require((sd.ReadDoubleWord(0x34)&(1u<<8))!=0,"write completes");
    }
    static void Read(SedsStm32Sdmmc sd,uint block,int length) {
        sd.WriteDoubleWord(0x28,(uint)length);Command(sd,18,block);
        for(int i=0;i<length;i+=4) {
            uint actual=sd.ReadDoubleWord(0x80);
            for(int j=0;j<4;j++)Require((byte)(actual>>(8*j))==Pattern(i+j),"read data survives backing/reset");
        }
    }
    static void Main() {
        long before=GC.GetTotalMemory(true);
        using(var sd=new SedsStm32Sdmmc(new Machine())) {
            sd.CardCapacityBytes=64UL*1024*1024*1024;
            Require(GC.GetTotalMemory(true)-before<8*1024*1024,"capacity must not allocate card-sized memory");
            Require(sd.CardCapacityBytes==64UL*1024*1024*1024,"64 GiB capacity");
            Command(sd,9);uint csize=((sd.ReadDoubleWord(0x18)&63)<<16)|(sd.ReadDoubleWord(0x1c)>>16);
            Require(((ulong)csize+1)*512*1024==sd.CardCapacityBytes,"CSD advertises actual SDHC capacity");
            Select(sd);uint block=(uint)((5UL*1024*1024*1024)/512)-1;
            Write(sd,block,1024);Require(sd.GetBytesWritten()==1024,"actual write count");
            sd.Reset();Select(sd);Read(sd,block,1024);
            Command(sd,17,(uint)(sd.CardCapacityBytes/512));
            Require((sd.ReadDoubleWord(0x34)&(1u<<3))!=0,"out-of-range read is rejected");
            var image=Path.Combine(Path.GetTempPath(),"seds-export-"+Guid.NewGuid().ToString("N"));
            try {
                sd.SaveCardImage(image);Require(new FileInfo(image).Length==(long)sd.CardCapacityBytes,"sparse export length");
                using(var f=File.OpenRead(image)) { f.Position=(long)block*512;for(int i=0;i<1024;i++)Require(f.ReadByte()==Pattern(i),"export retains data above 4 GiB");f.Position=0;Require(f.ReadByte()==0,"unwritten hole remains zero"); }
                bool refused=false;try { sd.SaveCardImage(image); }catch(IOException){refused=true;}Require(refused,"export never overwrites evidence");
            }finally { File.Delete(image); }
            Command(sd,24,0);sd.EjectCard();sd.WriteDoubleWord(0x80,123);
            Command(sd,17);Require((sd.ReadDoubleWord(0x34)&(1u<<2))!=0,"removed card never acknowledges");
        }
        var machine=new Machine();
        using(var sd=new SedsStm32Sdmmc(machine)) {
            sd.CardCapacityBytes=8UL*1024*1024*1024;Select(sd);
            const uint source=0x20001000,target=0x20008000;
            const int length=8192;
            for(int i=0;i<length;i++)machine.Bus.WriteByte(source+(uint)i,Pattern(i),sd);
            sd.WriteDoubleWord(0x58,source);sd.WriteDoubleWord(0x50,1);sd.WriteDoubleWord(0x28,length);
            uint block=(uint)((5UL*1024*1024*1024)/512);
            Command(sd,25,block);
            Require(sd.GetBytesWritten()==length,"IDMA writes complete above 4 GiB in bounded chunks");
            Require((sd.ReadDoubleWord(0x34)&(1u<<8))!=0 && sd.ReadDoubleWord(0x30)==0,"IDMA write completion is pending before interrupt mask enable");
            sd.WriteDoubleWord(0x3c,1u<<8);Require(sd.IRQ.IsSet,"HAL enabling DATAEND after CMD sees completion");
            sd.WriteDoubleWord(0x38,UInt32.MaxValue);Require(!sd.IRQ.IsSet,"write completion interrupt clears");
            sd.WriteDoubleWord(0x58,target);sd.WriteDoubleWord(0x28,length);Command(sd,18,block);
            for(int i=0;i<length;i++)Require(machine.Bus.ReadByte(target+(uint)i,sd)==Pattern(i),"IDMA read/write data round trip");
            ulong written=sd.GetBytesWritten();
            Command(sd,25,(uint)(sd.CardCapacityBytes/512));Require(sd.GetBytesWritten()==written,"out-of-range IDMA never mutates card");
        }
        using(var sd=new SedsStm32Sdmmc(new Machine())) {
            sd.LoadCardBytes(new byte[]{1,2,3});Select(sd);Command(sd,17,0);
            Require(sd.ReadDoubleWord(0x80)==0x00030201,"legacy image pads to a sector");
            sd.FailureEvery=1;Command(sd,17,0);Require((sd.ReadDoubleWord(0x34)&(1u<<2))!=0,"failure injection survives storage change");
        }
        using(var sd=new SedsStm32Sdmmc(new Machine())) {
            sd.FilesystemFixture="fat32:SEDSDAQ.ID";sd.CardCapacityBytes=8UL*1024*1024*1024;
            Select(sd);Command(sd,17,0);uint first=sd.ReadDoubleWord(0x80);Require((first&255)==0xeb || (first&255)==0xe9,"formatted boot sector");
            var image=Path.Combine(Path.GetTempPath(),"seds-fat-export-"+Guid.NewGuid().ToString("N"));
            try {
                sd.SaveCardImage(image);
                Run("fsck.fat","-n "+image);
                Run("mdir","-i "+image+" ::SEDSDAQ.ID");
            }finally {File.Delete(image);}
            bool refused=false;try {sd.FilesystemFixture="fat32:../bad";}catch(ArgumentException){refused=true;}Require(refused,"fixture marker cannot escape image");
        }
        Console.WriteLine("PASS: sparse 64 GiB SD, CSD, >4 GiB FIFO/read/export, IDMA round trip/completion, reset/removal/errors, FAT32 and provisioning fixture");
    }
    static void Run(string tool,string arguments) {
        using(var process=Process.Start(new ProcessStartInfo(tool,arguments){UseShellExecute=false})) {
            process.WaitForExit();Require(process.ExitCode==0,tool+" validates exported filesystem");
        }
    }
}
namespace Antmicro.Renode.Core {
    public interface IMachine { Bus GetSystemBus(object peripheral); }
    public sealed class Machine:IMachine { public readonly Bus Bus=new Bus(); public Bus GetSystemBus(object peripheral)=>Bus; }
    public sealed class Bus {
        readonly System.Collections.Generic.Dictionary<ulong,byte> bytes=new System.Collections.Generic.Dictionary<ulong,byte>();
        public void WriteByte(ulong address,byte value,object context){bytes[address]=value;}
        public byte ReadByte(ulong address,object context){byte value;return bytes.TryGetValue(address,out value)?value:(byte)0;}
    }
    public sealed class GPIO { public bool IsSet; public void Set(bool value){IsSet=value;} }
}
namespace Antmicro.Renode.Peripherals.Bus {
    public interface IDoubleWordPeripheral {}
    public interface IKnownSize {}
}
