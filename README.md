# GMA File Packer

A dependency-free Python command-line tool for creating, extracting, listing,
and verifying Garry's Mod `.gma` archives (GMAD format version 3).

The stock `gmad.exe` creator accepts only a built-in list of file types. This
tool packs every file by default, while `--strict` is available when you want
the stock whitelist behavior.

> [!IMPORTANT]
> Producing a valid GMAD archive does not guarantee that Garry's Mod or the
> Steam Workshop will accept, mount, or use every file in it. This tool changes
> archive creation behavior; it does not bypass Workshop publishing rules.

## Requirements

- Python 3.10 or newer
- No third-party packages

## Quick start

Create an archive:

```console
python gmapack.py create --folder "path/to/addon" --out "addon.gma"
```

Extract an archive:

```console
python gmapack.py extract --file "addon.gma" --out "extracted-addon"
```

Inspect or verify an archive:

```console
python gmapack.py list --file "addon.gma"
python gmapack.py verify --file "addon.gma"
```

On Windows, you can also drag an addon folder or a `.gma` file onto
`gmapack.bat`. The batch file uses the `python` command or Windows Python
launcher and pauses so you can read the result.

Run `python gmapack.py --help` or append `--help` to a subcommand for the full
option list.

## Creating archives

An addon folder may contain an `addon.json` file with the usual `title`,
`description`, `type`, `tags`, and `ignore` fields. `addon.json` supplies
archive metadata but is not itself packed.

Because the default mode intentionally packs non-whitelisted files, review the
source folder before publishing. Apart from `addon.json`, Git internals, and
common operating-system metadata, hidden files and local configuration are also
included unless they match `addon.json`'s `ignore` list or an `--exclude` glob.
Symbolic links and directory junctions are refused so a package cannot silently
pull content from outside its source tree.

Useful creation flags:

| Flag | Purpose |
|---|---|
| `--strict` | Reject files that stock `gmad.exe` would reject |
| `--warn-unlisted` | Report non-whitelisted files but still pack them |
| `--exclude GLOB` | Exclude a matching path; repeat as needed |
| `--nocrc` | Write zero for file and archive CRC fields |
| `--preserve-case` | Keep source path casing instead of lowercasing it |
| `--timestamp N` | Use a fixed Unix timestamp for reproducible output |
| `--force` | Replace an existing output archive |

Paths are sorted before packing, so using a fixed timestamp makes repeated
builds from unchanged input deterministic.

## Reading archives

The reader accepts ordinary GMAD streams and the LZMA-wrapped form commonly
encountered in downloaded Workshop content. Decoding is streamed through a
bounded in-memory spool that automatically spills larger archives to a temporary
file; LZMA dictionaries above 256 MiB are rejected.

Extraction rejects absolute paths, parent-directory traversal, duplicate paths,
linked-path escapes, and paths that resolve outside the requested destination.
It also refuses to replace existing files unless `--force` is supplied, writes
through temporary files, validates the whole-archive CRC before extraction, and
checks each stored file CRC before replacement. Verification checks every
nonzero per-file CRC and the whole-archive CRC.

## Development

Run the standard-library test suite from the repository root:

```console
python -m unittest discover -s tests -v
```

The tests create small synthetic addons and archives in temporary directories;
no game content is required.

See [GMA_FORMAT.md](GMA_FORMAT.md) for the documented binary layout and stock
whitelist behavior.

## Project scope

This repository contains only the tool and synthetic test data. Research
archives and third-party addon assets are intentionally excluded from Git
history.

This is an unofficial community project and is not affiliated with Facepunch
Studios, Valve, Garry's Mod, or Steam. Only package and distribute content you
have the right to use.
