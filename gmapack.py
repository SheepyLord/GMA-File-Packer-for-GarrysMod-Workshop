#!/usr/bin/env python3
"""
gmapack.py - Garry's Mod .gma reader/writer with an optional whitelist.

Format-compatible with GarrysMod/bin/gmad.exe (GMAD format version 3), except
that `create` packs every regular file instead of only whitelisted extensions.

Usage
  python gmapack.py create  -folder <dir> [-out <file.gma>] [options]
  python gmapack.py extract -file <file.gma> [-out <dir>]
  python gmapack.py list    -file <file.gma>
  python gmapack.py verify  -file <file.gma>

  # drag-n-drop: a folder is packed, a .gma is extracted
  python gmapack.py <path>

Create options
  -out PATH          output .gma (default: <folder>.gma next to the folder)
  --title S          addon title      (default: addon.json "title", else folder name)
  --desc S           description      (default: addon.json "description", else "Description")
  --type S           addon type       (default: addon.json "type", else "servercontent")
  --tag S            tag, repeatable  (default: addon.json "tags")
  --author S         author name      (default: "Author Name" - gmad always writes this)
  --steamid N        SteamID64 field  (default: 0, same as gmad)
  --timestamp N      unix timestamp   (default: now)
  --require S        "required content" entry, repeatable (default: none)
  --exclude GLOB     skip matching files, repeatable (added to addon.json "ignore")
  --strict           enforce gmad's whitelist (refuse non-whitelisted files)
  --warn-unlisted    just print which files gmad.exe would have rejected
  --nocrc            write 0 for every CRC (gmad's -nocrc)
  --preserve-case    keep filename case (default: lowercase, like gmad)
  --force            replace an existing output archive
  --quiet

Extract options
  -out PATH          output folder (default: archive name without .gma)
  --force            replace files already present in the output folder
  --quiet
"""

import argparse
import json
import os
import stat
import struct
import sys
import tempfile
import time
import zlib

IDENT = b"GMAD"
VERSION = 3
TAG_LIMIT = 2
COPY_BUFFER_SIZE = 1 << 20
DECOMPRESS_BUFFER_SIZE = 4 << 20
MAX_LZMA_DICTIONARY_SIZE = 256 << 20
MAX_STRING_SIZE = 16 << 20

ADDON_TYPES = [
    "gamemode", "map", "weapon", "vehicle", "npc",
    "entity", "tool", "effects", "model", "servercontent",
]
ADDON_TAGS = [
    "fun", "roleplay", "scenic", "movie", "realism",
    "cartoon", "water", "comic", "build",
]

# Exactly the wildcard table inside gmad.exe (v3, 2026-06 build).
# A leading '!' entry is a blacklist override.
WHITELIST = [
    "lua/*.lua",
    "scenes/*.vcd",
    "particles/*.pcf",
    "resource/fonts/*.ttf",
    "scripts/vehicles/*.txt",
    "resource/localization/*/*.properties",
    "maps/*.bsp", "maps/*.lmp", "maps/*.nav", "maps/*.ain", "maps/thumb/*.png",
    "sound/*.wav", "sound/*.mp3", "sound/*.ogg",
    "materials/*.vmt", "materials/*.vtf", "materials/*.png",
    "materials/*.jpg", "materials/*.jpeg",
    "materials/colorcorrection/*.raw",
    "models/*.mdl", "models/*.phy", "models/*.ani",
    "models/*.vvd", "models/*.vtx",
    "!models/*.sw.vtx", "!models/*.360.vtx", "!models/*.xbox.vtx",
    "gamemodes/*/*.txt", "!gamemodes/*/*/*.txt",
    "gamemodes/*/*.fgd", "!gamemodes/*/*/*.fgd",
    "gamemodes/*/logo.png", "gamemodes/*/icon24.png",
    "gamemodes/*/gamemode/*.lua",
    "gamemodes/*/entities/effects/*.lua",
    "gamemodes/*/entities/weapons/*.lua",
    "gamemodes/*/entities/entities/*.lua",
    "gamemodes/*/backgrounds/*.png",
    "gamemodes/*/backgrounds/*.jpg",
    "gamemodes/*/backgrounds/*.jpeg",
    "gamemodes/*/content/models/*.mdl",
    "gamemodes/*/content/models/*.phy",
    "gamemodes/*/content/models/*.ani",
    "gamemodes/*/content/models/*.vvd",
    "gamemodes/*/content/models/*.vtx",
    "!gamemodes/*/content/models/*.sw.vtx",
    "!gamemodes/*/content/models/*.360.vtx",
    "!gamemodes/*/content/models/*.xbox.vtx",
    "gamemodes/*/content/materials/*.vmt",
    "gamemodes/*/content/materials/*.vtf",
    "gamemodes/*/content/materials/*.png",
    "gamemodes/*/content/materials/*.jpg",
    "gamemodes/*/content/materials/*.jpeg",
    "gamemodes/*/content/materials/colorcorrection/*.raw",
    "gamemodes/*/content/scenes/*.vcd",
    "gamemodes/*/content/particles/*.pcf",
    "gamemodes/*/content/resource/fonts/*.ttf",
    "gamemodes/*/content/scripts/vehicles/*.txt",
    "gamemodes/*/content/resource/localization/*/*.properties",
    "gamemodes/*/content/maps/*.bsp",
    "gamemodes/*/content/maps/*.nav",
    "gamemodes/*/content/maps/*.ain",
    "gamemodes/*/content/maps/thumb/*.png",
    "gamemodes/*/content/sound/*.wav",
    "gamemodes/*/content/sound/*.mp3",
    "gamemodes/*/content/sound/*.ogg",
    "data_static/*.txt", "data_static/*.dat", "data_static/*.json",
    "data_static/*.xml", "data_static/*.csv",
    "shaders/fxc/*.vcs",
]

# Never packed, regardless of settings.
ALWAYS_IGNORE = [
    "addon.json", ".ds_store", "*/.ds_store", "thumbs.db", "*/thumbs.db",
    "desktop.ini", "*/desktop.ini", ".git", "*/.git", ".git/*", "*/.git/*",
    ".gmapack-*.part", "*/.gmapack-*.part",
]


def _configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


def display_text(value, allow_newlines=False):
    rendered = []
    for character in str(value):
        if character == "\n" and allow_newlines:
            rendered.append(character)
        elif character.isprintable():
            rendered.append(character)
        else:
            codepoint = ord(character)
            if codepoint <= 0xFF:
                rendered.append("\\x%02x" % codepoint)
            elif codepoint <= 0xFFFF:
                rendered.append("\\u%04x" % codepoint)
            else:
                rendered.append("\\U%08x" % codepoint)
    return "".join(rendered)


# ---------------------------------------------------------------- wildcards --

def wildcard(pattern, text):
    """Source-engine style match: '*' matches anything, including '/'."""
    p = i = 0
    star = -1
    mark = 0
    while i < len(text):
        if p < len(pattern) and pattern[p] == "*":
            star = p
            p += 1
            mark = i
        elif p < len(pattern) and (pattern[p] == text[i] or pattern[p] == "?"):
            p += 1
            i += 1
        elif star >= 0:
            p = star + 1
            mark += 1
            i = mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def whitelisted(path):
    path = path.lower()
    allowed = False
    for pat in WHITELIST:
        if pat.startswith("!"):
            if wildcard(pat[1:], path):
                return False
        elif wildcard(pat, path):
            allowed = True
    return allowed


# ------------------------------------------------------------- binary io ----

def w_str(out, s):
    if not isinstance(s, str):
        sys.exit("error: archive metadata must be text")
    if "\0" in s:
        sys.exit("error: archive strings cannot contain NUL bytes")
    encoded = s.encode("utf-8")
    if len(encoded) > MAX_STRING_SIZE:
        sys.exit("error: archive string exceeds the %d-byte safety limit"
                 % MAX_STRING_SIZE)
    out.write(encoded + b"\0")


def read_exact(f, size, field):
    data = f.read(size)
    if len(data) != size:
        sys.exit("error: truncated archive while reading %s" % field)
    return data


def r_str(f, field="string"):
    buf = bytearray()
    while True:
        c = f.read(1)
        if not c:
            sys.exit("error: truncated archive while reading %s" % field)
        if c == b"\0":
            break
        buf += c
        if len(buf) > MAX_STRING_SIZE:
            sys.exit("error: %s exceeds the %d-byte safety limit"
                     % (field, MAX_STRING_SIZE))
    return buf.decode("utf-8", "replace")


class Crc32Writer:
    """Wraps a file object, running a CRC32 over everything written."""

    def __init__(self, fh):
        self.fh = fh
        self.crc = 0

    def write(self, data):
        self.crc = zlib.crc32(data, self.crc)
        return self.fh.write(data)

    def tell(self):
        return self.fh.tell()


# ------------------------------------------------------------------ create --

def build_description(desc, atype, tags):
    """gmad rebuilds addon.json into this exact shape for the description field."""
    parts = ['{\n\t"description": %s' % json.dumps(desc)]
    parts.append('\t"type": %s' % json.dumps(atype))
    if tags:
        body = ",\n".join('\t\t%s' % json.dumps(t) for t in tags)
        parts.append('\t"tags": [\n%s\n\t]' % body)
    else:
        parts.append('\t"tags": [\n\t]')
    return ",\n".join(parts) + "\n}"


def _raise_walk_error(error):
    raise error


def _is_link(path):
    return (os.path.islink(path) or
            (hasattr(os.path, "isjunction") and os.path.isjunction(path)))


def _inside(root, path):
    try:
        return (os.path.normcase(os.path.commonpath((root, path))) ==
                os.path.normcase(root))
    except ValueError:
        return False


def _windows_reserved(part):
    if hasattr(os.path, "isreserved") and os.path.isreserved(part):
        return True
    if part != part.rstrip(" ."):
        return True
    if any(ord(character) < 32 for character in part):
        return True
    if any(character in '<>:"|?*' for character in part):
        return True
    base = part.split(".", 1)[0].upper()
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    devices.update("COM%s" % suffix for suffix in "123456789¹²³")
    devices.update("LPT%s" % suffix for suffix in "123456789¹²³")
    return base in devices


def gather(folder, ignores, lowercase=True, skip_paths=None):
    """-> [(path_in_gma, absolute_path, size)] sorted by path."""
    out = []
    seen = {}
    real_root = os.path.realpath(folder)
    skipped = {
        os.path.normcase(os.path.realpath(os.path.abspath(path)))
        for path in (skip_paths or [])
    }
    for root, dirs, names in os.walk(folder, onerror=_raise_walk_error):
        dirs.sort()
        for dirname in dirs:
            directory = os.path.join(root, dirname)
            if _is_link(directory):
                sys.exit("error: refusing linked directory: %s"
                         % display_text(os.path.relpath(directory, folder)))
        for n in sorted(names):
            full = os.path.join(root, n)
            real_full = os.path.realpath(os.path.abspath(full))
            if os.path.normcase(real_full) in skipped:
                continue
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            key = rel.lower()
            if any(wildcard(p, key) for p in ignores):
                continue
            if _is_link(full):
                sys.exit("error: refusing linked file: %s" % display_text(rel))
            if not _inside(real_root, real_full):
                sys.exit("error: refusing file outside addon root: %s"
                         % display_text(rel))
            archive_path = key if lowercase else rel
            duplicate_key = archive_path.casefold()
            if duplicate_key in seen:
                sys.exit("error: paths collide in the archive: %s and %s"
                         % (display_text(seen[duplicate_key]), display_text(rel)))
            seen[duplicate_key] = rel
            try:
                file_stat = os.stat(full, follow_symlinks=False)
            except OSError as err:
                sys.exit("error: cannot read %s: %s" % (display_text(rel), err))
            if not stat.S_ISREG(file_stat.st_mode):
                sys.exit("error: refusing non-regular file: %s"
                         % display_text(rel))
            size = file_stat.st_size
            out.append((archive_path, full, size))
    out.sort(key=lambda x: x[0])
    return out


def file_crc(path):
    crc = 0
    with open(path, "rb") as fh:
        while True:
            b = fh.read(COPY_BUFFER_SIZE)
            if not b:
                break
            crc = zlib.crc32(b, crc)
    return crc & 0xFFFFFFFF


def _write_archive(raw, args, files, timestamp, title, desc, atype, tags):
    total = 0
    expected_crcs = {}
    out = Crc32Writer(raw)
    out.write(IDENT)
    out.write(bytes([VERSION]))
    out.write(struct.pack("<Q", args.steamid))
    out.write(struct.pack("<Q", timestamp))
    for required in (args.require or []):
        w_str(out, required)
    out.write(b"\0")
    w_str(out, title)
    w_str(out, build_description(desc, atype, tags))
    w_str(out, args.author)
    out.write(struct.pack("<i", 1))

    for number, (rel, full, size) in enumerate(files, 1):
        crc = 0 if args.nocrc else file_crc(full)
        expected_crcs[full] = crc
        out.write(struct.pack("<I", number))
        w_str(out, rel)
        out.write(struct.pack("<q", size))
        out.write(struct.pack("<I", crc))
    out.write(struct.pack("<I", 0))

    for rel, full, size in files:
        written = 0
        copied_crc = 0
        with open(full, "rb") as source:
            while True:
                block = source.read(COPY_BUFFER_SIZE)
                if not block:
                    break
                out.write(block)
                written += len(block)
                copied_crc = zlib.crc32(block, copied_crc)
        if written != size:
            sys.exit("error: %s changed size while packing" % display_text(rel))
        copied_crc &= 0xFFFFFFFF
        if not args.nocrc and copied_crc != expected_crcs[full]:
            sys.exit("error: %s changed contents while packing" % display_text(rel))
        total += written
        if not args.quiet:
            print("  %-70s %10d" % (display_text(rel)[:70], size))

    out.write(struct.pack("<I", 0 if args.nocrc else (out.crc & 0xFFFFFFFF)))
    return total


def _publish_file(temp_path, out_path, force):
    if force:
        os.replace(temp_path, out_path)
        return
    try:
        if os.name == "nt":
            os.rename(temp_path, out_path)
            return
        os.link(temp_path, out_path)
    except FileExistsError:
        sys.exit("error: output already exists (use --force): %s"
                 % display_text(out_path))
    os.remove(temp_path)


def create(args):
    if not args.folder:
        sys.exit("error: addon folder is empty")
    folder = os.path.abspath(os.path.normpath(args.folder))
    if not os.path.isdir(folder):
        sys.exit("error: not a folder: %s" % display_text(folder))

    meta = {}
    aj = os.path.join(folder, "addon.json")
    if os.path.lexists(aj) and _is_link(aj):
        sys.exit("error: refusing linked metadata file: addon.json")
    if os.path.isfile(aj):
        try:
            with open(aj, "r", encoding="utf-8-sig") as meta_file:
                meta = json.load(meta_file)
        except Exception as err:
            sys.exit("error: addon.json is not valid JSON: %s" % err)
        if not isinstance(meta, dict):
            sys.exit("error: addon.json must contain a JSON object")

    title = args.title or meta.get("title") or os.path.basename(folder)
    desc = args.desc or meta.get("description") or "Description"
    atype = args.type or meta.get("type") or "servercontent"
    raw_tags = args.tag if args.tag is not None else meta.get("tags", [])
    raw_ignores = meta.get("ignore", [])

    for field, value in (("title", title), ("description", desc),
                         ("type", atype)):
        if not isinstance(value, str):
            sys.exit("error: addon.json %s must be a string" % field)
    if not isinstance(raw_tags, list) or not all(
            isinstance(tag, str) for tag in raw_tags):
        sys.exit("error: addon.json tags must be an array of strings")
    if not isinstance(raw_ignores, list) or not all(
            isinstance(pattern, str) for pattern in raw_ignores):
        sys.exit("error: addon.json ignore must be an array of strings")

    atype = atype.lower()
    tags = [tag.lower() for tag in raw_tags]

    if not title:
        sys.exit("error: title is empty!")
    if atype not in ADDON_TYPES:
        sys.exit("error: type isn't a supported type! (%s)" % ", ".join(ADDON_TYPES))
    if len(tags) > TAG_LIMIT:
        sys.exit("error: too many tags - specify %d only!" % TAG_LIMIT)
    for t in tags:
        if t not in ADDON_TAGS:
            sys.exit("error: tag '%s' isn't supported! (%s)" % (t, ", ".join(ADDON_TAGS)))

    ignores = list(ALWAYS_IGNORE)
    ignores += [pattern.lower() for pattern in raw_ignores]
    ignores += [p.lower() for p in (args.exclude or [])]

    for required in (args.require or []):
        if not required:
            sys.exit("error: required-content entries cannot be empty")
        if "\0" in required:
            sys.exit("error: required-content entries cannot contain NUL bytes")

    if not 0 <= args.steamid <= 0xFFFFFFFFFFFFFFFF:
        sys.exit("error: steamid must fit in an unsigned 64-bit integer")
    ts = args.timestamp if args.timestamp is not None else int(time.time())
    if not 0 <= ts <= 0xFFFFFFFFFFFFFFFF:
        sys.exit("error: timestamp must fit in an unsigned 64-bit integer")

    out_path = os.path.abspath(args.out or (folder + ".gma"))
    if os.path.lexists(out_path) and not args.force:
        sys.exit("error: output already exists (use --force): %s"
                 % display_text(out_path))
    files = gather(folder, ignores, lowercase=not args.preserve_case,
                   skip_paths=(out_path,))
    if not files:
        sys.exit("error: no files to pack")
    if len(files) > 0xFFFFFFFF:
        sys.exit("error: too many files for the GMAD index")
    for rel, _, size in files:
        if size > 0x7FFFFFFFFFFFFFFF:
            sys.exit("error: file is too large for GMAD: %s" % display_text(rel))

    if args.strict or args.warn_unlisted:
        bad = [f for f in files if not whitelisted(f[0])]
        if bad:
            for p, _, _ in bad:
                print("  [Not allowed by whitelist] %s" % display_text(p))
            if args.strict:
                sys.exit("error: %d file(s) rejected by whitelist (--strict)" % len(bad))
            print("  note: %d file(s) gmad.exe would reject - packing anyway" % len(bad))

    output_dir = os.path.dirname(out_path)
    fd, temp_path = tempfile.mkstemp(
        prefix=".gmapack-", suffix=".part", dir=output_dir)
    try:
        raw = os.fdopen(fd, "wb")
        fd = None
        with raw:
            total = _write_archive(
                raw, args, files, ts, title, desc, atype, tags)
        _publish_file(temp_path, out_path, args.force)
        temp_path = None
    finally:
        if fd is not None:
            os.close(fd)
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
    print("Wrote %s" % display_text(out_path))
    print("  %d files, %d bytes of content, %d bytes total"
          % (len(files), total, os.path.getsize(out_path)))


# ------------------------------------------------------------------- read ---

class Gma(object):
    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _open_gma(path):
    """Returns a seekable binary stream; transparently handles LZMA-wrapped gma."""
    fh = open(path, "rb")
    prefix = fh.read(4)
    if prefix == IDENT:
        fh.seek(0)
        return fh
    header = prefix + fh.read(9)
    import lzma
    if len(header) < 13:
        fh.close()
        sys.exit("error: not a GMAD file (and not LZMA-compressed)")
    properties = header[0]
    if properties >= 9 * 5 * 5:
        fh.close()
        sys.exit("error: invalid LZMA properties")
    lc = properties % 9
    remainder = properties // 9
    lp = remainder % 5
    pb = remainder // 5
    dictionary_size = struct.unpack("<I", header[1:5])[0]
    if dictionary_size > MAX_LZMA_DICTIONARY_SIZE:
        fh.close()
        sys.exit("error: LZMA dictionary exceeds the %d MiB safety limit"
                 % (MAX_LZMA_DICTIONARY_SIZE >> 20))
    expected_size = struct.unpack("<Q", header[5:13])[0]
    filters = [{
        "id": lzma.FILTER_LZMA1,
        "dict_size": dictionary_size,
        "lc": lc,
        "lp": lp,
        "pb": pb,
    }]
    output = tempfile.SpooledTemporaryFile(max_size=8 << 20, mode="w+b")
    unknown_size = 0xFFFFFFFFFFFFFFFF
    total = 0
    try:
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
        stream_done = False
        while not stream_done:
            compressed = fh.read(COPY_BUFFER_SIZE)
            if not compressed:
                break
            while True:
                limit = DECOMPRESS_BUFFER_SIZE
                if expected_size != unknown_size:
                    limit = min(limit, expected_size - total + 1)
                block = dec.decompress(compressed, max_length=max(1, limit))
                compressed = b""
                total += len(block)
                if expected_size != unknown_size and total > expected_size:
                    sys.exit("error: LZMA output exceeds its declared size")
                output.write(block)
                if dec.eof:
                    if dec.unused_data or fh.read(1):
                        sys.exit("error: trailing data after the LZMA stream")
                    stream_done = True
                    break
                if dec.needs_input:
                    break
    except (lzma.LZMAError, ValueError):
        output.close()
        sys.exit("error: not a GMAD file (and not valid LZMA data)")
    except BaseException:
        output.close()
        raise
    finally:
        fh.close()
    if expected_size != unknown_size and total != expected_size:
        output.close()
        sys.exit("error: LZMA size mismatch (expected %d, got %d)"
                 % (expected_size, total))
    if expected_size == unknown_size and not dec.eof:
        output.close()
        sys.exit("error: truncated LZMA stream")
    output.seek(0)
    if output.read(4) != IDENT:
        output.close()
        sys.exit("error: not a GMAD file")
    output.seek(0)
    print("  (input was LZMA-compressed, using a temporary decoded stream)")
    return output


def _stream_size(f):
    pos = f.tell()
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(pos)
    return size


def _require_payload(g, include_trailer=False):
    expected = g.data_end + (4 if include_trailer else 0)
    if g.archive_size < expected:
        missing = expected - g.archive_size
        g.close()
        sys.exit("error: truncated archive (%d byte(s) missing)" % missing)


def _calculate_addon_crc(g):
    g.fh.seek(g.data_end)
    trailer = read_exact(g.fh, 4, "addon CRC")
    tail_size = g.archive_size - (g.data_end + 4)
    g.fh.seek(0)
    crc, left = 0, g.data_end
    while left:
        block = read_exact(g.fh, min(1 << 22, left), "archive payload")
        left -= len(block)
        crc = zlib.crc32(block, crc)
    return struct.unpack("<I", trailer)[0], crc & 0xFFFFFFFF, tail_size


def read_gma(path):
    f = _open_gma(path)
    try:
        return _read_gma_stream(f)
    except BaseException:
        f.close()
        raise


def _read_gma_stream(f):
    g = Gma()
    g.fh = f
    if read_exact(f, 4, "archive identifier") != IDENT:
        sys.exit("error: bad ident, not a .gma")
    g.version = read_exact(f, 1, "format version")[0]
    if g.version not in (1, 2, 3):
        sys.exit("error: unsupported GMAD version %d" % g.version)
    g.steamid = struct.unpack(
        "<Q", read_exact(f, 8, "SteamID64 field"))[0]
    g.timestamp = struct.unpack(
        "<Q", read_exact(f, 8, "timestamp"))[0]
    g.required = []
    if g.version > 1:
        while True:
            s = r_str(f, "required-content entry")
            if s == "":
                break
            g.required.append(s)
    g.title = r_str(f, "title")
    g.description = r_str(f, "description")
    g.author = r_str(f, "author")
    g.addon_version = struct.unpack(
        "<i", read_exact(f, 4, "addon version"))[0]
    g.files = []
    seen_paths = set()
    while True:
        num = struct.unpack("<I", read_exact(f, 4, "file number"))[0]
        if num == 0:
            break
        name = r_str(f, "file name")
        if not name:
            sys.exit("error: archive contains an empty file name")
        name_key = name.replace("\\", "/").casefold()
        if name_key in seen_paths:
            sys.exit("error: archive contains a duplicate path: %s"
                     % display_text(name))
        seen_paths.add(name_key)
        size = struct.unpack(
            "<q", read_exact(f, 8, "file size"))[0]
        if size < 0:
            sys.exit("error: archive contains a negative file size: %s"
                     % display_text(name))
        crc = struct.unpack(
            "<I", read_exact(f, 4, "file CRC"))[0]
        g.files.append({"num": num, "name": name, "size": size, "crc": crc})
    g.data_offset = f.tell()
    off = g.data_offset
    for e in g.files:
        e["offset"] = off
        off += e["size"]
    g.data_end = off
    g.archive_size = _stream_size(f)
    return g


def cmd_list(args):
    g = read_gma(args.file)
    print("ident        GMAD v%d" % g.version)
    print("steamid      %d" % g.steamid)
    try:
        timestamp_text = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(g.timestamp))
    except (OSError, OverflowError, ValueError):
        timestamp_text = "outside this platform's date range"
    print("timestamp    %d (%s)" % (g.timestamp, timestamp_text))
    print("title        %s" % display_text(g.title))
    print("author       %s" % display_text(g.author))
    print("addonversion %d" % g.addon_version)
    if g.required:
        print("required     %s" % ", ".join(display_text(item) for item in g.required))
    description = display_text(g.description, allow_newlines=True)
    print("description  %s" % description.replace("\n", "\n             "))
    print("files        %d" % len(g.files))
    for e in g.files:
        print("  %6d  %12d  %08x  %s"
              % (e["num"], e["size"], e["crc"], display_text(e["name"])))
    print("content      offset %d .. %d" % (g.data_offset, g.data_end))
    g.close()


def cmd_verify(args):
    g = read_gma(args.file)
    _require_payload(g, include_trailer=True)
    bad = 0
    for e in g.files:
        g.fh.seek(e["offset"])
        crc, left = 0, e["size"]
        while left:
            b = read_exact(g.fh, min(COPY_BUFFER_SIZE, left),
                           "contents of %s" % e["name"])
            left -= len(b)
            crc = zlib.crc32(b, crc)
        crc &= 0xFFFFFFFF
        if e["crc"] and crc != e["crc"]:
            bad += 1
            print("  CRC MISMATCH  %s (stored %08x, actual %08x)"
                  % (display_text(e["name"]), e["crc"], crc))
    stored, whole, tail_size = _calculate_addon_crc(g)
    addon_bad = stored not in (0, whole)
    print("addon CRC    stored %08x, actual %08x  %s"
          % (stored, whole, "MISMATCH" if addon_bad else "OK"))
    if tail_size:
        print("warning: %d trailing byte(s) after the addon CRC" % tail_size)
    failures = bad + (1 if addon_bad else 0) + (1 if tail_size else 0)
    print("%d file(s), %d CRC failure(s)" % (len(g.files), failures))
    g.close()
    return 1 if failures else 0


def cmd_extract(args):
    g = read_gma(args.file)
    _require_payload(g, include_trailer=True)
    stored, whole, tail_size = _calculate_addon_crc(g)
    if stored not in (0, whole):
        g.close()
        sys.exit("error: addon CRC mismatch (stored %08x, actual %08x)"
                 % (stored, whole))
    if tail_size:
        g.close()
        sys.exit("error: archive has %d trailing byte(s)" % tail_size)
    out = args.out or os.path.splitext(os.path.abspath(args.file))[0]
    root = os.path.realpath(os.path.abspath(out))
    os.makedirs(root, exist_ok=True)
    refused = 0
    extracted = 0
    for e in g.files:
        rel = e["name"].replace("\\", "/")
        parts = rel.split("/")
        drive, _ = os.path.splitdrive(rel)
        dest = os.path.realpath(os.path.join(root, *parts))
        inside_root = _inside(root, dest)
        unsafe_windows_name = (os.name == "nt" and
                               any(_windows_reserved(part) for part in parts))
        if (not rel or drive or rel.startswith("/") or
                any(part in ("", ".", "..") for part in parts) or
                unsafe_windows_name or not inside_root or
                os.path.normcase(dest) == os.path.normcase(root)):
            print("  ! refusing unsafe archive path: %s" % display_text(rel),
                  file=sys.stderr)
            refused += 1
            continue
        parent = os.path.dirname(dest)
        os.makedirs(parent, exist_ok=True)
        resolved_parent = os.path.realpath(parent)
        if not _inside(root, resolved_parent):
            print("  ! refusing linked output directory: %s" % display_text(rel),
                  file=sys.stderr)
            refused += 1
            continue
        parent = resolved_parent
        dest = os.path.join(parent, os.path.basename(dest))
        if os.path.lexists(dest) and not args.force:
            print("  ! refusing existing output (use --force): %s"
                  % display_text(rel), file=sys.stderr)
            refused += 1
            continue
        g.fh.seek(e["offset"])
        left = e["size"]
        file_crc_value = 0
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=".gmapack-", suffix=".part", dir=parent)
            with os.fdopen(fd, "wb") as o:
                while left:
                    b = read_exact(g.fh, min(COPY_BUFFER_SIZE, left),
                                   "contents of %s" % rel)
                    left -= len(b)
                    file_crc_value = zlib.crc32(b, file_crc_value)
                    o.write(b)
            file_crc_value &= 0xFFFFFFFF
            if e["crc"] and file_crc_value != e["crc"]:
                print("  ! refusing CRC-mismatched file: %s" % display_text(rel),
                      file=sys.stderr)
                refused += 1
                continue
            if (os.path.normcase(os.path.realpath(parent)) !=
                    os.path.normcase(parent)):
                print("  ! refusing changed output directory: %s"
                      % display_text(rel), file=sys.stderr)
                refused += 1
                continue
            _publish_file(temp_path, dest, args.force)
            temp_path = None
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
        extracted += 1
        if not args.quiet:
            print("  %-70s %10d" % (display_text(rel)[:70], e["size"]))
    # rebuild an addon.json so the folder can be repacked
    meta = {}
    try:
        parsed = json.loads(g.description)
        if isinstance(parsed, dict):
            meta.update(parsed)
        else:
            meta["description"] = g.description
    except Exception:
        meta["description"] = g.description
    meta["title"] = g.title
    metadata_path = os.path.join(root, "addon.json")
    metadata_refused = os.path.lexists(metadata_path) and not args.force
    if metadata_refused:
        print("  ! refusing existing output (use --force): addon.json",
              file=sys.stderr)
    else:
        fd, temp_metadata = tempfile.mkstemp(
            prefix=".gmapack-", suffix=".json", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as o:
                json.dump(meta, o, indent="\t")
                o.write("\n")
            _publish_file(temp_metadata, metadata_path, args.force)
            temp_metadata = None
        finally:
            if temp_metadata and os.path.exists(temp_metadata):
                os.remove(temp_metadata)
    print("Extracted %d file(s) to %s" % (extracted, display_text(root)))
    if refused:
        print("Refused %d archive file(s)" % refused, file=sys.stderr)
    g.close()
    return 1 if refused or metadata_refused else 0


# ------------------------------------------------------------------- main ---

def main(argv=None):
    _configure_stdio()
    if argv is None:
        argv = sys.argv[1:]
    ap = argparse.ArgumentParser(
        prog="gmapack",
        description="Create, inspect, verify, and extract Garry's Mod .gma archives")
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("create", help="pack a folder into a .gma")
    c.add_argument("-folder", "--folder", "-f", required=True,
                   help="addon folder to pack")
    c.add_argument("-out", "--out", "-o",
                   help="output archive (default: FOLDER.gma)")
    c.add_argument("--title", help="override the addon title")
    c.add_argument("--desc", "--description", dest="desc",
                   help="override the addon description")
    c.add_argument("--type", type=str.lower, choices=ADDON_TYPES,
                   help="override the addon type")
    c.add_argument("--tag", type=str.lower, choices=ADDON_TAGS, action="append",
                   help="override addon tags; repeat up to twice")
    c.add_argument("--author", default="Author Name",
                   help="archive author field (default: %(default)s)")
    c.add_argument("--steamid", type=int, default=0,
                   help="SteamID64 header field (default: 0)")
    c.add_argument("--timestamp", type=int, default=None,
                   help="Unix timestamp (default: current time)")
    c.add_argument("--require", action="append",
                   help="required-content entry; repeat as needed")
    c.add_argument("--exclude", action="append",
                   help="path glob to omit; repeat as needed")
    c.add_argument("--strict", action="store_true",
                   help="reject paths outside the stock whitelist")
    c.add_argument("--warn-unlisted", action="store_true",
                   help="report paths outside the stock whitelist")
    c.add_argument("--nocrc", "-nocrc", action="store_true",
                   help="write zero CRC fields")
    c.add_argument("--preserve-case", action="store_true",
                   help="preserve path case instead of lowercasing")
    c.add_argument("--force", action="store_true",
                   help="replace an existing output archive")
    c.add_argument("--quiet", action="store_true",
                   help="hide per-file progress")
    c.set_defaults(func=create)

    e = sub.add_parser("extract", help="unpack a .gma into a folder")
    e.add_argument("-file", "--file", "-f", required=True,
                   help="archive to extract")
    e.add_argument("-out", "--out", "-o",
                   help="output folder (default: archive stem)")
    e.add_argument("--force", action="store_true",
                   help="replace existing extracted files")
    e.add_argument("--quiet", action="store_true",
                   help="hide per-file progress")
    e.set_defaults(func=cmd_extract)

    l = sub.add_parser("list", help="print header + file index")
    l.add_argument("-file", "--file", "-f", required=True,
                   help="archive to inspect")
    l.set_defaults(func=cmd_list)

    v = sub.add_parser("verify", help="check every CRC")
    v.add_argument("-file", "--file", "-f", required=True,
                   help="archive to verify")
    v.set_defaults(func=cmd_verify)

    # drag-n-drop / bare path
    if len(argv) == 1 and argv[0] not in ("create", "extract", "list", "verify",
                                          "-h", "--help"):
        p = argv[0]
        if os.path.isdir(p):
            argv = ["create", "-folder", p]
        elif os.path.isfile(p):
            argv = ["extract", "-file", p]

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    try:
        return args.func(args) or 0
    except OSError as err:
        print("error: %s" % err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
