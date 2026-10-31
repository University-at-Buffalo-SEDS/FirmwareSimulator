"""Use the unchanged real SDMMC model for latest DAQ's late-mask IDMA path."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STUBS = r'''
namespace Antmicro.Renode.Core {
 public interface IMachine {Bus GetSystemBus(object peripheral);}
 public class Machine:IMachine {public Bus Bus=new Bus();public Bus GetSystemBus(object p){return Bus;}}
 public class Bus {System.Collections.Generic.Dictionary<ulong,byte> b=new System.Collections.Generic.Dictionary<ulong,byte>();
  public void WriteByte(ulong a,byte v,object c){b[a]=v;}
  public byte ReadByte(ulong a,object c){byte v;return b.TryGetValue(a,out v)?v:(byte)0;}}
 public class GPIO {public bool IsSet;public void Set(bool v){IsSet=v;}}
}
namespace Antmicro.Renode.Peripherals.Bus {public interface IDoubleWordPeripheral{} public interface IKnownSize{}}
'''
TEST = r'''
class Test {
 static void Need(bool ok,string label){if(!ok)throw new System.Exception(label);}
 static void Command(Antmicro.Renode.Peripherals.Storage.SedsStm32Sdmmc s,uint c,uint a){s.WriteDoubleWord(0x38,uint.MaxValue);s.WriteDoubleWord(0x08,a);s.WriteDoubleWord(0x0c,(1u<<12)|c);}
 static void Main(){
  var m=new Antmicro.Renode.Core.Machine();
  using(var s=new Antmicro.Renode.Peripherals.Storage.SedsStm32Sdmmc(m)){
   s.CardCapacityBytes=8UL*1024*1024;Command(s,7,1u<<16);
   const uint from=0x20001000,to=0x20003000;
   for(uint i=0;i<4096;i++)m.Bus.WriteByte(from+i,(byte)(i*73+i/32),s);
   s.WriteDoubleWord(0x3c,0);s.WriteDoubleWord(0x58,from);s.WriteDoubleWord(0x50,1);s.WriteDoubleWord(0x28,4096);Command(s,25,1);
   Need(s.GetBytesWritten()==4096,"eight-sector IDMA writes actual bytes");
   Need((s.ReadDoubleWord(0x34)&(1u<<8))!=0&&!s.IRQ.IsSet,"completion retained while latest driver has IRQ disarmed");
   s.WriteDoubleWord(0x3c,1u<<8);Need(s.IRQ.IsSet,"late mask enable raises pending genuine DATAEND");
   s.WriteDoubleWord(0x3c,0);Need(!s.IRQ.IsSet,"driver disarm drops IRQ level");
   s.WriteDoubleWord(0x38,uint.MaxValue);Need(!s.IRQ.IsSet,"clear completion before next operation");
   s.WriteDoubleWord(0x58,to);s.WriteDoubleWord(0x28,4096);Command(s,18,1);
   Need((s.ReadDoubleWord(0x34)&(1u<<8))!=0&&!s.IRQ.IsSet,"IDMA read completion retained while masked");
   for(uint i=0;i<4096;i++)Need(m.Bus.ReadByte(to+i,s)==m.Bus.ReadByte(from+i,s),"IDMA full read/write roundtrip");
   s.WriteDoubleWord(0x3c,1u<<8);Need(s.IRQ.IsSet,"second late enable sees read completion");
   s.WriteDoubleWord(0x38,uint.MaxValue);Need(!s.IRQ.IsSet,"clearing status removes read IRQ");
   s.WriteDoubleWord(0x3c,1u<<8);Need(!s.IRQ.IsSet,"no phantom or stale IRQ after clear");
  }
  System.Console.WriteLine("SDMMC_LATEST_DAQ_IDMA_LATE_MASK_IRQ_PASS");
 }
}
'''

class SdmmcLatestDaqIrqTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('CSHARP_TEST_DOTNET_IMAGE'), 'Select cached .NET image')
    def test_idma_late_mask_completion_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p/'test.cs').write_text((ROOT/'renode/peripherals/SedsStm32Sdmmc.cs').read_text()+STUBS+TEST)
            (p/'test.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><TreatWarningsAsErrors>true</TreatWarningsAsErrors></PropertyGroup></Project>')
            result = subprocess.run(['docker','run','--rm','--network=none','--cpus=1','--user',str(os.getuid())+':'+str(os.getgid()),'--env','HOME=/tmp','--env','DOTNET_CLI_HOME=/tmp','--env','DOTNET_CLI_TELEMETRY_OPTOUT=1','--mount','type=bind,source='+tmp+',target=/fixture','--workdir','/fixture','--entrypoint','dotnet',os.environ['CSHARP_TEST_DOTNET_IMAGE'],'run','--project','test.csproj'], capture_output=True,text=True,timeout=60)
            print(result.stdout+result.stderr)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('SDMMC_LATEST_DAQ_IDMA_LATE_MASK_IRQ_PASS',result.stdout)

if __name__ == '__main__':
    unittest.main()
