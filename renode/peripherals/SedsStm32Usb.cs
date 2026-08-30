using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Memory;

namespace Antmicro.Renode.Peripherals.USB
{
    // USB receive-side model for the two controller generations used here:
    // STM32G4 USB device/PMA and STM32H5/U5 Synopsys OTG FS. It intentionally
    // models endpoint/FIFO state and interrupts, not a host-side USB stack.
    public sealed class SedsStm32Usb : IWordPeripheral, IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Usb(string variant, MappedMemory pma = null)
        {
            if(variant != "device_pma" && variant != "otg_fs") throw new ArgumentException("unknown STM32 USB variant");
            if(variant == "device_pma" && pma == null) throw new ArgumentException("device_pma requires packet memory");
            this.variant = variant;
            this.pma = pma;
            IRQ = new GPIO();
            Reset();
        }

        public ushort ReadWord(long offset)
        {
            if(variant != "device_pma") return (ushort)ReadDoubleWord(offset);
            if(offset >= 0 && offset < 0x20 && (offset & 3) == 0) return endpoints[offset / 4];
            switch(offset)
            {
            case 0x40: return control;
            case 0x44: return interruptStatus;
            case 0x48: return frameNumber++;
            case 0x4c: return deviceAddress;
            case 0x50: return bufferTable;
            case 0x54: return lpmControl;
            case 0x58: return batteryCharging;
            default: return 0;
            }
        }

        public void WriteWord(long offset, ushort value)
        {
            if(variant != "device_pma") { WriteDoubleWord(offset, value); return; }
            if(offset >= 0 && offset < 0x20 && (offset & 3) == 0)
            {
                var endpoint = offset / 4;
                // CTR bits clear when firmware writes zero; the remaining
                // endpoint configuration is retained exactly as written.
                var retained = (ushort)(endpoints[endpoint] & value & 0x8080);
                endpoints[endpoint] = (ushort)((value & 0x7f7f) | retained);
                if((endpoints[endpoint] & 0x8000) == 0) ClearPmaInterruptIfIdle();
            }
            else
            {
                switch(offset)
                {
                case 0x40: control = value; break;
                case 0x44: interruptStatus &= value; break;
                case 0x4c: deviceAddress = value; break;
                case 0x50: bufferTable = (ushort)(value & 0xfff8); break;
                case 0x54: lpmControl = value; break;
                case 0x58: batteryCharging = value; break;
                }
            }
            UpdateInterrupt();
        }

        public uint ReadDoubleWord(long offset)
        {
            if(variant == "device_pma") return ReadWord(offset);
            switch(offset)
            {
            case 0x008: return ahbConfiguration;
            case 0x00c: return usbConfiguration;
            case 0x010: return resetControl | (1u << 31);
            case 0x014: return globalInterruptStatus | (receiveBytes.Count > 0 ? 1u << 4 : 0);
            case 0x018: return globalInterruptMask;
            case 0x020:
                if(receivePackets.Count == 0) return 0;
                var length = receivePackets.Dequeue();
                return receiveEndpoint | ((uint)length << 4) | (2u << 17);
            default:
                if(offset >= 0x1000 && offset < 0x2000) return ReadReceiveFifo();
                uint value;
                return registers.TryGetValue(offset, out value) ? value : 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(variant == "device_pma") { WriteWord(offset, (ushort)value); return; }
            switch(offset)
            {
            case 0x008: ahbConfiguration = value; break;
            case 0x00c: usbConfiguration = value; break;
            case 0x010:
                // Core/RX/TX FIFO reset completes synchronously.
                resetControl = value & ~0x31u;
                if((value & 0x30) != 0) ClearReceiveFifo();
                break;
            case 0x014: globalInterruptStatus &= ~value; break;
            case 0x018: globalInterruptMask = value; break;
            default: registers[offset] = value; break;
            }
            UpdateInterrupt();
        }

        public void InjectPacket(byte[] packet) { InjectPacket(packet, 1); }
        public void InjectPacket(byte[] packet, uint endpoint)
        {
            if(packet == null || packet.Length == 0) throw new ArgumentException("USB packet cannot be empty");
            if(endpoint > 7) throw new ArgumentOutOfRangeException("endpoint");
            if(variant == "device_pma") InjectPmaPacket(packet, endpoint);
            else InjectOtgPacket(packet, endpoint);
            PacketsInjected++;
            BytesInjected += (ulong)packet.Length;
            UpdateInterrupt();
        }

        public ulong GetPacketsInjected() { return PacketsInjected; }
        public ulong GetBytesInjected() { return BytesInjected; }
        public GPIO IRQ { get; private set; }
        public long Size { get { return variant == "otg_fs" ? 0x20000 : 0x400; } }

        public void Reset()
        {
            Array.Clear(endpoints, 0, endpoints.Length);
            registers.Clear();
            receiveBytes.Clear();
            receivePackets.Clear();
            control = 0;
            interruptStatus = variant == "device_pma" ? (ushort)0x400 : (ushort)0;
            frameNumber = 0;
            deviceAddress = 0;
            bufferTable = 0;
            lpmControl = 0;
            batteryCharging = 0;
            ahbConfiguration = 0;
            usbConfiguration = 0;
            resetControl = 0;
            globalInterruptStatus = 0;
            globalInterruptMask = 0;
            receiveEndpoint = 0;
            IRQ.Set(false);
        }

        private void InjectPmaPacket(byte[] packet, uint endpoint)
        {
            var descriptor = bufferTable + endpoint * 8;
            var receiveAddress = pma.ReadWord(descriptor + 4);
            if(receiveAddress >= pma.Size || receiveAddress + packet.Length > pma.Size)
                throw new InvalidOperationException("USB PMA receive buffer is outside packet memory");
            pma.WriteBytes(receiveAddress, packet);
            pma.WriteWord(descriptor + 6, (ushort)packet.Length);
            endpoints[endpoint] |= 0x8000; // CTR_RX
            interruptStatus = (ushort)(0x8000 | 0x10 | endpoint);
        }

        private void InjectOtgPacket(byte[] packet, uint endpoint)
        {
            receiveEndpoint = endpoint;
            receivePackets.Enqueue(packet.Length);
            foreach(var value in packet) receiveBytes.Enqueue(value);
            while((receiveBytes.Count & 3) != 0) receiveBytes.Enqueue(0);
            globalInterruptStatus |= (1u << 4) | (1u << 19);
        }

        private uint ReadReceiveFifo()
        {
            uint value = 0;
            for(var i = 0; i < 4 && receiveBytes.Count > 0; i++) value |= (uint)receiveBytes.Dequeue() << (i * 8);
            if(receiveBytes.Count == 0) globalInterruptStatus &= ~(1u << 4);
            UpdateInterrupt();
            return value;
        }

        private void ClearReceiveFifo()
        {
            receiveBytes.Clear();
            receivePackets.Clear();
            globalInterruptStatus &= ~((1u << 4) | (1u << 19));
        }

        private void ClearPmaInterruptIfIdle()
        {
            for(var i = 0; i < endpoints.Length; i++) if((endpoints[i] & 0x8080) != 0) return;
            interruptStatus &= 0x7fff;
        }

        private void UpdateInterrupt()
        {
            if(variant == "device_pma") IRQ.Set((control & interruptStatus & 0xff80) != 0);
            else IRQ.Set((ahbConfiguration & 1) != 0 && (globalInterruptStatus & globalInterruptMask) != 0);
        }

        private readonly string variant;
        private readonly MappedMemory pma;
        private readonly ushort[] endpoints = new ushort[8];
        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
        private readonly Queue<byte> receiveBytes = new Queue<byte>();
        private readonly Queue<int> receivePackets = new Queue<int>();
        private ushort control;
        private ushort interruptStatus;
        private ushort frameNumber;
        private ushort deviceAddress;
        private ushort bufferTable;
        private ushort lpmControl;
        private ushort batteryCharging;
        private uint ahbConfiguration;
        private uint usbConfiguration;
        private uint resetControl;
        private uint globalInterruptStatus;
        private uint globalInterruptMask;
        private uint receiveEndpoint;
        private ulong PacketsInjected;
        private ulong BytesInjected;
    }
}
