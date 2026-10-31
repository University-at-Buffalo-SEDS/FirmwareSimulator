"""Exercise the production MCP model's timed conversion/IRQ/SPI contract."""
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

STUBS = r'''
using System;
using System.Collections.Generic;
namespace Antmicro.Renode.Time {public enum WorkMode {OneShot} public enum Direction {Ascending}}
namespace Antmicro.Renode.Core {
 public interface IMachine {object ClockSource{get;}}
 public sealed class Machine:IMachine {public object ClockSource{get{return this;}}}
 public sealed class GPIO {public bool IsSet;public int Falls;
  public void Set(bool value){if(IsSet&&!value)Falls++;IsSet=value;}}
}
namespace Antmicro.Renode.Peripherals.Timers {
 public sealed class LimitTimer {
  public static Dictionary<string,LimitTimer> Timers=new Dictionary<string,LimitTimer>();
  public event Action LimitReached;
  public bool Enabled;public ulong Limit,Value;
  public LimitTimer(object clock,long frequency,object owner,string name,ulong limit,bool enabled,bool eventEnabled,Antmicro.Renode.Time.WorkMode workMode,Antmicro.Renode.Time.Direction direction){
   if(clock==null||frequency!=1000000||!eventEnabled)throw new Exception("timer contract");
   Limit=limit;Enabled=enabled;Timers[name]=this;
  }
  public void Reset(){Enabled=false;}
  public void Fire(){if(!Enabled)throw new Exception("disabled timer fired");Enabled=false;LimitReached();}
 }
}
namespace Antmicro.Renode.Peripherals.Sensors {
 using Antmicro.Renode.Core;using Antmicro.Renode.Peripherals.Timers;
 public interface ISPIPeripheral{byte Transmit(byte value);void FinishTransmission();void Reset();}
 public interface IGPIOReceiver{void OnGPIO(int number,bool value);}
'''

TEST = r'''
class Test {
 static Antmicro.Renode.Peripherals.Sensors.SedsMcp3564r adc;
 static Antmicro.Renode.Peripherals.Timers.LimitTimer Clock{get{return Antmicro.Renode.Peripherals.Timers.LimitTimer.Timers["adcConversion"];}}
 static Antmicro.Renode.Peripherals.Timers.LimitTimer Release{get{return Antmicro.Renode.Peripherals.Timers.LimitTimer.Timers["adcReadyRelease"];}}
 static void Require(bool value,string message){if(!value)throw new Exception(message);}
 static void Write(params byte[] bytes){adc.OnGPIO(0,false);foreach(var b in bytes)adc.Transmit(b);adc.OnGPIO(0,true);}
 static uint Read(out byte status){adc.OnGPIO(0,false);status=adc.Transmit(0x41);uint raw=0;
  for(var i=0;i<4;i++)raw=(raw<<8)|adc.Transmit(0);adc.OnGPIO(0,true);return raw;}
 static bool DriverAccept(byte status){return (status&0xf8)==0x10&&(status&3)==3;}
 static void Main(){
  Require(DriverAccept(0x13)&&DriverAccept(0x17),"Microchip status address01 + complementary bit3");
  foreach(byte bad in new byte[]{0x1b,0x1f,0x00,0xff,0x12,0x11})Require(!DriverAccept(bad),"latest driver rejects bad address/fault status");
  adc=new Antmicro.Renode.Peripherals.Sensors.SedsMcp3564r(new Antmicro.Renode.Core.Machine(),externalChipSelect:true);
  Require(!Clock.Enabled&&adc.IRQ.IsSet,"reset is idle and IRQ high");
  Write(0x46,0x82,0x0c,0xcf,0xf0,0x06,0x08);Write(0x5e,0,0,0x43);Write(0x68);
  Require(Clock.Enabled&&Clock.Limit==384&&Release.Limit==380,"OSR256 AZ_MUX three-channel conversion period");
  byte status;Read(out status);Console.WriteLine("MCP_NOT_READY_STATUS="+status.ToString("X2"));Require(status==0x17&&adc.IRQ.IsSet,"no fresh sample before physical conversion");
  Clock.Fire();Require(!adc.IRQ.IsSet,"conversion drives falling edge independent of read");
  var first=Read(out status);Console.WriteLine("MCP_READY_STATUS="+status.ToString("X2"));Require(DriverAccept(status)&&status==0x13&&(first>>28)==6&&adc.IRQ.IsSet,"fresh CH6 full DMA frame");
  var repeat=Read(out status);Require(status==0x17&&repeat==first,"repeat read is not a new sample");
  Clock.Fire();Read(out status);Require(status==0x13,"next fresh conversion");
  Clock.Fire();Require(!adc.IRQ.IsSet,"unread sample IRQ");Release.Fire();Require(adc.IRQ.IsSet,"unread pin releases before next conversion");
  var falls=adc.IRQ.Falls;Clock.Fire();Require(adc.IRQ.Falls==falls+1,"unread sample still permits next falling edge");
  Write(0x46,0x82);Require(!Clock.Enabled&&!Release.Enabled&&adc.IRQ.IsSet,"standby stops IRQ and conversion");
  Write(0x4a,0x8c);Write(0x5e,0,0x10,0);Write(0x68);
  Require(Clock.Limit==1536,"temperature clock prescaler");Clock.Fire();Require((Read(out status)>>28)==12&&status==0x13,"temperature scan data-ready");
  adc.Reset();Require(!Clock.Enabled&&!Release.Enabled&&adc.IRQ.IsSet,"reset quiesces timers");
 }
}
'''

class McpReadyTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('CSHARP_TEST_DOTNET_IMAGE'), 'Select cached .NET test image explicitly')
    def test_timed_conversion_irq_and_five_byte_dma(self):
        source = (ROOT/'renode/peripherals/SedsSpiSensors.cs').read_text()
        model = source[source.index('    public sealed class SedsMcp3564r'):]
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp)
            (p/'test.cs').write_text(STUBS+model+TEST)
            (p/'test.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><TreatWarningsAsErrors>true</TreatWarningsAsErrors></PropertyGroup></Project>')
            result=subprocess.run(['docker','run','--rm','--network=none','--cpus=1','--user',str(os.getuid())+':'+str(os.getgid()),'--env','HOME=/tmp','--env','DOTNET_CLI_HOME=/tmp','--mount','type=bind,source='+tmp+',target=/fixture','--workdir','/fixture','--entrypoint','dotnet',os.environ['CSHARP_TEST_DOTNET_IMAGE'],'run','--project','test.csproj'],capture_output=True,text=True)
            print(result.stdout+result.stderr)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    @unittest.skipUnless(os.environ.get('RENODE_TEST_IMAGE'), 'Select cached Renode test image explicitly')
    def test_actual_u5_gpio_exti_nvic_and_ready_frame(self):
        result = subprocess.run([
            'docker', 'run', '--rm', '--network=none', '--cpus=1',
            '--mount', 'type=bind,source='+str(ROOT/'renode')+',target=/opt/firmware-sim/renode,readonly',
            '--entrypoint', '/opt/renode/renode', os.environ['RENODE_TEST_IMAGE'],
            '--disable-xwt', '--console', '--plain', '/opt/firmware-sim/renode/tests/mcp-drdy.resc',
        ], capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr
        print(output)
        self.assertEqual(result.returncode, 0, output)
        for marker in ('MCP_NO_EARLY_CONVERSION_PASS', 'MCP_DRDY_GPIO_EXTI_NVIC_PASS', 'MCP_FIVE_BYTE_STATUS_DATA_PASS',
                       'MCP_NEXT_CONVERSION_IRQ_PASS', 'MCP_STANDBY_QUIESCENT_PASS'):
            self.assertIn(marker, output)
        # Renode monitor script errors can still produce process exit0.
        self.assertNotIn('There was an error executing command', output)
        self.assertNotIn('AssertionError', output)
