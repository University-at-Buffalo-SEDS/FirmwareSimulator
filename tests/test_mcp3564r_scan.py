"""Run the actual Renode ADC model with minimal peripheral interface stubs."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('mcs') and shutil.which('mono'), 'Mono compiler/runtime required')
class Mcp3564rScanTests(unittest.TestCase):
    def test_legacy_and_tagged_two_channel_data(self):
        source = (ROOT / 'renode/peripherals/SedsSpiSensors.cs').read_text()
        model = source[source.index('    public sealed class SedsMcp3564r'):]
        harness = '''
using System;
using System.Collections.Generic;
namespace Antmicro.Renode.Peripherals.Sensors {
public interface ISPIPeripheral { byte Transmit(byte value); void FinishTransmission(); void Reset(); }
public interface IGPIOReceiver { void OnGPIO(int number, bool value); }
''' + model + '''
class Test {
    static Antmicro.Renode.Peripherals.Sensors.SedsMcp3564r adc =
        new Antmicro.Renode.Peripherals.Sensors.SedsMcp3564r(externalChipSelect: true);
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
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'adc.cs'
            path.write_text(harness)
            binary = Path(directory) / 'adc.exe'
            subprocess.run(['mcs', '-out:' + str(binary), str(path)], check=True, capture_output=True)
            subprocess.run(['mono', str(binary)], check=True, capture_output=True)
