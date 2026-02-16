# instatimefixer

Fix the "Shooting Time" in Insta360 video files when your camera recorded with the wrong date.

## The Problem

When an Insta360 camera (e.g. **GO Ultra**) records video without first syncing to the Insta360 app on your phone, it may stamp files with the wrong date and time. The "Shooting Time" displayed in Insta360 Studio comes from **proprietary metadata** inside a custom `inst` MP4 box that standard tools like ExifTool cannot fully modify.

Even if you fix the standard EXIF/MP4 metadata with ExifTool, Insta360 Studio will still show the wrong Shooting Time — and stats overlays will use the wrong date.

## The Solution

This script performs **in-place binary patching** of all the timestamp fields that Insta360 Studio reads from, across both the MP4 and LRV files:

| Location | Format | Description |
|---|---|---|
| `mvhd`, `tkhd`, `mdhd` | uint32 BE (seconds since 1904) | Standard MP4 atom timestamps |
| Protobuf field 7 | Varint (YYYYMMDDHHmmss) | Insta360 `creation_time` in `inst` box |
| Protobuf field 36 | Varint (ms since epoch) | `first_gps_timestamp` in `inst` box |
| Embedded file paths | ASCII | Internal path strings containing the date |
| Embedded filename | UTF-16LE | Filename in `inst` box header |

All patches are **the same byte length** as the originals — no file structure is altered, no re-encoding happens.

## Requirements

- Python 3.10+
- No external dependencies

## Usage

### Fix Shooting Time (auto-detect current wrong time)

```bash
python3 instatimefixer.py VID_*.mp4 LRV_*.lrv 20260215143800
```

The script reads the current (wrong) timestamp from the first file automatically. You only need to provide the **correct** time in `YYYYMMDDHHmmss` format.

### Fix Shooting Time (explicit wrong and correct times)

```bash
python3 instatimefixer.py VID_*.mp4 LRV_*.lrv 20260103194656 20260215143800
```

### Preview changes without writing (dry run)

```bash
python3 instatimefixer.py --dry-run VID_*.mp4 LRV_*.lrv 20260215143800
```

### Read the current Shooting Time

```bash
python3 instatimefixer.py --read VID_*.mp4 LRV_*.lrv
```

## Time Format

Times are 14 digits, no separators, 24-hour clock:

```
YYYYMMDDHHmmss
```

| Part | Digits | Example |
|---|---|---|
| Year | YYYY | 2026 |
| Month | MM | 02 |
| Day | DD | 15 |
| Hour | HH | 14 |
| Minute | mm | 38 |
| Second | ss | 00 |

So `2026-02-15 14:38:00` becomes `20260215143800`.

## Important Notes

- **Back up your files first.** This tool writes directly to the files.
- **Patch both the MP4 and LRV files.** Insta360 Studio reads metadata from both. Pass them all in one command.
- The script modifies files **in-place** — it does not create copies or backups.
- Tested with the **Insta360 GO Ultra**. Should work with other Insta360 cameras that use the same `inst` box format, but YMMV.

## How It Works

Insta360 cameras embed a proprietary `inst` MP4 box at the end of each video file. Inside this box, metadata is stored as a protobuf-encoded binary blob. The "Shooting Time" shown in Insta360 Studio is derived from a combination of:

1. The standard MP4 `mvhd`/`tkhd`/`mdhd` creation timestamps
2. A protobuf `creation_time` field (varint-encoded as the integer `YYYYMMDDHHmmss`)
3. A `first_gps_timestamp` (milliseconds since Unix epoch)
4. ASCII and UTF-16LE filename strings embedded in the metadata

This script finds each of these by scanning for known byte patterns (protobuf field tags, MP4 box type codes, date strings), verifies the existing bytes match what's expected, and overwrites them with the corrected values. The replacement values are always the same byte length, so the file structure is never corrupted.

## License

MIT
