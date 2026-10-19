using System;
using System.Collections.Generic;
using System.IO;
using System.Diagnostics;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Storage
{
    // STM32 SDMMC v2 receive/data-path model. It implements card discovery,
    // single/multiple-block FIFO and IDMA transfers, interrupts, and
    // deterministic removal/failure injection.
    public sealed class SedsStm32Sdmmc : IDoubleWordPeripheral, IKnownSize, IDisposable
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
            case 0x10: return responseCommand;
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
                // The command state machine finishes synchronously here.
                // CPSMEN clears on completion; subsequent CMDTRANS updates
                // must not issue the previous command a second time.
                registers[offset] = value & ~(1u << 12);
                // STM32 H5/U5 SDMMC v2 enables the command-path state machine
                // with CMD.CPSMEN bit 12 (older STM32 blocks used bit 10).
                if((value & (1u << 12)) != 0) ExecuteCommand(value & 0x3f);
                break;
            case 0x28: dataLength = value & 0x01ffffffu; registers[offset] = dataLength; break;
            case 0x2c: dataControl = value; registers[offset] = value; break;
            case 0x38: status &= ~value; break;
            case 0x3c: interruptMask = value; registers[offset] = value; break;
            case 0x50: idmaControl = value; registers[offset] = value; break;
            case 0x54: idmaBufferSize = value; registers[offset] = value; break;
            case 0x58: idmaBase = value; registers[offset] = value; break;
            case 0x5c: idmaBase1 = value; registers[offset] = value; break;
            case 0x80: WriteFifoWord(value); break;
            default: registers[offset] = value; break;
            }
            UpdateInterrupt();
        }

        public void LoadCardBytes(byte[] bytes)
        {
            if(bytes == null || bytes.Length == 0) throw new ArgumentException("SD card image cannot be empty");
            CreateCard(((ulong)bytes.Length + BlockSize - 1) / BlockSize * BlockSize);
            card.Write(bytes, 0, bytes.Length);
            MarkRange(0, (ulong)bytes.Length);
        }

        private void CreateCard(ulong capacity)
        {
            if(capacity < BlockSize || capacity % BlockSize != 0 || capacity > (1UL << 41))
                throw new ArgumentOutOfRangeException("capacity", "SDHC capacity must be a positive multiple of 512 bytes, at most 2 TiB");
            Dispose();
            backingPath = Path.Combine(Path.GetTempPath(), "seds-sd-" + Guid.NewGuid().ToString("N") + ".img");
            card = new FileStream(backingPath, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.Read);
            card.SetLength((long)capacity); // Sparse zero-filled image; no capacity-sized allocation.
            dirtyPages.Clear();
            cardPresent = true;
            highCapacity = true;
            selected = false;
            applicationCommand = false;
        }

        public void Dispose()
        {
            cardPresent = false; writing = false;
            if(card != null) { card.Dispose(); card = null; }
            if(backingPath != null) { File.Delete(backingPath); backingPath = null; }
        }

        private void MarkRange(ulong offset, ulong length)
        {
            if(length == 0) return;
            for(var page = offset / PageSize; page <= (offset + length - 1) / PageSize; page++)
                dirtyPages.Add((long)page);
        }

        // Explicit test-fixture preparation only. Blank-card and removal tests
        // retain their normal behavior unless the descriptor opts in.
        public string FilesystemFixture
        {
            get { return filesystemFixture; }
            set
            {
                var marker = ParseFixture(value);
                filesystemFixture = value;
                if(card != null && !String.IsNullOrEmpty(value)) PrepareFilesystem(marker);
            }
        }
        private static string ParseFixture(string fixture)
        {
            if(String.IsNullOrEmpty(fixture) || fixture == "fat32") return null;
            if(!fixture.StartsWith("fat32:")) throw new ArgumentException("unsupported SD filesystem fixture");
            var name = fixture.Substring(6);
            if(name.Length == 0 || name.Length > 12) throw new ArgumentException("invalid SD fixture marker name");
            foreach(var c in name)
                if(!(c >= 'A' && c <= 'Z') && !(c >= '0' && c <= '9') && c != '.' && c != '_' && c != '-')
                    throw new ArgumentException("invalid SD fixture marker name");
            return name;
        }
        private void RunFilesystemTool(string tool, string arguments)
        {
            card.Dispose(); card = null;
            try
            {
                var start = new ProcessStartInfo(tool, arguments) { UseShellExecute = false,
                    RedirectStandardOutput = true, RedirectStandardError = true };
                using(var process = Process.Start(start))
                {
                    var output = process.StandardOutput.ReadToEnd();
                    var error = process.StandardError.ReadToEnd();
                    process.WaitForExit();
                    if(process.ExitCode != 0) throw new InvalidOperationException(tool + ": " + output + error);
                }
            }
            finally { card = new FileStream(backingPath, FileMode.Open, FileAccess.ReadWrite, FileShare.Read); }
        }
        private static string QuotePath(string path)
        {
            return "\"" + path.Replace("\"", "\\\"") + "\"";
        }
        private void PrepareFilesystem(string marker)
        {
            // Paths are private GUID names and marker names are validated above;
            // no shell or caller-provided executable/arguments are accepted.
            RunFilesystemTool("mkfs.fat", "-F 32 -n SEDSSIM " + QuotePath(backingPath));
            if(marker != null)
            {
                var seed = Path.Combine(Path.GetTempPath(), "seds-marker-" + Guid.NewGuid().ToString("N"));
                try
                {
                    File.WriteAllText(seed, "SEDS simulated storage\n");
                    RunFilesystemTool("mcopy", "-i " + QuotePath(backingPath) + " " + QuotePath(seed) + " ::" + marker);
                }
                finally { File.Delete(seed); }
            }
            // mkfs/mcopy update reserved/FAT/root sectors outside the MMIO path.
            // Preserve their metadata and the single optional marker cluster in
            // sparse exports, along with all subsequent firmware writes.
            var boot = new byte[512]; ReadCard(0, boot);
            var reserved = BitConverter.ToUInt16(boot, 14);
            var fatSectors = BitConverter.ToUInt32(boot, 36);
            var metadataEnd = ((ulong)reserved + (ulong)boot[16] * fatSectors + 4UL * boot[13]) * BlockSize;
            MarkRange(0, Math.Min((ulong)card.Length, metadataEnd));
        }

        private void ReadCard(ulong offset, byte[] bytes)
        {
            card.Position = checked((long)offset);
            var read = 0;
            while(read < bytes.Length)
            {
                var count = card.Read(bytes, read, bytes.Length - read);
                if(count == 0) throw new EndOfStreamException("short SD backing read");
                read += count;
            }
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
            writing = false;
            cardPresent = false;
            selected = false;
            fifo.Clear();
            remainingData = 0;
        }

        public bool GetCardPresent() { return cardPresent; }
        public void SaveCardImage(string path)
        {
            // Preserve holes while retaining every firmware-written page. Never
            // replace an earlier diagnostic capture or allocate a full card copy.
            card.Flush();
            using(var output = new FileStream(path, FileMode.CreateNew))
            {
                output.SetLength(card.Length);
                var buffer = new byte[PageSize];
                foreach(var page in dirtyPages)
                {
                    var offset = page * PageSize;
                    var length = (int)Math.Min((long)PageSize, card.Length - offset);
                    card.Position = offset;
                    var read = 0;
                    while(read < length)
                    {
                        var count = card.Read(buffer, read, length - read);
                        if(count == 0) throw new EndOfStreamException("short SD export read");
                        read += count;
                    }
                    output.Position = offset; output.Write(buffer, 0, length);
                }
            }
        }
        public ulong GetCommandsExecuted() { return commands; }
        public ulong GetBytesRead() { return bytesRead; }
        public ulong GetBytesWritten() { return bytesWritten; }
        public ulong CardCapacityBytes
        {
            get { return card == null ? 0 : (ulong)card.Length; }
            set
            {
                CreateCard(value);
                if(!String.IsNullOrEmpty(filesystemFixture)) PrepareFilesystem(ParseFixture(filesystemFixture));
            }
        }
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
            responseCommand = 0;
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
            bytesWritten = 0;
            selected = false;
            applicationCommand = false;
            IRQ.Set(false);
            writing = false;
        }

        private void ExecuteCommand(uint index)
        {
            commands++;
            responseCommand = index;
            Array.Clear(response, 0, response.Length);
            status &= ~(CommandSent | CommandResponse | CommandTimeout | DataTimeout | DataEnd | DataBlockEnd);
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
                var capacity = (uint)Math.Max(1, card.Length / (512 * 1024)) - 1;
                response[1] |= (capacity >> 16) & 0x3f;
                response[2] = (capacity << 16) | 0x0a40; response[3] = 0x00400000;
                status |= CommandResponse;
                break;
            case 12: // STOP_TRANSMISSION
                fifo.Clear(); remainingData = 0;
                status |= CommandResponse | DataEnd;
                break;
            case 16: // SET_BLOCKLEN
                status |= argument > 0 && argument <= BlockSize ? CommandResponse : CommandTimeout;
                break;
            case 17: // READ_SINGLE_BLOCK
                status |= CommandResponse;
                PrepareRead(argument, 1);
                break;
            case 18: // READ_MULTIPLE_BLOCK
                status |= CommandResponse;
                PrepareRead(argument, Math.Max(1u, dataLength / BlockSize));
                break;
            case 24: // WRITE_SINGLE_BLOCK
            case 25: // WRITE_MULTIPLE_BLOCK
                status |= CommandResponse;
                writeOffset = highCapacity ? (ulong)argument * BlockSize : argument;
                remainingData = index == 24 ? BlockSize : dataLength;
                writing = selected && remainingData != 0 && writeOffset + remainingData <= (ulong)card.Length;
                if(!writing) { remainingData = 0; status |= DataTimeout; }
                else
                {
                    card.Position = (long)writeOffset; MarkRange(writeOffset, remainingData);
                    if((idmaControl & 1) != 0 && idmaBase != 0) WriteIdmaData();
                }
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
            case 13: // SEND_STATUS or SD_STATUS (ACMD13)
                status |= CommandResponse;
                if(applicationCommand)
                {
                    response[0] = 0;
                    applicationCommand = false;
                    PrepareData(new byte[64]);
                }
                else response[0] = 4u << 9; // TRANSFER state
                break;
            case 51: // SEND_SCR (ACMD51)
                if(!applicationCommand) { status |= CommandTimeout; break; }
                response[0] = 0;
                applicationCommand = false;
                status |= CommandResponse;
                // SCR.SD_BUS_WIDTHS advertises the mandatory 1-bit mode and
                // the commonly supported 4-bit mode. The HAL byte-swaps the
                // two FIFO words before testing bits 16 and 18.
                PrepareData(new byte[] { 0x00, 0x05, 0x00, 0x00, 0, 0, 0, 0 });
                break;
            case 6: // SWITCH_FUNC or SET_BUS_WIDTH (ACMD6)
                response[0] = 0;
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
            if(requested > int.MaxValue) { status |= DataTimeout; return; }
            var transfer = new byte[(int)requested];
            ReadCard(byteOffset, transfer);
            PrepareData(transfer);
        }

        private void PrepareData(byte[] transfer)
        {
            remainingData = (uint)transfer.Length;
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

        private void WriteIdmaData()
        {
            var bus = machine.GetSystemBus(this);
            // Match the deployed HAL's single-buffer IDMA path without a
            // transfer-sized host allocation. DATAEND remains pending until
            // the HAL enables its mask after submitting CMD24/CMD25.
            var scratch = new byte[4096];
            ulong source = idmaBase;
            while(remainingData != 0)
            {
                var count = (int)Math.Min((uint)scratch.Length, remainingData);
                for(var i = 0; i < count; i++) scratch[i] = bus.ReadByte(source + (ulong)i, this);
                card.Write(scratch, 0, count);
                source += (uint)count; writeOffset += (uint)count;
                bytesWritten += (uint)count; remainingData -= (uint)count;
            }
            writing = false;
            status |= DataEnd | DataBlockEnd;
        }

        private void WriteFifoWord(uint value)
        {
            if(!writing) return;
            for(var i = 0; i < 4 && remainingData != 0; i++)
            {
                card.WriteByte((byte)(value >> (8 * i)));
                writeOffset++; bytesWritten++;
                remainingData--;
            }
            if(remainingData == 0)
            {
                writing = false;
                status |= DataEnd | DataBlockEnd;
            }
            UpdateInterrupt();
        }

        private uint StatusValue
        {
            get
            {
                var value = status;
                if(writing) value |= 1u << 14; // TXFIFOHE
                if(fifo.Count == 0) value |= 1u << 19; // RXFIFOE
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
        private FileStream card;
        private string backingPath;
        private string filesystemFixture;
        private const int PageSize = 65536;
        private readonly SortedSet<long> dirtyPages = new SortedSet<long>();
        private bool cardPresent;
        private bool highCapacity;
        private bool selected;
        private bool applicationCommand;
        private bool writing;
        private ulong writeOffset;
        private uint argument;
        private uint responseCommand;
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
        private ulong bytesWritten;

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
