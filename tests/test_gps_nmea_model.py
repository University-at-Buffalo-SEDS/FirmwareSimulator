"""Execute the actual SPI GPS model; verify framing, checksums and UTC pacing."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
@unittest.skipUnless(shutil.which('mcs') and shutil.which('mono'),'Mono compiler/runtime required')
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
 static void Check(string text,string time,string date) {
  var lines=text.Split(new[]{"\r\n"},StringSplitOptions.RemoveEmptyEntries);
  Require(lines.Length==2,"exactly one bounded GGA/RMC fix");
  foreach(var line in lines){
   Require(line[0]=='$',"sentence start");var star=line.IndexOf('*');byte crc=0;
   for(var i=1;i<star;i++)crc^=(byte)line[i];
   Require(line.Substring(star+1)==crc.ToString("X2"),"NMEA checksum");
   Require(line.Contains(","+time+","),"virtual UTC advances independently of SPI traffic");
  }
  Require(lines[1].Contains(","+date+",,,A*"),"RMC date and active fix");
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
  Console.WriteLine("PASS: real GPS model, valid NMEA, bounded1Hz, UTC rollover, reset continuity and SPI failure");
 }
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'gps.cs';path.write_text(code);exe=Path(tmp)/'gps.exe'
            subprocess.run(['mcs','-out:'+str(exe),str(path)],check=True)
            subprocess.run(['mono',str(exe)],check=True)
