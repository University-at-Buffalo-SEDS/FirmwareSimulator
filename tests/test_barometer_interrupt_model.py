"""Execute the production BMP390 model with a deterministic timer fixture."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('FIRMWARE_SIM_RENODE_TEST_IMAGE'),
                     'An explicitly selected local Renode image is required')
class BarometerRenodeClock(unittest.TestCase):
    def test_real_renode_virtual_clock(self):
        result = subprocess.run(['docker', 'run', '--rm', '--network=none',
            '--mount', f'type=bind,source={ROOT / "renode"},target=/opt/firmware-sim/renode,readonly',
            '--entrypoint', '/opt/renode/renode', os.environ['FIRMWARE_SIM_RENODE_TEST_IMAGE'],
            '--disable-xwt', '--console', '--execute',
            'include @/opt/firmware-sim/renode/tests/barometer-interrupt.resc'],
            capture_output=True, text=True, timeout=60)
        log = result.stdout + result.stderr
        # Renode's console can exit zero after a script assertion fails.
        self.assertEqual(result.returncode, 0, log)
        self.assertIn('BMP390_REAL_VIRTUAL_CLOCK_INTERRUPT_PASS', log)
        self.assertNotIn('There was an error executing command', log)


@unittest.skipUnless(shutil.which('mcs') or os.environ.get('CSHARP_TEST_DOTNET_IMAGE'),
                     'Mono or an explicitly selected local .NET image required')
class BarometerInterruptModel(unittest.TestCase):
    def test_non_latched_pulse_status_clear_and_latched_mode(self):
        source = (ROOT / 'renode/peripherals/SedsSpiSensors.cs').read_text()
        model = source[source.index('    public sealed class SedsFlightSensorBus'):
                       source.index('    public sealed class SedsNeoM9N')]
        code = r'''
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.SPI;
using Antmicro.Renode.Peripherals.Timers;
namespace Antmicro.Renode.Core {public interface IMachine {object ClockSource{get;}}}
namespace Antmicro.Renode.Peripherals {
 public interface IGPIO {void Set(bool value);bool IsSet{get;}}
 public class GPIO:IGPIO {public bool IsSet{get;private set;} public void Set(bool value){IsSet=value;}}
 public interface IGPIOReceiver {void OnGPIO(int pin,bool value);}
 public interface INumberedGPIOOutput {IReadOnlyDictionary<int,IGPIO> Connections{get;}}
}
namespace Antmicro.Renode.Peripherals.SPI {public interface ISPIPeripheral {byte Transmit(byte value);void FinishTransmission();void Reset();}}
namespace Antmicro.Renode.Time {public enum WorkMode {Periodic,OneShot} public enum Direction {Ascending,Descending}}
namespace Antmicro.Renode.Peripherals.Timers {
 public class LimitTimer {
  public static Dictionary<string,LimitTimer> All=new Dictionary<string,LimitTimer>();
  public LimitTimer(object clock,long frequency,object owner,string name,ulong limit,bool enabled,bool eventEnabled,Antmicro.Renode.Time.WorkMode workMode=Antmicro.Renode.Time.WorkMode.Periodic,Antmicro.Renode.Time.Direction direction=Antmicro.Renode.Time.Direction.Descending){
   All[name]=this;Limit=limit;Enabled=enabled;Frequency=frequency;Mode=workMode;
  }
  public event Action LimitReached;
  public ulong Limit{get;set;} public bool Enabled{get;set;} public long Frequency;
  public Antmicro.Renode.Time.WorkMode Mode;
  public void Reset(){Enabled=false;}
  public void Fire(){if(!Enabled)return;if(Mode==Antmicro.Renode.Time.WorkMode.OneShot)Enabled=false;LimitReached();}
 }
}
namespace Antmicro.Renode.Peripherals.Sensors {
''' + model + r'''
}
class Machine:IMachine {public object ClockSource{get{return this;}}}
class Program {
 static void Check(bool value,string reason){if(!value)throw new Exception(reason);}
 static void Write(Antmicro.Renode.Peripherals.Sensors.SedsFlightSensorBus bus,byte reg,byte value){bus.OnGPIO(0,false);bus.Transmit(reg);bus.Transmit(value);bus.OnGPIO(0,true);}
 static byte Read(Antmicro.Renode.Peripherals.Sensors.SedsFlightSensorBus bus,byte reg){bus.OnGPIO(0,false);bus.Transmit((byte)(reg|128));bus.Transmit(0);var value=bus.Transmit(0);bus.OnGPIO(0,true);return value;}
 static void Main(){
  var bus=new Antmicro.Renode.Peripherals.Sensors.SedsFlightSensorBus(new Machine());
  Write(bus,0x19,0x42);Write(bus,0x1b,0x33);
  var conversion=LimitTimer.All["conversion0"];
  conversion.Fire();Check(bus.Connections[0].IsSet,"conversion must assert DRDY");
  var pulse=LimitTimer.All["barometerDrdyPulse"];
  Check(pulse.Enabled && pulse.Frequency==1000000 && pulse.Limit==2500,"non-latched DRDY must last 2.5ms virtual time");
  pulse.Fire();Check(!bus.Connections[0].IsSet,"pulse must expire without any SPI read");
  Check(Read(bus,0x11)==8 && Read(bus,0x11)==0,"INT_STATUS must retain and clear DRDY on read");
  conversion.Fire();Check(bus.Connections[0].IsSet,"unread previous conversion must not suppress the next edge");
  Read(bus,4);Check(bus.Connections[0].IsSet,"data read must not substitute for interrupt pulse timing");
  Read(bus,0x11);Check(!bus.Connections[0].IsSet && !pulse.Enabled,"INT_STATUS read must deassert early");
  Write(bus,0x19,0x46);conversion.Fire();
  Check(bus.Connections[0].IsSet && !pulse.Enabled,"latched mode must not start the non-latched timer");
  Read(bus,4);Check(bus.Connections[0].IsSet,"latched DRDY survives data-only reads");
  Read(bus,0x11);Check(!bus.Connections[0].IsSet,"latched status clears on INT_STATUS read");
  bus.Reset();Check(!bus.Connections[0].IsSet && !pulse.Enabled,"reset must cancel pulse");
  Console.WriteLine("PASS BMP390 non-latched pulse, repeated edges, read-clear status and latched mode");
 }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test.cs').write_text(code)
            image = os.environ.get('CSHARP_TEST_DOTNET_IMAGE')
            if image:
                (root / 'contract.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>')
                result = subprocess.run(['docker', 'run', '--rm', '--network=none',
                    '--user', f'{os.getuid()}:{os.getgid()}', '--env', 'HOME=/tmp',
                    '--env', 'DOTNET_CLI_HOME=/tmp', '--mount', f'type=bind,source={root},target=/fixture',
                    '--workdir', '/fixture', '--entrypoint', 'dotnet', image,
                    'run', '--project', 'contract.csproj'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            else:
                subprocess.run(['mcs', '-out:' + str(root / 'test.exe'), str(root / 'test.cs')], check=True)
                subprocess.run(['mono', str(root / 'test.exe')], check=True)
