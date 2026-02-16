#!/usr/bin/env python3
"""
instatimefixer - Fix "Shooting Time" in Insta360 video files.

When an Insta360 camera (e.g. GO Ultra) records without being synced to a phone,
it may stamp videos with the wrong date/time. The "Shooting Time" shown in
Insta360 Studio/app comes from proprietary metadata that standard tools like
ExifTool cannot fully reach. This script patches all the relevant timestamp
fields in-place so the app displays the correct date.

What gets patched:
  1. MP4 atom timestamps (mvhd, tkhd, mdhd) - standard MP4 creation/modification
  2. Protobuf creation_time in the 'inst' box (field 7, tag 0x38)
  3. first_gps_timestamp in the 'inst' box (field 36, tag 0xa0 0x02)
  4. ASCII date/time strings in embedded file paths
  5. UTF-16LE date/time strings in embedded filenames

All patches are the same byte length as the originals, so no file structure is
altered. Only the timestamp values change.

Requires: Python 3.10+ (no external dependencies)

Usage:
  python3 instatimefixer.py [options] <file>... <correct_time>
  python3 instatimefixer.py [options] <file>... <wrong_time> <correct_time>

  Times are in YYYYMMDDHHmmss format (14 digits, 24-hour clock).
  If only <correct_time> is given, the wrong time is auto-detected from the file.

Options:
  --dry-run   Show what would be patched without writing any changes.
  --read      Just display the current Shooting Time from each file and exit.

Examples:
  # Auto-detect wrong time, patch both MP4 and LRV:
  python3 instatimefixer.py VID_*.mp4 LRV_*.lrv 20260215143800

  # Explicit wrong and correct times:
  python3 instatimefixer.py VID_*.mp4 LRV_*.lrv 20260103194656 20260215143800

  # Preview changes without writing:
  python3 instatimefixer.py --dry-run VID_*.mp4 LRV_*.lrv 20260215143800

  # Just read the current Shooting Time:
  python3 instatimefixer.py --read VID_*.mp4 LRV_*.lrv
"""

import sys
import struct
from datetime import datetime, timezone
from pathlib import Path

# Seconds between 1904-01-01 and 1970-01-01 (MP4 epoch offset)
_MP4_EPOCH_OFFSET = 2082844800

# Read files in 64 MB chunks to handle large videos without loading into memory
_CHUNK_SIZE = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Protobuf varint helpers
# ---------------------------------------------------------------------------

def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf base-128 varint."""
    parts = []
    while value > 0x7F:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    parts.append(value)
    return bytes(parts)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint starting at *pos*. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not (byte & 0x80):
            break
        shift += 7
    return result, pos


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> datetime:
    """Parse a YYYYMMDDHHmmss string into a UTC datetime."""
    return datetime(
        int(ts_str[:4]), int(ts_str[4:6]), int(ts_str[6:8]),
        int(ts_str[8:10]), int(ts_str[10:12]), int(ts_str[12:14]),
        tzinfo=timezone.utc,
    )


def _fmt_ts(ts_str: str) -> str:
    """Format a 14-digit timestamp string for display."""
    return f"{ts_str[:4]}-{ts_str[4:6]}-{ts_str[6:8]} {ts_str[8:10]}:{ts_str[10:12]}:{ts_str[12:14]}"


def validate_timestamp(ts_str: str) -> None:
    """Raise ValueError if *ts_str* is not a valid YYYYMMDDHHmmss string."""
    if len(ts_str) != 14 or not ts_str.isdigit():
        raise ValueError(f"Timestamp must be 14 digits (YYYYMMDDHHmmss), got: {ts_str}")
    y, mo, d = int(ts_str[:4]), int(ts_str[4:6]), int(ts_str[6:8])
    h, mi, s = int(ts_str[8:10]), int(ts_str[10:12]), int(ts_str[12:14])
    if not (2000 <= y <= 2099):
        raise ValueError(f"Year out of range: {y}")
    if not (1 <= mo <= 12):
        raise ValueError(f"Month out of range: {mo}")
    if not (1 <= d <= 31):
        raise ValueError(f"Day out of range: {d}")
    if not (0 <= h <= 23):
        raise ValueError(f"Hour out of range: {h}")
    if not (0 <= mi <= 59):
        raise ValueError(f"Minute out of range: {mi}")
    if not (0 <= s <= 59):
        raise ValueError(f"Second out of range: {s}")


# ---------------------------------------------------------------------------
# Binary search
# ---------------------------------------------------------------------------

def _find_pattern(filepath: str, pattern: bytes) -> list[int]:
    """Return every file offset where *pattern* occurs, using chunked reads."""
    overlap = len(pattern)
    offsets: list[int] = []
    with open(filepath, "rb") as f:
        file_pos = 0
        prev_tail = b""
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            search_data = prev_tail + chunk
            search_base = file_pos - len(prev_tail)
            pos = 0
            while True:
                idx = search_data.find(pattern, pos)
                if idx == -1:
                    break
                offsets.append(search_base + idx)
                pos = idx + 1
            prev_tail = chunk[-overlap:] if len(chunk) >= overlap else chunk
            file_pos += len(chunk)
    return offsets


# ---------------------------------------------------------------------------
# Read current Shooting Time
# ---------------------------------------------------------------------------

def read_shooting_time(filepath: str) -> str:
    """Read the Insta360 creation_time (protobuf field 7) from a file.

    Searches for the protobuf tag byte 0x38 followed by a varint that decodes
    to a plausible YYYYMMDDHHmmss value. Returns the 14-digit string.
    """
    for offset in _find_pattern(filepath, b"\x38"):
        with open(filepath, "rb") as f:
            f.seek(offset + 1)
            buf = f.read(10)
        try:
            val, _ = _decode_varint(buf, 0)
        except Exception:
            continue
        s = str(val)
        if len(s) != 14 or s[:2] != "20":
            continue
        y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:8])
        h, mi, sec = int(s[8:10]), int(s[10:12]), int(s[12:14])
        if 1 <= mo <= 12 and 1 <= d <= 31 and h <= 23 and mi <= 59 and sec <= 59:
            return s
    raise ValueError(f"Could not find Insta360 creation_time in {filepath}")


# ---------------------------------------------------------------------------
# Patch logic
# ---------------------------------------------------------------------------

def _patch_write(filepath: str, offset: int, old: bytes, new: bytes) -> bool:
    """Verify *old* bytes at *offset*, then overwrite with *new*."""
    with open(filepath, "r+b") as f:
        f.seek(offset)
        existing = f.read(len(old))
        if existing != old:
            print(f"    ERROR: unexpected bytes at offset {offset}")
            return False
        f.seek(offset)
        f.write(new)
    return True


def patch_file(
    filepath: str,
    wrong_str: str,
    correct_str: str,
    dry_run: bool = False,
) -> bool:
    """Find and patch every timestamp location in *filepath*.

    Returns True if all patches succeeded (or dry_run found patches).
    """
    wrong_ts = int(wrong_str)
    correct_ts = int(correct_str)
    wrong_date = wrong_str[:8]
    correct_date = correct_str[:8]
    wrong_time = wrong_str[8:]
    correct_time = correct_str[8:]

    wrong_dt = _parse_ts(wrong_str)
    correct_dt = _parse_ts(correct_str)
    delta_sec = int((correct_dt - wrong_dt).total_seconds())
    delta_ms = delta_sec * 1000

    patches: list[dict] = []

    # ---- MP4 atom timestamps (mvhd, tkhd, mdhd) ----
    for box_type in (b"mvhd", b"tkhd", b"mdhd"):
        for offset in _find_pattern(filepath, box_type):
            with open(filepath, "rb") as f:
                f.seek(offset + 4)
                buf = f.read(20)
            version = buf[0]
            if version != 0:
                continue
            # Read creation and modification independently — they may differ
            ct = struct.unpack_from(">I", buf, 4)[0]
            try:
                ct_dt = datetime.fromtimestamp(ct - _MP4_EPOCH_OFFSET, tz=timezone.utc)
            except (OSError, ValueError):
                continue
            if not (2000 <= ct_dt.year <= 2099):
                continue
            for field_off, name in ((4, "creation"), (8, "modification")):
                val = struct.unpack_from(">I", buf, field_off)[0]
                new_val = val + delta_sec
                patches.append({
                    "type": f"{box_type.decode()}.{name}",
                    "offset": offset + 4 + field_off,
                    "old": struct.pack(">I", val),
                    "new": struct.pack(">I", new_val),
                })

    # ---- Protobuf creation_time (field 7, tag 0x38) ----
    wrong_varint = _encode_varint(wrong_ts)
    correct_varint = _encode_varint(correct_ts)
    if len(wrong_varint) != len(correct_varint):
        print(f"  ERROR: varint length mismatch ({len(wrong_varint)} vs "
              f"{len(correct_varint)}), cannot patch in-place.")
        return False

    for offset in _find_pattern(filepath, b"\x38" + wrong_varint):
        patches.append({
            "type": "protobuf creation_time",
            "offset": offset + 1,
            "old": wrong_varint,
            "new": correct_varint,
        })

    # ---- first_gps_timestamp (field 36, tag 0xa0 0x02) ----
    wrong_epoch_ms = int(wrong_dt.timestamp() * 1000)
    for offset in _find_pattern(filepath, b"\xa0\x02"):
        with open(filepath, "rb") as f:
            f.seek(offset + 2)
            buf = f.read(10)
        try:
            val, vend = _decode_varint(buf, 0)
        except Exception:
            continue
        # Must be a plausible ms-epoch within 24 h of the wrong time
        if not (1577836800000 <= val <= 1893456000000):
            continue
        if abs(val - wrong_epoch_ms) >= 86_400_000:
            continue
        old_v = buf[:vend]
        new_v = _encode_varint(val + delta_ms)
        if len(old_v) == len(new_v):
            patches.append({
                "type": "first_gps_timestamp",
                "offset": offset + 2,
                "old": old_v,
                "new": new_v,
            })

    # ---- ASCII date/time in embedded file paths ----
    wrong_ascii = f"{wrong_date}_{wrong_time}".encode("ascii")
    correct_ascii = f"{correct_date}_{correct_time}".encode("ascii")
    for offset in _find_pattern(filepath, wrong_ascii):
        patches.append({
            "type": "ASCII filename date",
            "offset": offset,
            "old": wrong_ascii,
            "new": correct_ascii,
        })

    # ---- UTF-16LE date/time in inst box header filename ----
    wrong_utf16 = wrong_date.encode("utf-16-le")
    correct_utf16 = correct_date.encode("utf-16-le")
    for offset in _find_pattern(filepath, wrong_utf16):
        with open(filepath, "rb") as f:
            ctx_start = max(0, offset - 20)
            f.seek(ctx_start)
            ctx = f.read(60)
        if b"V\x00I\x00D\x00" not in ctx and b"L\x00R\x00V\x00" not in ctx:
            continue
        patches.append({
            "type": "UTF-16LE filename date",
            "offset": offset,
            "old": wrong_utf16,
            "new": correct_utf16,
        })
        # Time portion follows: date (16 B) + separator (2 B) + time
        time_offset = offset + 18
        wrong_time_u16 = wrong_time.encode("utf-16-le")
        correct_time_u16 = correct_time.encode("utf-16-le")
        with open(filepath, "rb") as f:
            f.seek(time_offset)
            if f.read(len(wrong_time_u16)) == wrong_time_u16:
                patches.append({
                    "type": "UTF-16LE filename time",
                    "offset": time_offset,
                    "old": wrong_time_u16,
                    "new": correct_time_u16,
                })

    # ---- Deduplicate & report ----
    seen: set[tuple[int, str]] = set()
    unique: list[dict] = []
    for p in patches:
        key = (p["offset"], p["type"])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    patches = unique

    if not patches:
        print("  No patchable timestamps found.")
        return False

    for p in patches:
        print(f"  [{p['type']}] offset {p['offset']}")

    if dry_run:
        print(f"  ({len(patches)} patches found, dry run — no changes written)")
        return True

    ok = True
    for p in patches:
        if not _patch_write(filepath, p["offset"], p["old"], p["new"]):
            ok = False
    if ok:
        print(f"  {len(patches)} patches applied.")
    return ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2 or "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__.strip())
        sys.exit(0 if "--help" in sys.argv or "-h" in sys.argv else 1)

    dry_run = "--dry-run" in sys.argv
    read_only = "--read" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    # --read mode: just display current Shooting Time
    if read_only:
        for filepath in args:
            if not Path(filepath).exists():
                print(f"{filepath}: not found")
                continue
            try:
                ts = read_shooting_time(filepath)
                print(f"{Path(filepath).name}: {_fmt_ts(ts)}")
            except ValueError as e:
                print(f"{Path(filepath).name}: {e}")
        return

    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    # Determine if wrong_time was provided or should be auto-detected.
    auto_detect = False
    last = args[-1]
    second_last = args[-2] if len(args) >= 3 else ""

    if (len(args) >= 3
            and second_last.isdigit() and len(second_last) == 14
            and last.isdigit() and len(last) == 14):
        files = args[:-2]
        wrong_str = second_last
        correct_str = last
    elif last.isdigit() and len(last) == 14:
        files = args[:-1]
        correct_str = last
        wrong_str = ""
        auto_detect = True
    else:
        print(__doc__)
        sys.exit(1)

    if dry_run:
        print("=== DRY RUN MODE ===\n")

    validate_timestamp(correct_str)

    if auto_detect:
        first_file = next((f for f in files if Path(f).exists()), None)
        if not first_file:
            print("ERROR: no valid files found")
            sys.exit(1)
        wrong_str = read_shooting_time(first_file)
        print(f"Auto-detected current time: {_fmt_ts(wrong_str)}")

    else:
        validate_timestamp(wrong_str)

    if wrong_str == correct_str:
        print(f"Shooting Time is already {_fmt_ts(correct_str)} — nothing to do.")
        return

    print(f"Current Shooting Time: {_fmt_ts(wrong_str)}")
    print(f"Correct Shooting Time: {_fmt_ts(correct_str)}\n")

    for filepath in files:
        if not Path(filepath).exists():
            print(f"  {filepath}: FILE NOT FOUND — skipping\n")
            continue

        print(f"--- {Path(filepath).name} ---")
        success = patch_file(filepath, wrong_str, correct_str, dry_run=dry_run)
        print("  DONE\n" if success else "  FAILED\n")


if __name__ == "__main__":
    main()
