# The `.gma` (GMAD) file format

Documented from the stock `gmad.exe` distributed with Garry's Mod ("Garry's
Mod Addon Creator 1.1", `garrysmod.main` x86-64, 2026-06) and cross-checked
against a 60-file GMAD v3 archive. The research archive is not included here
because it contains third-party game assets.

Everything is **little-endian**. Strings are **NUL-terminated UTF-8**, no length
prefix. There is no compression and no alignment/padding anywhere.

## Layout

```
+--------------------------------------------------------------+
| Header                                                        |
| Addon metadata                                                |
| File index (names + sizes + CRCs)                             |
| File contents, concatenated in index order                    |
| uint32 addon CRC32 of everything above                        |
+--------------------------------------------------------------+
```

### Header

| Offset | Type       | Field       | Notes                                        |
|-------:|------------|-------------|----------------------------------------------|
| 0x00   | char[4]    | ident       | `"GMAD"`                                     |
| 0x04   | uint8      | version     | `3` (current). `1` and `2` still readable    |
| 0x05   | uint64     | steamid     | always `0` — gmad never fills this in        |
| 0x0D   | uint64     | timestamp   | unix time of packing                         |
| 0x15   | string[]   | required content | **version > 1 only**; empty-string terminated |
| …      | string     | title       | addon name                                   |
| …      | string     | description | see below — a JSON blob, not a plain sentence |
| …      | string     | author      | gmad hardcodes `"Author Name"`               |
| …      | int32      | addon version | always `1`, unused                         |

For new archives, gmad emits no required-content entries, only the terminating
`0x00` byte.

### The `description` field

gmad does **not** copy `addon.json` verbatim. It re-emits a JSON document with
exactly three keys — `description`, `type`, `tags` — tab-indented, `\n` newlines.
`title` and `ignore` are dropped (title already has its own field). This is what
GMod's Lua reads back for the addon browser:

```json
{
	"description": "Description",
	"type": "model",
	"tags": [
		"cartoon",
		"fun"
	]
}
```

If `addon.json` has no `description` key, gmad writes the literal string
`"Description"`.

### File index

Repeats until a file number of `0` is read:

| Type   | Field       | Notes                                                   |
|--------|-------------|---------------------------------------------------------|
| uint32 | file number | 1-based, increments by 1; `0` terminates the index      |
| string | path        | forward slashes, **lowercased**, relative to addon root |
| int64  | size        | signed 64-bit                                           |
| uint32 | crc32       | zlib/PKZIP CRC32 of the file's bytes (`0` with `-nocrc`)|

### File contents

Immediately after the terminating `uint32 0`, the raw bytes of every file are
concatenated **in index order**, with no separators. A file's absolute offset is
therefore `index_end + sum(sizes of preceding entries)`.

The order is *not* required to be alphabetical — gmad emits whatever order the
filesystem enumeration gives it. The only hard rule is that the content order
matches the index order.

### Trailer

The final 4 bytes are a `uint32` CRC32 of the entire file up to (not including)
those 4 bytes. `-nocrc` writes `0` here too. GMod tolerates `0`.

## Whitelist (the reason for this tool)

`gmad.exe create` refuses any file not matched by its built-in wildcard table
and prints `[Not allowed by whitelist]`. A condensed representation follows;
entries beginning with `!` are blacklist overrides that win over a match. The
complete observed table is preserved as `WHITELIST` in `gmapack.py`.

```
lua/*.lua                       scenes/*.vcd
particles/*.pcf                 resource/fonts/*.ttf
scripts/vehicles/*.txt          resource/localization/*/*.properties
maps/*.bsp  maps/*.lmp  maps/*.nav  maps/*.ain  maps/thumb/*.png
sound/*.wav  sound/*.mp3  sound/*.ogg
materials/*.vmt  materials/*.vtf  materials/*.png  materials/*.jpg  materials/*.jpeg
materials/colorcorrection/*.raw
models/*.mdl  models/*.phy  models/*.ani  models/*.vvd  models/*.vtx
!models/*.sw.vtx  !models/*.360.vtx  !models/*.xbox.vtx
gamemodes/*/*.txt   !gamemodes/*/*/*.txt
gamemodes/*/*.fgd   !gamemodes/*/*/*.fgd
gamemodes/*/logo.png  gamemodes/*/icon24.png
gamemodes/*/gamemode/*.lua
gamemodes/*/entities/{effects,weapons,entities}/*.lua
gamemodes/*/backgrounds/*.{png,jpg,jpeg}
gamemodes/*/content/...   (mirrors every rule above)
data_static/*.txt  data_static/*.dat  data_static/*.json
data_static/*.xml  data_static/*.csv
shaders/fxc/*.vcs
*/.DS_Store                     (ignored, not packed)
```

`*` matches across `/`, so `models/*.mdl` also matches `models/a/b/c.mdl`.

The whitelist is enforced **only in the writer**. In local testing, the stock
`gmad.exe` extractor accepted synthetic archives containing non-whitelisted
paths. Whether Garry's Mod or the Workshop accepts or uses such content is a
separate question.

## `addon.json` fields validated by gmad

- `title` — required, non-empty
- `description` — optional
- `type` — one of `gamemode, map, weapon, vehicle, npc, entity, tool, effects, model, servercontent`
- `tags` — max **2**, from `fun, roleplay, scenic, movie, realism, cartoon, water, comic, build`
- `ignore` — array of wildcards; matching files are skipped

## gmad.exe CLI

```
gmad.exe create  -folder path/to/folder [-out path/to/gma.gma] [-nocrc] [-quiet]
gmad.exe extract -file   path/to/gma.gma [-out path/to/folder]
```

## Workshop variant

Files uploaded via `gmpublish.exe` are LZMA-compressed (raw 5-byte props +
8-byte size header, no `.xz`/`.7z` container) with the GMAD stream inside.
`gmapack.py` detects and decompresses this transparently on read.

## Research provenance

This document records format behavior and constants observed while studying a
locally installed copy of the stock Garry's Mod tooling and comparing its output
with locally generated archives. This repository does not include `gmad.exe`,
game binaries, Workshop downloads, or the third-party addon used during the
original investigation. Automated tests use only small synthetic fixtures.
