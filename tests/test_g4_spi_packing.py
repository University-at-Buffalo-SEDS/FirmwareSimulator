"""Exercise the real G4 SPI model with Cube's packed five-byte transfer."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
@unittest.skipUnless(shutil.which('mcs') and shutil.which('mono'),'Mono compiler/runtime required')
class G4SpiPackingTests(unittest.TestCase):
    def test_hal_halfword_fifo_threshold_and_odd_tail(self):
        source=(ROOT/'renode/peripherals/SedsStm32G4Spi.cs').read_text()
        stubs=r'''
using System;
namespace Antmicro.Renode.Core {
 public interface IMachine {}
 public class GPIO { public bool State;public void Set(bool b){State=b;}public void Unset(){State=false;}public void Blink(){} }
}
namespace Antmicro.Renode.Core.Structure {
 public class NullRegistrationPointPeripheralContainer<T> {
  public NullRegistrationPointPeripheralContainer(Antmicro.Renode.Core.IMachine machine){}
  public T RegisteredPeripheral {get;set;}public virtual void Reset(){}
 }
}
namespace Antmicro.Renode.Peripherals.Bus {
 public interface IKnownSize {long Size{get;}}
 public interface IDoubleWordPeripheral {uint ReadDoubleWord(long o);void WriteDoubleWord(long o,uint v);}
 public interface IWordPeripheral {ushort ReadWord(long o);void WriteWord(long o,ushort v);}
 public interface IBytePeripheral {byte ReadByte(long o);void WriteByte(long o,byte v);}
}
namespace Antmicro.Renode.Peripherals.SPI {
 public interface ISPIPeripheral {byte Transmit(byte b);}
}
class Wire:Antmicro.Renode.Peripherals.SPI.ISPIPeripheral {
 public int Count;public byte Transmit(byte b){if(b!=255)throw new Exception("clock bytes");return (byte)++Count;}
}
class Program {
 static void Require(bool b){if(!b)throw new Exception("G4 FIFO packing mismatch");}
 static void Main(){
  var spi=new Antmicro.Renode.Peripherals.SPI.SedsStm32G4Spi(null);var wire=new Wire();spi.RegisteredPeripheral=wire;
  spi.WriteDoubleWord(0,64);spi.WriteDoubleWord(4,7u<<8);
  // Cube clocks two bytes with a DR register store, reads them together,
  // and changes FRXTH to byte mode for the final odd byte.
  spi.WriteDoubleWord(12,65535);Require((spi.ReadDoubleWord(8)&1)!=0);
  Require(spi.ReadWord(12)==0x0201);Require((spi.ReadDoubleWord(8)&1)==0);
  spi.WriteWord(12,65535);Require(spi.ReadDoubleWord(12)==0x0403);
  spi.WriteDoubleWord(4,(7u<<8)|0x1000u);spi.WriteByte(12,255);
  Require((spi.ReadDoubleWord(8)&1)!=0);Require(spi.ReadByte(12)==5);Require(wire.Count==5);
  // A byte access under the halfword threshold must not assert RXNE.
  spi.WriteDoubleWord(4,7u<<8);spi.WriteByte(12,255);Require((spi.ReadDoubleWord(8)&1)==0);
  spi.WriteByte(12,255);Require((spi.ReadDoubleWord(8)&1)!=0);Require(spi.ReadWord(12)==0x0706);
  for(var i=0;i<5;i++)spi.WriteByte(12,255);
  Require((spi.ReadDoubleWord(8)&64)!=0);Require((spi.ReadDoubleWord(8)&0x600)==0x600);
  spi.Reset();Require((spi.ReadDoubleWord(8)&(0x600|64|1))==0);
  spi.WriteByte(12,255);Require(wire.Count==12); // disabled controller does not clock the peripheral
  Console.WriteLine("PASS: real G4 model, packed HAL block, odd tail, RXNE threshold, bounded FIFO and reset");
 }
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            # Keep model using directives before the stub type declarations.
            path=Path(tmp)/'spi.cs';path.write_text('using System;\n'+source+stubs.replace('using System;','',1));exe=Path(tmp)/'spi.exe'
            subprocess.run(['mcs','-out:'+str(exe),str(path)],check=True)
            subprocess.run(['mono',str(exe)],check=True)
