using System;
using Antmicro.Renode.Peripherals.Sensors;
class Program
{
    static byte Read(SedsLtc2990 chip,byte address) {chip.Write(new[]{address});return chip.Read(1)[0];}
    static void Write(SedsLtc2990 chip,byte[] bytes) {chip.Write(bytes);chip.FinishTransmission();}
    static void Require(bool value,string reason) {if(!value)throw new Exception(reason);}
    static void Main()
    {
        var split=new SedsLtc2990();
        split.Write(new byte[]{1});split.Write(new byte[]{0x1f});split.FinishTransmission();
        split.Write(new byte[]{2});split.Write(new byte[]{0});split.FinishTransmission();
        Require((Read(split,0)&0x3c)==0x3c,"HAL RELOAD split write triggers conversions");
        Require(Read(split,1)==0x1f,"repeated START preserves pointer and next transaction resets it");
        var chip=new SedsLtc2990(samples:"9830,9336,9175,9175");
        Require(Read(chip,0)==0,"reset has no completed conversion");
        Write(chip,new byte[]{1,0x1f});Write(chip,new byte[]{2,0});
        Require((Read(chip,0)&0x3c)==0x3c,"all four ready flags must be set");
        for(byte channel=0;channel<4;channel++)
        {
            var msb=Read(chip,(byte)(6+2*channel));var lsb=Read(chip,(byte)(7+2*channel));
            Require((msb&128)!=0,"valid flag");
            var raw=((msb<<8)|lsb)&0x7fff;
            Require(raw==new[]{9830,9336,9175,9175}[channel],"configured conversion");
            Require((Read(chip,0)&(1<<(2+channel)))==0,"MSB consumption clears only that ready bit");
        }
        Write(chip,new byte[]{2,0});Require((Read(chip,0)&0x3c)==0x3c,"trigger rearms conversion flags");
        chip.Reset();Require(Read(chip,0)==0,"reset clears readiness");
        Write(chip,new byte[]{1,0x1f});Write(chip,new byte[]{2,0});Require(Read(chip,8)==(0x80|(9336>>8)),"reset preserves configured conversion codes");
        var failed=new SedsLtc2990(failureEvery:1);
        Require(Read(failed,0)==0,"failed status cannot masquerade as all-ready");
        Require((Read(failed,6)&128)==0,"failed conversion cannot masquerade as valid");
        var current=new SedsLtc2990(samples:"25,0,32743,0");
        Write(current,new byte[]{1,0x1e});Write(current,new byte[]{2,0});
        Require((Read(current,0)&0x3c)==0x14,"differential mode exposes V1-V2 and V3-V4");
        Require(Read(current,6)==0x80 && Read(current,7)==25,"positive differential code");
        Require(Read(current,10)==0xff && Read(current,11)==0xe7,"negative differential code preserves sign");
        Console.WriteLine("PASS: LTC2990 four channels, readiness, signed codes, trigger/reset and invalid conversions");
    }
}
namespace Antmicro.Renode.Core { }
namespace Antmicro.Renode.Peripherals.I2C
{
    public interface II2CPeripheral { void Write(byte[] data); byte[] Read(int count); void FinishTransmission(); void Reset(); }
}
