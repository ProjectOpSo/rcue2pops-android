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

# CRITICAL CONSTANTS - DO NOT CHANGE
# POPS VCD images require 2352-byte RAW sectors and a fixed 1 MiB VCD header.
SECTORSIZE = 2352
VCD_HEADER_SIZE = 0x100000
IO_BUFFER_SIZE = 0x40000

# Global flag to track user interruption (SIGINT/SIGTERM)
g_interrupted = False


def handle_signal(sig, frame):
    """Signal handler for graceful shutdown on user cancellation."""
    global g_interrupted
    g_interrupted = True


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


@dataclass
class Parameters:
    """Stores execution flags, patching options, and internal game identification states."""
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


def dec_to_bcd(val: int) -> int:
    """Converts a standard decimal integer into a packed BCD (Binary Coded Decimal) byte representation."""
    return ((val // 10) * 16) + (val % 10)


def bcd_add_seconds(m_bcd: int, s_bcd: int, sec_offset: int) -> tuple[int, int]:
    """
    Adjusts BCD minute and second values by adding or subtracting seconds.
    Replicates the exact CUE2POPS BCD arithmetic for gap++/gap-- and the mandatory 2-second offset.
    """
    if sec_offset == 2:
        if (s_bcd in (0x08, 0x09, 0x18, 0x19, 0x28, 0x29, 0x38, 0x39, 0x48, 0x49)) and (s_bcd not in (0x58, 0x59)):
            s_bcd += 8
        elif s_bcd in (0x58, 0x59):
            s_bcd = 0x00 if s_bcd == 0x58 else 0x01
            if m_bcd in (0x09, 0x19, 0x29, 0x39, 0x49, 0x59, 0x69, 0x79, 0x89):
                m_bcd += 7  # BCD decade carry (e.g., 0x09 + 7 = 0x10)
            else:
                m_bcd += 1
        else:
            s_bcd += 2
    elif sec_offset == -2:
        if (s_bcd in (0x10, 0x11, 0x20, 0x21, 0x30, 0x31, 0x40, 0x41, 0x50, 0x51)) and (s_bcd not in (0x00, 0x01)):
            s_bcd -= 8
        elif s_bcd in (0x00, 0x01):
            s_bcd = 0x58 if s_bcd == 0x00 else 0x59
            if m_bcd in (0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90):
                m_bcd -= 7  # BCD decade borrow (e.g., 0x10 - 7 = 0x09)
            else:
                m_bcd -= 1
        else:
            s_bcd -= 2
    return m_bcd, s_bcd


def check_disk_space(path: Path, required_bytes: int) -> bool:
    """Verifies that the target storage medium has enough free space before starting conversion."""
    try:
        parent_dir = path.parent if not path.exists() else path
        usage = shutil.disk_usage(parent_dir)
        if usage.free < required_bytes:
            sys.stderr.write(
                f"[ERROR] Insufficient disk space. Required: {required_bytes} bytes, "
                f"Available: {usage.free} bytes\n"
            )
            return False
        return True
    except Exception:
        return True


def game_identifier(inbuf: bytearray, p: Parameters):
    """Scans the primary executable region in sector 0 to detect specific game titles and apply flags."""
    if p.debug_cue:
        if not p.vmode:
            print("-" * 82)
        print("Hello from game_identifier!")

    if p.game_title == 0:
        for ptr in range(0, min(len(inbuf), VCD_HEADER_SIZE) - 16, 4):
            # Crash Bandicoot [SCES-00344]
            if inbuf[ptr:ptr+16] == b"SCES-00344      ":
                if not p.debug_cue:
                    print("-" * 82)
                print("Crash Bandicoot [SCES-00344]")
                p.deny_vmode += 1
                p.game_title = 1
                p.game_has_cheats = 1
                p.fix_game = 0
                break
            # Crash Bandicoot [SCUS-94900]
            if inbuf[ptr:ptr+16] == b"SCUS-94900      ":
                if not p.debug_cue:
                    print("-" * 82)
                print("Crash Bandicoot [SCUS-94900]")
                p.game_title = 2
                p.game_has_cheats = 1
                p.fix_game = 0
                break
            # Crash Bandicoot [SCPS-10031]
            if inbuf[ptr:ptr+16] == b"SCPS-10031      ":
                if not p.debug_cue:
                    print("-" * 82)
                print("Crash Bandicoot [SCPS-10031]")
                p.game_title = 3
                p.game_has_cheats = 1
                p.fix_game = 0
                break
            # Metal Gear Solid : Special Missions [SLES-02136]
            if inbuf[ptr:ptr+18] == b" 1999081614163300$":
                if not p.debug_cue:
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
    if p.game_title != 0 and not p.debug_cue:
        print("-" * 82)


def game_fixer(inbuf: bytearray, p: Parameters):
    """Applies title-specific compatibility binary patches (e.g., MGS Special Missions disc swap)."""
    if p.game_fixed == 0:
        for ptr in range(0, min(len(inbuf), VCD_HEADER_SIZE) - 4, 4):
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


def game_trainer(inbuf: bytearray, p: Parameters):
    """Applies cheat/trainer byte patches for recognized game titles."""
    if p.game_trained == 0:
        for ptr in range(0, min(len(inbuf), VCD_HEADER_SIZE) - 4, 4):
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


def ntsc_patcher(inbuf: bytearray, tracker: int, p: Parameters):
    """Scans and updates PAL GPU routines to force NTSC video mode and adjust Y-Pos offsets."""
    for i in range(0, min(len(inbuf), VCD_HEADER_SIZE) - 28, 4):
        # Y-Pos pattern match
        if (inbuf[i] == 0x13 and inbuf[i+1] == 0x00 and (inbuf[i+2] in (0x90, 0x91)) and inbuf[i+3] == 0x24) and \
           (inbuf[i+4] == 0x10 and inbuf[i+5] == 0x00 and (inbuf[i+6] in (0x90, 0x91)) and inbuf[i+7] == 0x24):
            print(f"Y-Pos pattern found at dump offset 0x{tracker + i:X} / LBA {(tracker + i) // SECTORSIZE} (VCD offset 0x{VCD_HEADER_SIZE + tracker + i:X})")
            inbuf[i] = 0xF8
            inbuf[i+1] = 0xFF
            inbuf[i+4] = 0xF8
            inbuf[i+5] = 0xFF
            print("-" * 82)
        # VMODE pattern match
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


def parse_cue_and_build_header(cue_path: Path, bin_size: int, params: Parameters) -> tuple[bytearray, Path, int, int]:
    """
    Parses the CUE sheet, computes TOC descriptors (A0, A1, A2, track entries in BCD),
    calculates the Lead-Out position, and generates the complete 1 MiB POPS header.
    """
    with open(cue_path, "r", encoding="utf-8", errors="ignore") as f:
        cue_text = f.read()

    if "FILE " not in cue_text:
        raise ValueError("Error: The cue sheet is not valid")

    # Locate linked BIN image name inside CUE sheet
    file_match = re.search(r'FILE\s+["\']?([^"\']+)["\']?\s+BINARY', cue_text, re.IGNORECASE)
    if not file_match:
        file_match = re.search(r'FILE\s+["\']?([^"\']+)["\']?', cue_text, re.IGNORECASE)
    if not file_match:
        raise ValueError("Error: The cue sheet is not valid")

    bin_filename = file_match.group(1)
    bin_path = cue_path.parent / bin_filename
    if not bin_path.exists():
        bin_path = cue_path.parent / Path(bin_filename).name

    # Validate structure and enforce Single-BIN format
    binary_count = len(re.findall(r'\bBINARY\b', cue_text, re.IGNORECASE))
    wave_count = len(re.findall(r'\bWAVE\b', cue_text, re.IGNORECASE))
    track_matches = list(re.finditer(r'TRACK\s+(\d+)\s+([^\s\r\n]+)', cue_text, re.IGNORECASE))
    index1_count = len(re.findall(r'INDEX 01', cue_text, re.IGNORECASE))
    pregap_count = len(re.findall(r'\bPREGAP\b', cue_text, re.IGNORECASE))
    postgap_count = len(re.findall(r'\bPOSTGAP\b', cue_text, re.IGNORECASE))

    if binary_count == 0:
        raise ValueError("Error: Unstandard cue sheet")
    if len(track_matches) == 0 or len(track_matches) != index1_count:
        raise ValueError("Error: Cannot count tracks")
    if binary_count != 1 or wave_count != 0:
        raise ValueError("Error: Cue sheets of splitted dumps aren't supported. Please merge your multi-BIN dump into a single BIN file first.")

    fix_CDRWIN = 1 if (pregap_count == 1 and postgap_count == 0) else 0
    if fix_CDRWIN:
        print("Warning : The input file seems to be a CDRWIN cue sheet")
        print("          A pregap will be inserted in the output file...\n")

    if "TRACK 01 MODE2/2352" not in cue_text.upper():
        raise ValueError("Error: Looks like your game dump is not MODE2/2352, or the cue is invalid.\n")

    # Allocate 1 MiB POPS header buffer
    headerbuf = bytearray(VCD_HEADER_SIZE)

    # Base TOC Descriptor setup
    headerbuf[0] = 0x41   # 1st track Type = DATA
    headerbuf[2] = 0xA0   # Descriptor A0
    headerbuf[7] = 0x01   # 1st track number = 1
    headerbuf[8] = 0x20   # Disc Type = CD-XA
    headerbuf[12] = 0xA1  # Descriptor A1
    headerbuf[22] = 0xA2  # Descriptor A2

    header_ptr = 20
    daTrack_ptr = 0

    lines = cue_text.splitlines()
    current_track_type = 0x41
    current_track_num = 1
    gap_msf = None

    # Parse tracks and write BCD entries
    for line in lines:
        line_s = line.strip()
        if not line_s:
            continue

        tm = re.match(r'TRACK\s+(\d+)\s+([^\s]+)', line_s, re.IGNORECASE)
        if tm:
            current_track_num = int(tm.group(1))
            ttype_str = tm.group(2).upper()
            current_track_type = 0x01 if "AUDIO" in ttype_str else 0x41
            gap_msf = None
            continue

        idx0_m = re.match(r'INDEX 00\s+(\d+):(\d+):(\d+)', line_s, re.IGNORECASE)
        if idx0_m:
            gap_msf = [int(idx0_m.group(1)), int(idx0_m.group(2)), int(idx0_m.group(3))]
            continue

        idx1_m = re.match(r'INDEX 01\s+(\d+):(\d+):(\d+)', line_s, re.IGNORECASE)
        if idx1_m:
            header_ptr += 10
            idx1_msf = [int(idx1_m.group(1)), int(idx1_m.group(2)), int(idx1_m.group(3))]

            headerbuf[10] = current_track_type
            headerbuf[20] = current_track_type
            headerbuf[header_ptr] = current_track_type

            track_num_bcd = dec_to_bcd(current_track_num)
            headerbuf[header_ptr + 2] = track_num_bcd
            headerbuf[17] = track_num_bcd

            # Detect pointer offset to the first CD-DA audio track
            if daTrack_ptr == 0 and current_track_type == 0x01 and gap_msf is not None:
                daTrack_ptr = ((gap_msf[0] * 4500) + (gap_msf[1] * 75) + gap_msf[2]) * SECTORSIZE
            elif daTrack_ptr == 0 and current_track_type == 0x01 and gap_msf is None:
                daTrack_ptr = ((idx1_msf[0] * 4500) + (idx1_msf[1] * 75) + idx1_msf[2]) * SECTORSIZE

            m_00_bcd = dec_to_bcd(idx1_msf[0])
            s_00_bcd = dec_to_bcd(idx1_msf[1])
            f_00_bcd = dec_to_bcd(idx1_msf[2])

            m_01_bcd = m_00_bcd
            s_01_bcd = s_00_bcd
            f_01_bcd = f_00_bcd

            if gap_msf is not None:
                m_00_bcd = dec_to_bcd(gap_msf[0])
                s_00_bcd = dec_to_bcd(gap_msf[1])
                f_00_bcd = dec_to_bcd(gap_msf[2])

            idx_i = (header_ptr - 30) // 10

            # Mandatory +2 second offset for track indexing
            if idx_i != 0:
                m_00_bcd, s_00_bcd = bcd_add_seconds(m_00_bcd, s_00_bcd, 2)
                m_01_bcd, s_01_bcd = bcd_add_seconds(m_01_bcd, s_01_bcd, 2)
            else:
                m_01_bcd, s_01_bcd = bcd_add_seconds(m_01_bcd, s_01_bcd, 2)

            if fix_CDRWIN == 1 and idx_i != 0:
                m_00_bcd, s_00_bcd = bcd_add_seconds(m_00_bcd, s_00_bcd, 2)
                m_01_bcd, s_01_bcd = bcd_add_seconds(m_01_bcd, s_01_bcd, 2)

            if params.gap_more and idx_i != 0:
                m_00_bcd, s_00_bcd = bcd_add_seconds(m_00_bcd, s_00_bcd, 2)
                m_01_bcd, s_01_bcd = bcd_add_seconds(m_01_bcd, s_01_bcd, 2)

            if params.gap_less and idx_i != 0:
                m_00_bcd, s_00_bcd = bcd_add_seconds(m_00_bcd, s_00_bcd, -2)
                m_01_bcd, s_01_bcd = bcd_add_seconds(m_01_bcd, s_01_bcd, -2)

            headerbuf[header_ptr + 3] = m_00_bcd
            headerbuf[header_ptr + 4] = s_00_bcd
            headerbuf[header_ptr + 5] = f_00_bcd

            headerbuf[header_ptr + 7] = m_01_bcd
            headerbuf[header_ptr + 8] = s_01_bcd
            headerbuf[header_ptr + 9] = f_01_bcd

    if daTrack_ptr == 0 and headerbuf[10] == 0x41:
        daTrack_ptr = bin_size

    # Calculate Lead-Out location in MSF format
    total_sectors = (bin_size // SECTORSIZE) + (150 * (pregap_count + postgap_count)) + 150
    leadoutM = total_sectors // 4500
    leadoutS = (total_sectors - leadoutM * 4500) // 75
    leadoutF = total_sectors - leadoutM * 4500 - leadoutS * 75

    headerbuf[27] = dec_to_bcd(leadoutM)
    headerbuf[28] = dec_to_bcd(leadoutS)
    headerbuf[29] = dec_to_bcd(leadoutF)

    # Write total sector count at 0x0408 and 0x040C in Little-Endian uint32
    sector_count_final = (bin_size // SECTORSIZE) + (150 * (pregap_count + postgap_count))
    sec_bytes = sector_count_final.to_bytes(4, byteorder='little')
    headerbuf[1032:1036] = sec_bytes
    headerbuf[1036:1040] = sec_bytes

    # Insert "kHn " POPS signature at offset 1024
    headerbuf[1024:1028] = b"\x6B\x48\x6E\x20"

    return headerbuf, bin_path, daTrack_ptr, fix_CDRWIN


def convert_cue_to_vcd(cue_path: Path, vcd_path: Path, params: Parameters) -> bool:
    """Performs streaming image creation by writing the VCD header and copying BIN data in chunks."""
    global g_interrupted

    with open(cue_path, "r", encoding="utf-8", errors="ignore") as f:
        cue_text = f.read()
    file_match = re.search(r'FILE\s+["\']?([^"\']+)["\']?', cue_text, re.IGNORECASE)

    if not file_match:
        sys.stderr.write("[ERROR] No FILE entry found in the .cue file.\n")
        return False

    bin_path_temp = cue_path.parent / file_match.group(1)
    if not bin_path_temp.exists():
        bin_path_temp = cue_path.parent / Path(file_match.group(1)).name

    if not bin_path_temp.exists():
        sys.stderr.write(f"[ERROR] Input BIN file was not found: {bin_path_temp}\n")
        return False

    bin_size = bin_path_temp.stat().st_size
    required_space = VCD_HEADER_SIZE + bin_size + (150 * SECTORSIZE)

    if not check_disk_space(vcd_path, required_space):
        return False

    try:
        headerbuf, bin_path, daTrack_ptr, fix_CDRWIN = parse_cue_and_build_header(cue_path, bin_size, params)
    except Exception as e:
        sys.stderr.write(f"[ERROR] {e}\n")
        return False

    tmp_vcd_path = vcd_path.with_suffix(".vcd.tmp")

    try:
        outbuf = bytearray(IO_BUFFER_SIZE)
        padding_buffer = bytearray(150 * SECTORSIZE)

        print("Saving the virtual CD-ROM image. Please wait...")

        with open(tmp_vcd_path, "wb") as vcd_file, open(bin_path, "rb") as bin_file:
            # Step 1: Write the 1 MiB POPS VCD header
            vcd_file.write(headerbuf)

            # Step 2: Stream BIN image content in memory-efficient chunks
            i = 0
            while i < bin_size:
                if g_interrupted:
                    print("\nOperation cancelled by user.")
                    return False

                # Handle CDRWIN pregap padding insertion at audio transition
                if fix_CDRWIN == 1 and (i + VCD_HEADER_SIZE >= daTrack_ptr):
                    bytes_before = daTrack_ptr - i
                    if bytes_before > 0:
                        bin_file.readinto(memoryview(outbuf)[:bytes_before])
                        vcd_file.write(memoryview(outbuf)[:bytes_before])

                    vcd_file.write(padding_buffer)

                    bytes_after = VCD_HEADER_SIZE - bytes_before
                    if bytes_after > 0:
                        bin_file.readinto(memoryview(outbuf)[:bytes_after])
                        vcd_file.write(memoryview(outbuf)[:bytes_after])

                    fix_CDRWIN = 0
                    i += VCD_HEADER_SIZE
                    continue

                chunk_to_read = min(VCD_HEADER_SIZE, bin_size - i)
                bytes_read = bin_file.readinto(memoryview(outbuf)[:chunk_to_read])

                if bytes_read == 0:
                    break

                chunk_view = memoryview(outbuf)[:bytes_read]

                # Identify game and apply executable patches on the first block
                if i == 0:
                    game_identifier(chunk_view, params)

                if params.game_title >= 0 and params.game_has_cheats == 1 and params.trainer and i == 0:
                    print("game_trainer is ON\n" + "-" * 82)

                if params.game_title >= 0 and params.game_trained == 0 and params.game_has_cheats == 1 and params.trainer and i <= daTrack_ptr:
                    game_trainer(chunk_view, params)

                if params.game_title >= 0 and params.game_fixed == 0 and params.fix_game == 1 and i <= daTrack_ptr:
                    game_fixer(chunk_view, params)

                if params.vmode and i <= daTrack_ptr:
                    ntsc_patcher(chunk_view, i, params)

                vcd_file.write(chunk_view)
                i += bytes_read

            if not params.no_sync:
                vcd_file.flush()
                os.fsync(vcd_file.fileno())

        # Atomic file replacement
        os.replace(tmp_vcd_path, vcd_path)
        print("A POPS virtual CD-ROM image was saved to :")
        print(f"{vcd_path}\n")

        # Optional debug header inspection
        if params.debug_validate:
            print("\n--- VALIDAÇÃO DE DEBUG DO HEADER GENERATED ---")
            print(f"Marcador CUE2POPS: {headerbuf[1024:1028].decode('ascii', errors='ignore')}")
            sec_cnt = int.from_bytes(headerbuf[1032:1036], 'little')
            print(f"Total de Setores (Header): {sec_cnt} (0x{sec_cnt:X})")
            print(f"Lead-Out MSF: {headerbuf[27]:02X}:{headerbuf[28]:02X}:{headerbuf[29]:02X}")
            print(f"Tamanho do VCD: {vcd_path.stat().st_size} bytes")
            print("--------------------------------------------\n")

        return True

    except Exception as e:
        sys.stderr.write(f"[ERROR] Conversion failed: {e}\n")
        if tmp_vcd_path.exists():
            try:
                tmp_vcd_path.unlink()
            except Exception:
                pass
        return False


def main():
    """Command line argument entry point."""
    parser = argparse.ArgumentParser(
        description="BIN/CUE to IMAGE0.VCD conversion tool (Python Port)"
    )
    parser.add_argument("input_cue", help="Input CUE sheet file path")
    parser.add_argument("vcd_path", nargs="?", default=None, help="Optional output VCD file path")
    parser.add_argument("--no-sync", action="store_true", help="Disable os.fsync() buffer flushing")
    parser.add_argument("--debug-validate", action="store_true", help="Display header debug metadata")
    parser.add_argument("extra_args", nargs="*", help="Legacy flags (vmode, trainer, gap++, gap--)")

    args = parser.parse_args()

    cue_path = Path(args.input_cue)
    if not cue_path.exists():
        print(f"input .cue file: {cue_path} did not exist")
        sys.exit(0)

    params = Parameters()
    params.no_sync = args.no_sync
    params.debug_validate = args.debug_validate

    vcd_path = Path(args.vcd_path) if args.vcd_path and not args.vcd_path.startswith("--") else None

    all_extras = args.extra_args + ([args.vcd_path] if args.vcd_path and args.vcd_path.startswith("--") else [])
    for arg in all_extras:
        arg_l = arg.lower()
        if arg_l == "gap++":
            params.gap_more = True
        elif arg_l == "gap--":
            params.gap_less = True
        elif arg_l == "vmode":
            params.vmode = True
        elif arg_l == "trainer":
            params.trainer = True
        elif vcd_path is None and not arg.startswith("--"):
            vcd_path = Path(arg)

    if params.gap_more and params.gap_less:
        print("Syntax Error : Conflicting gap++/gap-- arguments.\n")
        sys.exit(0)

    if vcd_path is None:
        vcd_path = cue_path.with_suffix(".VCD")

    success = convert_cue_to_vcd(cue_path, vcd_path, params)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
