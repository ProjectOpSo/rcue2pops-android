#!/usr/bin/env python3

import argparse
import errno
import os
import re
import signal
import sys
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

# CRITICAL CONSTANTS - DO NOT ALTER
SECTORSIZE = 2352
VCD_HEADER_SIZE = 0x100000  # 1 MiB
IO_BUFFER_SIZE = 0x40000   # 256 KiB buffer for Android/Termux

# User interrupt signal flag (SIGINT/SIGTERM)
g_interrupted = False


def handle_signal(sig, frame):
    """Signal handler for graceful shutdown."""
    global g_interrupted
    g_interrupted = True


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


@dataclass
class Parameters:
    """Structure equivalent to parameters in cue2pops.c"""
    vmode: bool = False
    trainer: bool = False
    gap_more: bool = False
    gap_less: bool = False
    debug_cue: bool = False
    force_overwrite: bool = False
    no_sync: bool = False
    debug_validate: bool = False

    deny_vmode: int = 0
    fix_game: int = 0
    game_has_cheats: int = 0
    game_title: int = 0
    game_trained: int = 0
    game_fixed: int = 0


def game_identifier(inbuf: memoryview, p: Parameters):
    """Searches for specific title signatures in the executable within the 1 MiB window."""
    if p.game_title == 0:
        buf_len = len(inbuf)
        limit = min(buf_len, VCD_HEADER_SIZE)
        for ptr in range(0, limit - 16, 4):
            # Crash Bandicoot [SCES-00344]
            if inbuf[ptr:ptr+16] == b"SCES-00344      ":
                print("-" * 82)
                print("Crash Bandicoot [SCES-00344]")
                p.deny_vmode += 1
                p.game_title = 1
                p.game_has_cheats = 1
                p.fix_game = 0
                break
            # Crash Bandicoot [SCUS-94900]
            if inbuf[ptr:ptr+16] == b"SCUS-94900      ":
                print("-" * 82)
                print("Crash Bandicoot [SCUS-94900]")
                p.game_title = 2
                p.game_has_cheats = 1
                p.fix_game = 0
                break
            # Crash Bandicoot [SCPS-10031]
            if inbuf[ptr:ptr+16] == b"SCPS-10031      ":
                print("-" * 82)
                print("Crash Bandicoot [SCPS-10031]")
                p.game_title = 3
                p.game_has_cheats = 1
                p.fix_game = 0
                break
            # Metal Gear Solid : Special Missions [SLES-02136]
            if ptr <= limit - 18 and inbuf[ptr:ptr+18] == b" 1999081614163300$":
                print("-" * 82)
                print("Metal Gear Solid : Special Missions [SLES-02136]")
                p.game_title = 4
                p.game_has_cheats = 0
                p.fix_game = 1
                break

    if p.game_title != 0 and p.fix_game == 1:
        print("GameFixer is ON")
    if p.game_title != 0 and p.trainer and p.game_has_cheats == 0:
        print("There is no cheat for this title")
    if p.game_title != 0 and p.deny_vmode != 0 and p.vmode:
        print("VMODE patching is disabled for this title")
    if p.game_title != 0:
        print("-" * 82)


def game_fixer(inbuf: memoryview, p: Parameters):
    """Applies specific binary fixes (e.g., disc swap in Metal Gear Solid)."""
    if p.game_fixed == 0:
        buf_len = len(inbuf)
        limit = min(buf_len, VCD_HEADER_SIZE)
        for ptr in range(0, limit - 4, 4):
            if p.game_title == 4:
                if inbuf[ptr:ptr+4] == b"\x78\x26\x43\x8c":
                    inbuf[ptr] = 0x74
                if inbuf[ptr:ptr+4] == b"\xe8\x75\x06\x80":
                    if ptr >= 8:
                        inbuf[ptr - 8] = 0x07
                        print("game_fixer : Disc Swap Patched")
                        p.game_fixed = 1
                        print("-" * 82)
                        break


def game_trainer(inbuf: memoryview, p: Parameters):
    """Applies cheats/trainers directly into game data."""
    if p.game_trained == 0:
        buf_len = len(inbuf)
        limit = min(buf_len, VCD_HEADER_SIZE)
        for ptr in range(0, limit - 4, 4):
            if p.game_title == 1 and inbuf[ptr:ptr+4] == b"\x7c\x16\x20\xac":
                inbuf[ptr+2] = 0x22
                print("game_trainer : Test Save System Enabled")
                p.game_trained = 1
                print("-" * 82)
                break
            elif p.game_title == 2 and inbuf[ptr:ptr+4] == b"\x9c\x19\x20\xac":
                inbuf[ptr+2] = 0x22
                print("game_trainer : Test Save System Enabled")
                p.game_trained = 1
                print("-" * 82)
                break
            elif p.game_title == 3 and inbuf[ptr:ptr+4] == b"\x84\x19\x20\xac":
                inbuf[ptr+2] = 0x22
                print("game_trainer : Test Save System Enabled")
                p.game_trained = 1
                print("-" * 82)
                break


def ntsc_patcher(inbuf: memoryview, tracker: int, p: Parameters):
    """Searches for and applies NTSC video mode patches and Y-Pos alignment."""
    buf_len = len(inbuf)
    limit = min(buf_len, VCD_HEADER_SIZE)
    for i in range(0, limit - 28, 4):
        # Y-Pos Pattern
        if (inbuf[i] == 0x13 and inbuf[i+1] == 0x00 and (inbuf[i+2] in (0x90, 0x91)) and inbuf[i+3] == 0x24) and \
           (inbuf[i+4] == 0x10 and inbuf[i+5] == 0x00 and (inbuf[i+6] in (0x90, 0x91)) and inbuf[i+7] == 0x24):
            print(f"Y-Pos pattern found at dump offset 0x{tracker + i:X} / LBA {(tracker + i) // SECTORSIZE} (VCD offset 0x{VCD_HEADER_SIZE + tracker + i:X})")
            inbuf[i] = 0xF8
            inbuf[i+1] = 0xFF
            inbuf[i+4] = 0xF8
            inbuf[i+5] = 0xFF
            print("-" * 82)
        # VMODE Pattern
        elif inbuf[i+2] != 0xBD and inbuf[i+3] != 0x27 and inbuf[i+4] == 0x08 and inbuf[i+5] == 0x00 and \
             inbuf[i+6] == 0xE0 and inbuf[i+7] == 0x03 and inbuf[i+14] == 0x02 and inbuf[i+15] == 0x3C and \
             inbuf[i+18] == 0x42 and inbuf[i+19] == 0x8C and inbuf[i+20] == 0x08 and inbuf[i+21] == 0x00 and \
             inbuf[i+22] == 0xE0 and inbuf[i+23] == 0x03 and inbuf[i+24] == 0x00 and inbuf[i+25] == 0x00 and \
             inbuf[i+26] == 0x00 and inbuf[i+27] == 0x00 and \
             ((inbuf[i+2] == 0x24 and inbuf[i+3] == 0xAC) or (inbuf[i+6] == 0x24 and inbuf[i+7] == 0xAC) or (inbuf[i+10] == 0x24 and inbuf[i+11] == 0xAC)):
            if p.deny_vmode != 0:
                print(f"Skipped VMODE pattern at dump offset 0x{tracker + i:X} / LBA {(tracker + i) // SECTORSIZE} (VCD offset 0x{VCD_HEADER_SIZE + tracker + i:X})")
            else:
                print(f"VMODE pattern found at dump offset 0x{tracker + i:X} / LBA {(tracker + i) // SECTORSIZE} (VCD offset 0x{VCD_HEADER_SIZE + tracker + i:X})")
                inbuf[i+12:i+18] = b"\x00" * 6
                inbuf[i+18] = 0x02
                inbuf[i+19] = 0x24
                if inbuf[i+2] == 0x24 and inbuf[i+3] == 0xAC:
                    inbuf[i+2] = 0x20
                elif inbuf[i+6] == 0x24 and inbuf[i+7] == 0xAC:
                    inbuf[i+6] = 0x20
                elif inbuf[i+10] == 0x24 and inbuf[i+11] == 0xAC:
                    inbuf[i+10] = 0x20
            print("-" * 82)


def get_file_size(path: Path) -> int:
    """Returns the file size in bytes."""
    try:
        return path.stat().st_size
    except Exception as e:
        sys.stderr.write(f"Error: Cannot get size of {path}: {e}\n")
        return -1


def is_cue(file_name: str) -> bool:
    """Validates existence and extension of the .cue file."""
    if file_name.lower().endswith(".cue"):
        return Path(file_name).exists()
    return Path(file_name).exists()


def convert_file_ending_to_vcd(file_name: str) -> str:
    """Generates the VCD filename based on the CUE filename."""
    p = Path(file_name)
    if p.suffix.lower() == ".cue":
        return str(p.with_suffix(".VCD"))
    return file_name + ".VCD"


def get_lead_out(hbuf: bytearray, b_size: int, pregap_count: int, postgap_count: int) -> int:
    """Calculates Lead-Out MSF based on BIN file size."""
    sector_count = (b_size // SECTORSIZE) + (150 * (pregap_count + postgap_count)) + 150
    leadoutM = sector_count // 4500
    leadoutS = (sector_count - leadoutM * 4500) // 75
    leadoutF = sector_count - leadoutM * 4500 - leadoutS * 75

    sector_count = (b_size // SECTORSIZE) + (150 * (pregap_count + postgap_count))

    leadout_str = f"{leadoutM:02d}{leadoutS:02d}{leadoutF:02d}"
    hbuf[27] = ((ord(leadout_str[0]) - 48) * 16) + (ord(leadout_str[1]) - 48)
    hbuf[28] = ((ord(leadout_str[2]) - 48) * 16) + (ord(leadout_str[3]) - 48)
    hbuf[29] = ((ord(leadout_str[4]) - 48) * 16) + (ord(leadout_str[5]) - 48)

    return sector_count


def parse_cli_args() -> tuple[Optional[str], Optional[str], Parameters]:
    """Custom parser to preserve the exact CLI behavior of cue2pops.c."""
    params = Parameters()
    raw_args = sys.argv[1:]

    if not raw_args:
        return None, None, params

    positional = []
    for arg in raw_args:
        if arg == "gap++":
            params.gap_more = True
        elif arg == "gap--":
            params.gap_less = True
        elif arg == "vmode":
            params.vmode = True
        elif arg == "trainer":
            params.trainer = True
        elif arg == "--debug-cue":
            params.debug_cue = True
        elif arg in ("-f", "--force"):
            params.force_overwrite = True
        elif arg == "--no-sync":
            params.no_sync = True
        elif arg == "--debug-validate":
            params.debug_validate = True
        else:
            positional.append(arg)

    cue_path = positional[0] if len(positional) > 0 else None
    vcd_path = positional[1] if len(positional) > 1 else None

    return cue_path, vcd_path, params


def main():
    cue_name, vcd_name, params = parse_cli_args()

    if not cue_name:
        print("\nBIN/CUE to IMAGE0.VCD conversion tool v2.0")
        print("Usage :")
        print(f"{sys.argv[0]} input.cue <cmd_1> <cmd_2> <cmd_3> <output.vcd>\n")
        print("Commands are :")
        print("gap++ : Adds 2 seconds to all track indexes MSF")
        print("gap-- : Substracts 2 seconds to all track indexes MSF")
        print("vmode : Attempts to patch the video mode to NTSC and to fix the screen position")
        print("trainer : Enable cheats\n")
        return

    if not is_cue(cue_name):
        print(f"input .cue file: {cue_name} did not exist")
        return

    if vcd_name is None:
        vcd_name = convert_file_ending_to_vcd(cue_name)

    if params.gap_more and params.gap_less:
        print("Syntax Error : Conflicting gap++/gap-- arguments.\n")
        return

    cue_path = Path(cue_name)
    cue_size = get_file_size(cue_path)
    if cue_size < 0:
        return

    try:
        with open(cue_path, "rb") as f:
            cue_bytes = f.read()
    except Exception as e:
        print(f"Failed to open cuefile {cue_name}, error {e}")
        return

    try:
        cue_text = cue_bytes.decode("latin-1")
    except Exception:
        cue_text = cue_bytes.decode("utf-8", errors="ignore")

    # CUE pattern analysis
    if "INDEX 01 00" not in cue_text and not re.search(r"INDEX\s+01\s+00", cue_text):
        if "INDEX 01" not in cue_text:
            print("Error: The cue sheet is not valid\n")
            return

    file_match = re.search(r'FILE\s+"([^"]+)"', cue_text, re.IGNORECASE)
    if not file_match:
        file_match = re.search(r'FILE\s+([^\s]+)\s+BINARY', cue_text, re.IGNORECASE)
        if not file_match:
            print("Error: The cue sheet is not valid\n")
            return

    bin_filename = file_match.group(1)
    bin_path = cue_path.parent / bin_filename
    if not bin_path.exists():
        bin_path = Path(bin_filename)

    track_count = len(re.findall(r'TRACK\s+', cue_text, re.IGNORECASE))
    index1_count = len(re.findall(r'INDEX\s+01', cue_text, re.IGNORECASE))
    index0_count = len(re.findall(r'INDEX\s+00', cue_text, re.IGNORECASE))
    binary_count = len(re.findall(r'BINARY', cue_text, re.IGNORECASE))
    wave_count = len(re.findall(r'WAVE', cue_text, re.IGNORECASE))
    pregap_count = len(re.findall(r'PREGAP', cue_text, re.IGNORECASE))
    postgap_count = len(re.findall(r'POSTGAP', cue_text, re.IGNORECASE))

    if binary_count == 0:
        print("Error: Unstandard cue sheet\n")
        return
    if track_count == 0 or track_count != index1_count:
        print("Error: Cannot count tracks\n")
        return
    if binary_count != 1 or wave_count != 0:
        print("Error: Cue sheets of splitted dumps aren't supported\n")
        return

    fix_CDRWIN = 0
    if pregap_count == 1 and postgap_count == 0:
        print("Warning : The input file seems to be a CDRWIN cue sheet")
        print("          A pregap will be inserted in the output file...\n")
        fix_CDRWIN = 1

    headerbuf = bytearray(VCD_HEADER_SIZE)

    # POPS Header Descriptors
    headerbuf[0] = 0x41
    headerbuf[2] = 0xA0
    headerbuf[7] = 0x01
    headerbuf[8] = 0x20
    headerbuf[12] = 0xA1
    headerbuf[22] = 0xA2

    header_ptr = 20
    daTrack_ptr = 0
    noCDDA = 0

    # CUE track processing reproducing original C logic
    track_blocks = re.split(r'TRACK\s+', cue_text, flags=re.IGNORECASE)[1:]
    for i, block in enumerate(track_blocks):
        header_ptr += 10
        lines = block.strip().splitlines()
        first_line = lines[0] if lines else ""

        is_audio = "AUDIO" in first_line.upper()
        track_num_match = re.match(r'(\d+)', first_line)
        track_num = int(track_num_match.group(1)) if track_num_match else (i + 1)

        track_type = 0x01 if is_audio else 0x41
        headerbuf[10] = track_type
        headerbuf[20] = track_type
        headerbuf[header_ptr] = track_type

        headerbuf[header_ptr + 2] = ((track_num // 10) * 16) + (track_num % 10)
        headerbuf[17] = headerbuf[header_ptr + 2]

        idx00_match = re.search(r'INDEX\s+00\s+(\d+):(\d+):(\d+)', block, re.IGNORECASE)
        idx01_match = re.search(r'INDEX\s+01\s+(\d+):(\d+):(\d+)', block, re.IGNORECASE)

        if idx01_match:
            m1, s1, f1 = map(int, idx01_match.groups())
            m = ((m1 // 10) * 16) + (m1 % 10)
            s = ((s1 // 10) * 16) + (s1 % 10)
            f = ((f1 // 10) * 16) + (f1 % 10)

            if daTrack_ptr == 0 and is_audio and idx00_match:
                m0, s0, f0 = map(int, idx00_match.groups())
                daTrack_ptr = (((m0 * 4500) + (s0 * 75) + f0)) * SECTORSIZE
            elif daTrack_ptr == 0 and is_audio and not idx00_match:
                daTrack_ptr = (((m1 * 4500) + (s1 * 75) + f1)) * SECTORSIZE
            elif daTrack_ptr == 0 and not is_audio and i == len(track_blocks) - 1:
                noCDDA = 1

            headerbuf[header_ptr + 3] = m
            headerbuf[header_ptr + 4] = s
            headerbuf[header_ptr + 5] = f
            headerbuf[header_ptr + 7] = m
            headerbuf[header_ptr + 8] = s
            headerbuf[header_ptr + 9] = f

            if idx00_match:
                m0, s0, f0 = map(int, idx00_match.groups())
                headerbuf[header_ptr + 3] = ((m0 // 10) * 16) + (m0 % 10)
                headerbuf[header_ptr + 4] = ((s0 // 10) * 16) + (s0 % 10)
                headerbuf[header_ptr + 5] = ((f0 // 10) * 16) + (f0 % 10)

            # Unconditional +2s adjustment
            def apply_plus2(m_pos, s_pos):
                s_val = headerbuf[s_pos]
                m_val = headerbuf[m_pos]
                if s_val in (0x08, 0x09, 0x18, 0x19, 0x28, 0x29, 0x38, 0x39, 0x48, 0x49):
                    headerbuf[s_pos] += 8
                elif s_val in (0x58, 0x59):
                    headerbuf[s_pos] = 0x00 if s_val == 0x58 else 0x01
                    if m_val in (0x09, 0x19, 0x29, 0x39, 0x49, 0x59, 0x69, 0x79, 0x89):
                        headerbuf[m_pos] += 7
                    else:
                        headerbuf[m_pos] += 1
                else:
                    headerbuf[s_pos] += 2

            if i != 0:
                apply_plus2(header_ptr + 3, header_ptr + 4)
            apply_plus2(header_ptr + 7, header_ptr + 8)

            if fix_CDRWIN == 1:
                if i != 0:
                    apply_plus2(header_ptr + 3, header_ptr + 4)
                apply_plus2(header_ptr + 7, header_ptr + 8)

            if params.gap_more:
                if i != 0:
                    apply_plus2(header_ptr + 3, header_ptr + 4)
                apply_plus2(header_ptr + 7, header_ptr + 8)

            if params.gap_less:
                def apply_minus2(m_pos, s_pos):
                    s_val = headerbuf[s_pos]
                    m_val = headerbuf[m_pos]
                    if s_val in (0x10, 0x11, 0x20, 0x21, 0x30, 0x31, 0x40, 0x41, 0x50, 0x51):
                        headerbuf[s_pos] += 8
                    elif s_val in (0x00, 0x01):
                        headerbuf[s_pos] = 0x58 if s_val == 0x00 else 0x59
                        if m_val in (0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90):
                            headerbuf[m_pos] -= 7
                        else:
                            headerbuf[m_pos] -= 1
                    elif i != 0:
                        headerbuf[s_pos] -= 2

                if i != 0:
                    apply_minus2(header_ptr + 3, header_ptr + 4)
                apply_minus2(header_ptr + 7, header_ptr + 8)

    bin_size = get_file_size(bin_path)
    if bin_size < 0:
        return

    sector_count = get_lead_out(headerbuf, bin_size, pregap_count, postgap_count)
    if noCDDA == 1:
        daTrack_ptr = bin_size

    headerbuf[1032:1036] = sector_count.to_bytes(4, byteorder='little')
    headerbuf[1036:1040] = sector_count.to_bytes(4, byteorder='little')

    headerbuf[1024] = 0x6B
    headerbuf[1025] = 0x48
    headerbuf[1026] = 0x6E
    headerbuf[1027] = 0x20

    print("Saving the virtual CD-ROM image. Please wait...")
    vcd_out_path = Path(vcd_name)
    try:
        with open(vcd_out_path, "wb") as f_vcd:
            f_vcd.write(headerbuf)
    except Exception as e:
        print(f"Error: Cannot write to {vcd_name}: {e}")
        return

    # Chunked I/O processing via IO_BUFFER_SIZE (256 KiB) accumulating a logical 1 MiB window
    analysis_window = bytearray()
    
    try:
        with open(vcd_out_path, "ab+") as f_vcd, open(bin_path, "rb") as f_bin:
            i = 0
            while i < bin_size:
                if g_interrupted:
                    print("\nOperation interrupted by user.")
                    return

                # Read up to 1 MiB to build the logical analysis window from original C
                read_target = min(VCD_HEADER_SIZE, bin_size - i)
                window_buf = bytearray()
                
                while len(window_buf) < read_target:
                    chunk = f_bin.read(min(IO_BUFFER_SIZE, read_target - len(window_buf)))
                    if not chunk:
                        break
                    window_buf.extend(chunk)

                if fix_CDRWIN == 1 and (i + len(window_buf) >= daTrack_ptr):
                    bytes_before = daTrack_ptr - i
                    if bytes_before > 0:
                        f_vcd.write(window_buf[:bytes_before])

                    # Insert 150 zero sectors (CDRWIN pregap fix)
                    padding = b"\x00" * (150 * SECTORSIZE)
                    f_vcd.write(padding)

                    bytes_after = len(window_buf) - bytes_before
                    if bytes_after > 0:
                        f_vcd.write(window_buf[bytes_after:])
                    fix_CDRWIN = 0
                else:
                    if params.vmode and i == 0:
                        print("-" * 82)
                        print("NTSC Patcher is ON")
                        print("-" * 82)

                    view = memoryview(window_buf)
                    if i == 0:
                        game_identifier(view, params)

                    if params.game_title >= 0 and params.game_has_cheats == 1 and params.trainer and i == 0:
                        print("game_trainer is ON")
                        print("-" * 82)

                    if params.game_title >= 0 and params.game_trained == 0 and params.game_has_cheats == 1 and params.trainer and i <= daTrack_ptr:
                        game_trainer(view, params)

                    if params.game_title >= 0 and params.game_fixed == 0 and params.fix_game == 1 and i <= daTrack_ptr:
                        game_fixer(view, params)

                    if params.vmode and i <= daTrack_ptr:
                        ntsc_patcher(view, i, params)

                    f_vcd.write(window_buf)

                i += len(window_buf)

    except Exception as e:
        print(f"Error during file processing: {e}")
        return

    if params.game_title >= 0 and params.fix_game == 1 and params.game_fixed == 0:
        print("COULD NOT APPLY THE GAME FIXE(S) : No data to patch found")
        print("-" * 82)
    if params.game_title >= 0 and params.game_has_cheats == 1 and params.game_trained == 0 and params.trainer:
        print("COULD NOT APPLY THE GAME CHEAT(S) : No data to patch found")
        print("-" * 82)

    print("A POPS virtual CD-ROM image was saved to :")
    print(f"{vcd_name}\n")


if __name__ == "__main__":
    main()
