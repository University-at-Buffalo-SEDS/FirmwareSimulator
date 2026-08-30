using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Storage
{
    // STM32 SDMMC v2 receive/data-path model. It implements card discovery,
    // single/multiple-block reads, FIFO status, IDMA writes, interrupts, and
    // deterministic removal/failure injection.
    public sealed class SedsStm32Sdmmc : IDoubleWordPeripheral, IKnownSize
    {
        public SedsStm32Sdmmc(IMachine machine, ulong failureEvery = 0,
            ulong disconnectAfter = ulong.MaxValue)
        {
            this.machine = machine;
            FailureEvery = failureEvery;
            DisconnectAfter = disconnectAfter;
            IRQ = new GPIO();
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case 0x14: return response[0];
            case 0x18: return response[1];
            case 0x1c: return response[2];
            case 0x20: return response[3];
            case 0x30: return remainingData;
            case 0x34: return StatusValue;
            case 0x80: return ReadFifoWord();
            default:
                uint value;
                return registers.TryGetValue(offset, out value) ? value : 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case 0x08: argument = value; break;
            case 0x0c:
                registers[offset] = value;
                if((value & (1u << 10)) != 0) ExecuteCommand(value & 0x3f);
                break;
            case 0x28: dataLength = value; registers[offset] = value; break;
            case 0x2c: dataControl = value; registers[offset] = value; break;
            case 0x38: status &= ~value; break;
            case 0x3c: interruptMask = value; registers[offset] = value; break;
            case 0x50: idmaControl = value; registers[offset] = value; break;
            case 0x54: idmaBufferSize = value; registers[offset] = value; break;
            case 0x58: idmaBase = value; registers[offset] = value; break;
            case 0x5c: idmaBase1 = value; registers[offset] = value; break;
            default: registers[offset] = value; break;
            }
            UpdateInterrupt();
        }

        public void LoadCardBytes(byte[] bytes)
        {
            if(bytes == null || bytes.Length == 0) throw new ArgumentException("SD card image cannot be empty");
            card = new byte[((bytes.Length + BlockSize - 1) / BlockSize) * BlockSize];
            Array.Copy(bytes, card, bytes.Length);
            cardPresent = true;
            highCapacity = true;
            selected = false;
            applicationCommand = false;
        }

        public void BeginCardImage()
        {
            cardBuilder.Clear();
            EjectCard();
        }

        public void AppendCardBytes(byte[] bytes)
        {
            if(bytes == null) throw new ArgumentNullException("bytes");
            cardBuilder.AddRange(bytes);
        }

        public void MountCardImage()
        {
            LoadCardBytes(cardBuilder.ToArray());
            cardBuilder.Clear();
        }

        public void EjectCard()
        {
            cardPresent = false;
            selected = false;
            fifo.Clear();
            remainingData = 0;
        }

        public bool GetCardPresent() { return cardPresent; }
        public ulong GetCommandsExecuted() { return commands; }
        public ulong GetBytesRead() { return bytesRead; }
        public GPIO IRQ { get; private set; }
        public ulong FailureEvery { get; set; }
        public ulong DisconnectAfter { get; set; }
        public long Size { get { return 0x400; } }

        public void Reset()
        {
            registers.Clear();
            fifo.Clear();
            Array.Clear(response, 0, response.Length);
            argument = 0;
            status = 0;
            interruptMask = 0;
            dataLength = 0;
            dataControl = 0;
            remainingData = 0;
            idmaControl = 0;
            idmaBufferSize = 0;
            idmaBase = 0;
            idmaBase1 = 0;
            commands = 0;
            bytesRead = 0;
            selected = false;
            applicationCommand = false;
            IRQ.Set(false);
        }

        private void ExecuteCommand(uint index)
        {
            commands++;
            status &= ~(CommandSent | CommandResponse | CommandTimeout | DataEnd | DataBlockEnd);
            if(commands > DisconnectAfter) cardPresent = false;
            if(!cardPresent || (FailureEvery != 0 && commands % FailureEvery == 0))
            {
                status |= index == 0 ? CommandSent : CommandTimeout;
                UpdateInterrupt();
                return;
            }

            switch(index)
            {
            case 0: // GO_IDLE_STATE
                selected = false;
                applicationCommand = false;
                status |= CommandSent;
                break;
            case 2: // ALL_SEND_CID
                response[0] = 0x53454453; response[1] = 0x53494d31;
                response[2] = 0x01000000; response[3] = 0x12345678;
                status |= CommandResponse;
                break;
            case 3: // SEND_RELATIVE_ADDR
                response[0] = RelativeCardAddress << 16;
                status |= CommandResponse;
                break;
            case 7: // SELECT/DESELECT_CARD
                selected = (argument >> 16) == RelativeCardAddress;
                response[0] = selected ? 0x700u : 0u;
                status |= CommandResponse;
                break;
            case 8: // SEND_IF_COND
                response[0] = 0x1aa;
                status |= CommandResponse;
                break;
            case 9: // SEND_CSD
                response[0] = 0x400e0032; response[1] = 0x5b590000;
                response[2] = 0x7f800a40; response[3] = 0x00400000;
                status |= CommandResponse;
                break;
            case 12: // STOP_TRANSMISSION
                fifo.Clear(); remainingData = 0;
                status |= CommandResponse | DataEnd;
                break;
            case 16: // SET_BLOCKLEN
                status |= argument == BlockSize ? CommandResponse : DataTimeout;
                break;
            case 17: // READ_SINGLE_BLOCK
                status |= CommandResponse;
                PrepareRead(argument, 1);
                break;
            case 18: // READ_MULTIPLE_BLOCK
                status |= CommandResponse;
                PrepareRead(argument, Math.Max(1u, dataLength / BlockSize));
                break;
            case 55: // APP_CMD
                applicationCommand = true;
                response[0] = 1u << 5;
                status |= CommandResponse;
                break;
            case 41: // SD_SEND_OP_COND (ACMD41)
                if(!applicationCommand) { status |= CommandTimeout; break; }
                response[0] = 0xc0ff8000;
                applicationCommand = false;
                status |= CommandResponse;
                break;
            default:
                response[0] = 0;
                status |= CommandResponse;
                break;
            }
            UpdateInterrupt();
        }

        private void PrepareRead(uint cardAddress, uint blocks)
        {
            if(!selected && cardAddress != 0) { status |= DataTimeout; return; }
            var byteOffset = highCapacity ? (ulong)cardAddress * BlockSize : cardAddress;
            var requested = Math.Min((ulong)blocks * BlockSize, dataLength == 0 ? (ulong)blocks * BlockSize : dataLength);
            if(byteOffset >= (ulong)card.Length || byteOffset + requested > (ulong)card.Length)
            {
                status |= DataTimeout;
                return;
            }
            var transfer = new byte[checked((int)requested)];
            Array.Copy(card, checked((long)byteOffset), transfer, 0, transfer.Length);
            remainingData = (uint)requested;
            if((idmaControl & 1) != 0 && idmaBase != 0)
            {
                var bus = machine.GetSystemBus(this);
                for(var i = 0; i < transfer.Length; i++) bus.WriteByte(idmaBase + (ulong)i, transfer[i], this);
                remainingData = 0;
                bytesRead += (ulong)transfer.Length;
                status |= DataEnd | DataBlockEnd;
            }
            else
            {
                foreach(var value in transfer) fifo.Enqueue(value);
            }
        }

        private uint ReadFifoWord()
        {
            uint value = 0;
            var consumed = 0;
            for(var i = 0; i < 4 && fifo.Count > 0; i++)
            {
                value |= (uint)fifo.Dequeue() << (8 * i);
                consumed++;
            }
            remainingData -= (uint)consumed;
            bytesRead += (uint)consumed;
            if(fifo.Count == 0)
            {
                status |= DataEnd | DataBlockEnd;
                remainingData = 0;
            }
            UpdateInterrupt();
            return value;
        }

        private uint StatusValue
        {
            get
            {
                var value = status;
                if(fifo.Count > 0) value |= ReceiveDataAvailable;
                if(fifo.Count >= 32) value |= ReceiveFifoHalfFull;
                return value;
            }
        }

        private void UpdateInterrupt() { IRQ.Set((StatusValue & interruptMask) != 0); }

        private readonly IMachine machine;
        private readonly Dictionary<long, uint> registers = new Dictionary<long, uint>();
        private readonly Queue<byte> fifo = new Queue<byte>();
        private readonly List<byte> cardBuilder = new List<byte>();
        private readonly uint[] response = new uint[4];
        private byte[] card = new byte[0];
        private bool cardPresent;
        private bool highCapacity;
        private bool selected;
        private bool applicationCommand;
        private uint argument;
        private uint status;
        private uint interruptMask;
        private uint dataLength;
        private uint dataControl;
        private uint remainingData;
        private uint idmaControl;
        private uint idmaBufferSize;
        private uint idmaBase;
        private uint idmaBase1;
        private ulong commands;
        private ulong bytesRead;

        private const uint BlockSize = 512;
        private const uint RelativeCardAddress = 1;
        private const uint CommandTimeout = 1u << 2;
        private const uint DataTimeout = 1u << 3;
        private const uint CommandResponse = 1u << 6;
        private const uint CommandSent = 1u << 7;
        private const uint DataEnd = 1u << 8;
        private const uint DataBlockEnd = 1u << 10;
        private const uint ReceiveFifoHalfFull = 1u << 15;
        private const uint ReceiveDataAvailable = 1u << 21;
    }
}
