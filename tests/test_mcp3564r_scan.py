"""Run the actual Renode ADC model with minimal peripheral interface stubs."""
from pathlib import Path
import shutil
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((shutil.which('mcs') and shutil.which('mono')) or os.environ.get('CSHARP_TEST_DOTNET_IMAGE'), 'Mono or explicitly selected local .NET test image required')
class Mcp3564rScanTests(unittest.TestCase):
    def test_legacy_and_tagged_two_channel_data(self):
        source = (ROOT / 'renode/peripherals/SedsSpiSensors.cs').read_text()
        model = source[source.index('    public sealed class SedsMcp3564r'):]
        harness = '''
using System;
using System.Collections.Generic;
namespace Antmicro.Renode.Time {public enum WorkMode {OneShot} public enum Direction {Ascending}}
namespace Antmicro.Renode.Peripherals.Sensors {
public interface ISPIPeripheral { byte Transmit(byte value); void FinishTransmission(); void Reset(); }
public interface IGPIOReceiver { void OnGPIO(int number, bool value); }
public interface IMachine {object ClockSource {get;}}
public class GPIO {public void Set(bool value) {}}
public class LimitTimer {
 public LimitTimer(object clock,long frequency,object owner,string name,ulong limit,bool enabled,bool eventEnabled,Antmicro.Renode.Time.WorkMode workMode,Antmicro.Renode.Time.Direction direction){}
 public event Action LimitReached {add{} remove{}}
 public bool Enabled; public ulong Limit,Value; public void Reset(){}
}
''' + model + '''
class Test {
    static Antmicro.Renode.Peripherals.Sensors.SedsMcp3564r adc =
        new Antmicro.Renode.Peripherals.Sensors.SedsMcp3564r(null,externalChipSelect: true);
    static void Write(params byte[] bytes) {
        adc.OnGPIO(0, false);
        foreach(var b in bytes) adc.Transmit(b);
        adc.OnGPIO(0, true);
    }
    static uint Read(int count) {
        adc.OnGPIO(0, false);
        if(adc.Transmit(0x41) != 0) throw new Exception("ADC not ready");
        // HAL command and DMA data are separate controller transfers, same CS.
        adc.FinishTransmission();
        uint value=0;
        for(var i=0; i<count; i++) value=(value<<8)|adc.Transmit(0);
        adc.OnGPIO(0, true);
        return value;
    }
    static void Equal(uint actual, uint expected) {
        if(actual != expected) throw new Exception(actual.ToString("X8")+" != "+expected.ToString("X8"));
    }
    static void Main() {
        Equal(Read(3), 0x100000);
        Write(0x78); // reset
        Write(0x46, 0x82, 0x14, 0xcf, 0xf0, 0x03, 0x08); // CONFIG0..MUX
        Write(0x5e, 0x00, 0x00, 0x03); // SCAN CH0 + CH1
        Write(0x68); // start
        // Datasheet 5.15: highest enabled SCAN bit is converted first.
        Equal(Read(4), 0x10200000);
        Equal(Read(4), 0x00100000);
        Equal(Read(4), 0x10200011);
        Equal(Read(4), 0x00100011);
        Write(0x5e, 0x00, 0x00, 0x00); // disable scan
        Write(0x5a, 0x18); // MUX CH1-AGND
        Equal(Read(4), 0x00200022); // MUX mode always reports CH_ID=0 (5.6).
        adc.Reset();
        Equal(Read(3), 0x100000);
        adc.Reset();
        Write(0x46, 0x82, 0x14, 0xcf, 0xf0, 0x03, 0x08);
        Write(0x5e, 0x00, 0x02, 0x01); // Differential P6/ID9 plus CH0.
        Write(0x68);
        Equal(Read(4), 0x9ff00000); // CH2-CH3 = -0x100000, sign extended/tagged.
        Equal(Read(4), 0x00100000);
        Equal(Read(4), 0x9ff00011);
        Write(0x5e, 0x00, 0x02, 0x00); // P6-only fast scan must not become MUX/CH0.
        Write(0x68);
        Equal(Read(4), 0x9ff00022);
        Equal(Read(4), 0x9ff00033);
        Write(0x5e, 0x00, 0x10, 0x00); // TEMP-only scan must complete, not time out as CH0.
        Write(0x68);
        Equal(Read(4), 0xc004a9f4);
        for(var i=0; i<1000; i++) Equal(Read(4), 0xc004a9f4);
        Write(0x5e, 0x00, 0x80, 0x00); // Highest channel ID fits the 4-bit tag.
        Write(0x68);
        Equal(Read(4), 0xf0000000);
        // Opt-in fixtures change ADC raw input only; real SPI formatting remains in use.
        var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "mcp-stimulus.csv");
        var codes = new string[16];for(var i=0;i<16;i++) codes[i]="0";
        codes[0]="8388607";codes[1]="8388608";codes[9]="16777215";
        System.IO.File.WriteAllText(path,string.Join(",",codes));
        adc=new Antmicro.Renode.Peripherals.Sensors.SedsMcp3564r(null,externalChipSelect:true,stimulusPath:path);
        Equal(Read(3),0x7fffff); // Maximum positive signed code.
        for(var i=0;i<19;i++) Equal(Read(3),0x7fffff); // Explicit fixture holds; no +17 drift.
        Write(0x46,0x82,0x14,0xcf,0xf0,0x03,0x18); // Tagged 32-bit, MUX CH1.
        Equal(Read(4),0x0f800000); // Most-negative 24-bit signed sample sign-extended/tagged.
        Write(0x5e,0x00,0x02,0x00);Write(0x68);
        Equal(Read(4),0x9fffffff); // Differential ID9=-1 retains channel tag/sign.
        foreach(var invalid in new[]{"1,2",string.Join(",",codes).Replace("16777215","16777216"),new string('1',1025),string.Join(",",codes).Replace("16777215","-1")}) {
            System.IO.File.WriteAllText(path,invalid);var before=adc.StimulusRejected;
            for(var i=0;i<10;i++) Equal(Read(4),0x9fffffff);
            if(adc.StimulusRejected!=before+1) throw new Exception("one bounded poll/rejection per ten conversions");
        }
        // A bad final channel must not partially overwrite an earlier valid one.
        codes[0]="123";codes[15]="bad";System.IO.File.WriteAllText(path,string.Join(",",codes));
        Write(0x5e,0,0,0);Write(0x5a,0x08);
        for(var i=0;i<10;i++) Equal(Read(4),0x007fffff);
        codes[15]="0";System.IO.File.WriteAllText(path,string.Join(",",codes));
        for(var i=0;i<10;i++) Read(4);Equal(Read(4),123);
        System.IO.File.Delete(path);var rejected=adc.StimulusRejected;
        for(var i=0;i<10;i++) Equal(Read(4),123);
        if(adc.StimulusRejected!=rejected+1) throw new Exception("missing fixture holds last good code");
        bool invalidChannel=false,invalidCode=false;
        try {adc.SetChannelSample(16,1);}catch(ArgumentOutOfRangeException){invalidChannel=true;}
        try {adc.SetChannelSample(0,0x1000000);}catch(ArgumentOutOfRangeException){invalidCode=true;}
        if(!invalidChannel||!invalidCode) throw new Exception("setter range validation");
        adc.SetChannelSample(0,0xffffff);Write(0x46,0x82,0x14,0xcf,0x20); // signed32 data format2.
        Equal(Read(4),0xffffffff);
        adc.Reset();Write(0x46,0x82,0x14,0xcf,0x10); // format1 left aligned raw24.
        adc.SetChannelSample(0,0x800000);Equal(Read(4),0x80000000);
        adc=new Antmicro.Renode.Peripherals.Sensors.SedsMcp3564r(null,externalChipSelect:true);
        Equal(Read(3),0x100000);Equal(Read(3),0x100011); // Legacy no-file path unchanged.

    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'adc.cs'
            path.write_text(harness)
            binary = Path(directory) / 'adc.exe'
            image = os.environ.get('CSHARP_TEST_DOTNET_IMAGE')
            if image:
                (Path(directory) / 'contract.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><EnableDefaultCompileItems>false</EnableDefaultCompileItems><TreatWarningsAsErrors>true</TreatWarningsAsErrors></PropertyGroup><ItemGroup><Compile Include="adc.cs"/></ItemGroup></Project>')
                result = subprocess.run(['docker','run','--rm','--network=none','--cpus=1','--user',str(os.getuid())+':'+str(os.getgid()),'--env','HOME=/tmp','--env','DOTNET_CLI_HOME=/tmp','--mount','type=bind,source='+directory+',target=/fixture','--workdir','/fixture','--entrypoint','dotnet',image,'run','--project','contract.csproj'],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            else:
                subprocess.run(['mcs', '-out:' + str(binary), str(path)], check=True, capture_output=True)
                subprocess.run(['mono', str(binary)], check=True, capture_output=True)
