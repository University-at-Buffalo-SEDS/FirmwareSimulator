"""Execute the actual SPI GPS model; verify framing, checksums and UTC pacing."""
from pathlib import Path
import shutil
import os
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
@unittest.skipUnless((shutil.which('mcs') and shutil.which('mono')) or os.environ.get('CSHARP_TEST_DOTNET_IMAGE'),'Mono or explicitly selected local .NET image required')
class GpsNmeaModelTests(unittest.TestCase):
    def test_valid_bounded_one_hz_stream_and_reset_time(self):
        source=(ROOT/'renode/peripherals/SedsSpiSensors.cs').read_text()
        model=source[source.index('    public sealed class SedsNeoM9N'):source.index('    // MCP3564R')]
        code=r'''
using System;
using System.Collections.Generic;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.SPI;
using Antmicro.Renode.Peripherals.Timers;
namespace Antmicro.Renode.Core {public interface IMachine {object ClockSource{get;}}}
namespace Antmicro.Renode.Peripherals.SPI {public interface ISPIPeripheral {byte Transmit(byte b);void FinishTransmission();void Reset();}}
namespace Antmicro.Renode.Peripherals.Timers {
 public sealed class LimitTimer {
  public static LimitTimer Last;public event Action LimitReached;
  public LimitTimer(object clock,long frequency,object owner,string name,ulong limit,bool enabled,bool eventEnabled) {
   if(clock==null || frequency!=1000 || limit!=1000 || !enabled || !eventEnabled)throw new Exception("GPS must run at 1Hz");Last=this;
  }
  public void Fire(){if(LimitReached!=null)LimitReached();}
 }
}
namespace Antmicro.Renode.Peripherals.Sensors {
'''+model+r'''
}
class Machine:IMachine {public object ClockSource{get{return this;}}}
class Program {
 static void Require(bool value,string reason){if(!value)throw new Exception(reason);}
 static string Read(Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N gps) {
  var text=new StringBuilder();
  for(var i=0;i<1024;i++){var b=gps.Transmit(0xff);if(b==0xff){gps.FinishTransmission();return text.ToString();}text.Append((char)b);}
  throw new Exception("GPS output grew without bound");
 }
 static void Check(string text,string time,string date,string coordinates="4300.0000,N,07846.8000,W",string altitude="300.0",bool fix=true) {
  var lines=text.Split(new[]{"\r\n"},StringSplitOptions.RemoveEmptyEntries);
  Require(lines.Length==2,"exactly one bounded GGA/RMC fix");
  foreach(var line in lines){
   Require(line[0]=='$',"sentence start");var star=line.IndexOf('*');byte crc=0;
   for(var i=1;i<star;i++)crc^=(byte)line[i];
   Require(line.Substring(star+1)==crc.ToString("X2"),"NMEA checksum");
   Require(line.Contains(","+time+","),"virtual UTC advances independently of SPI traffic");
  }
  Require(lines[1].Contains(","+date+(fix?",,,A*":",,,N*")),"RMC date and fix mode");
  Require(lines[0].Contains(","+coordinates+(fix?",1,12,0.8,":",0,00,99.9,")+altitude+",M,"),"GGA position, quality, satellites and altitude");
  Require(lines[1].Contains((fix?",A,":",V,")+coordinates+",0.0,0.0,"),"RMC fix status and position agree with GGA");
 }
 static void Main(){
  var gps=new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine());var clock=LimitTimer.Last;
  Check(Read(gps),"000000.00","010126");Require(Read(gps)=="","no duplicate fixes before timer");
  clock.Fire();Check(Read(gps),"000001.00","010126");
  for(var i=0;i<10;i++)clock.Fire();
  Check(Read(gps),"000002.00","010126");Require(Read(gps)=="","bounded output while host stalls");
  gps.Reset();Check(Read(gps),"000011.00","010126");
  clock.Fire();Check(Read(gps),"000012.00","010126");
  for(var i=0;i<86400;i++)clock.Fire();gps.Reset();Check(Read(gps),"000012.00","020126");
  var bad=new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine(),failureEvery:1);
  Require(bad.Transmit(0xff)==0xff,"configured SPI failure remains a failure");
  System.Threading.Thread.CurrentThread.CurrentCulture=new System.Globalization.CultureInfo("fr-FR");
  var site=new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine(),latitude:31.758,longitude:-102.046,altitudeM:890.016);
  Check(Read(site),"000000.00","010126","3145.4800,N,10202.7600,W","890.016");
  var southern=new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine(),latitude:-33.5,longitude:151.25,altitudeM:-5.5);
  Check(Read(southern),"000000.00","010126","3330.0000,S,15115.0000,E","-5.5");
  var rounding=new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine(),latitude:89.999999999,longitude:-179.999999999,altitudeM:0);
  Check(Read(rounding),"000000.00","010126","9000.0000,N,18000.0000,W","0.0");
  foreach(var invalid in new[]{Double.NaN,Double.PositiveInfinity,Double.NegativeInfinity,90.1,-90.1}) {
   bool rejected=false;try {new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine(),latitude:invalid);}catch(ArgumentOutOfRangeException){rejected=true;}Require(rejected,"invalid latitude rejected");
  }
  foreach(var invalid in new[]{Double.NaN,Double.PositiveInfinity,Double.NegativeInfinity,180.1,-180.1}) {
   bool rejected=false;try {new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine(),longitude:invalid);}catch(ArgumentOutOfRangeException){rejected=true;}Require(rejected,"invalid longitude rejected");
  }
  foreach(var invalid in new[]{Double.NaN,Double.PositiveInfinity,Double.NegativeInfinity}) {
   bool rejected=false;try {new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine(),altitudeM:invalid);}catch(ArgumentOutOfRangeException){rejected=true;}Require(rejected,"invalid altitude rejected");
  }
  var file=System.IO.Path.Combine(System.IO.Path.GetTempPath(),"gps-fix-stimulus.txt");
  System.IO.File.WriteAllText(file,"0\n");
  var controlled=new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine(),stimulusPath:file);var fixtureClock=LimitTimer.Last;
  Check(Read(controlled),"000000.00","010126",fix:false);
  System.IO.File.WriteAllText(file,"1");Require(Read(controlled)=="","no SPI byte polls fixture or invents a new fix");
  fixtureClock.Fire();Check(Read(controlled),"000001.00","010126");
  System.IO.File.WriteAllText(file,"0");fixtureClock.Fire();
  // Preserve a complete pending batch when the next input changes; never splice NMEA.
  System.IO.File.WriteAllText(file,"1");fixtureClock.Fire();Check(Read(controlled),"000002.00","010126",fix:false);
  fixtureClock.Fire();Check(Read(controlled),"000004.00","010126");
  var second=4;
  foreach(var malformed in new[]{"", "true", "-1", "0,1", new string('1',17)}) {
    System.IO.File.WriteAllText(file,malformed);var before=controlled.StimulusRejected;
    Require(Read(controlled)==""&&controlled.StimulusRejected==before,"reads do not poll the file");
    fixtureClock.Fire();second++;
    Check(Read(controlled),new DateTime(2026,1,1).AddSeconds(second).ToString("HHmmss",System.Globalization.CultureInfo.InvariantCulture)+".00","010126");
    Require(controlled.StimulusRejected==before+1,"one rejected bounded read per second, retaining last good fix");
  }
  System.IO.File.Delete(file);var missing=new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine(),stimulusPath:file);var missingClock=LimitTimer.Last;
  Require(missing.StimulusRejected==1,"missing initial fixture rejected");Check(Read(missing),"000000.00","010126",fix:false);
  System.IO.File.WriteAllText(file,"1");missingClock.Fire();Check(Read(missing),"000001.00","010126");
  missing.Reset();Check(Read(missing),"000001.00","010126");
  System.IO.File.Delete(file);missingClock.Fire();Check(Read(missing),"000002.00","010126");Require(missing.StimulusRejected==2,"missing later input retains last good");
  var ordinary=new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine());
  var nofile=new Antmicro.Renode.Peripherals.Sensors.SedsNeoM9N(new Machine(),stimulusPath:"");
  Require(Read(ordinary)==Read(nofile),"empty fixture path preserves exact default NMEA bytes");
  Console.WriteLine("PASS: real GPS model, bounded1Hz/checksums/UTC, raw nofix-to-fix input and last-good rejection");
 }
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'gps.cs';path.write_text(code);exe=Path(tmp)/'gps.exe'
            image=os.environ.get('CSHARP_TEST_DOTNET_IMAGE')
            if image:
                (Path(tmp)/'contract.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><EnableDefaultCompileItems>false</EnableDefaultCompileItems><TreatWarningsAsErrors>true</TreatWarningsAsErrors></PropertyGroup><ItemGroup><Compile Include="gps.cs"/></ItemGroup></Project>')
                result=subprocess.run(['docker','run','--rm','--network=none','--user',str(os.getuid())+':'+str(os.getgid()),'--env','HOME=/tmp','--env','DOTNET_CLI_HOME=/tmp','--mount','type=bind,source='+tmp+',target=/fixture','--workdir','/fixture','--entrypoint','dotnet',image,'run','--project','contract.csproj'],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            else:
                subprocess.run(['mcs','-out:'+str(exe),str(path)],check=True)
                subprocess.run(['mono',str(exe)],check=True)
