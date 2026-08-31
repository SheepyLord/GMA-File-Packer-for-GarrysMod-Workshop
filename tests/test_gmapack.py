import contextlib
import io
import json
import lzma
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

import gmapack  # noqa: E402


def write_test_gma(path, entries):
    """Write a minimal valid GMAD v3 archive for parser safety tests."""
    body = io.BytesIO()
    body.write(gmapack.IDENT)
    body.write(bytes([gmapack.VERSION]))
    body.write(struct.pack("<Q", 0))
    body.write(struct.pack("<Q", 1234567890))
    body.write(b"\0")
    gmapack.w_str(body, "Synthetic test addon")
    gmapack.w_str(
        body,
        gmapack.build_description("Test fixture", "servercontent", []),
    )
    gmapack.w_str(body, "Test Author")
    body.write(struct.pack("<i", 1))

    for number, (name, data) in enumerate(entries, 1):
        body.write(struct.pack("<I", number))
        gmapack.w_str(body, name)
        body.write(struct.pack("<q", len(data)))
        body.write(struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF))
    body.write(struct.pack("<I", 0))

    for _, data in entries:
        body.write(data)

    payload = body.getvalue()
    path.write_bytes(payload + struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF))


class GmaPackTests(unittest.TestCase):
    def run_main(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = gmapack.main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def make_addon(self, root):
        addon = root / "Example Addon"
        (addon / "lua" / "autorun").mkdir(parents=True)
        (addon / "custom").mkdir()
        (addon / "addon.json").write_text(
            json.dumps(
                {
                    "title": "Synthetic addon",
                    "description": "Round-trip fixture",
                    "type": "servercontent",
                    "tags": ["fun"],
                    "ignore": ["custom/ignored.tmp"],
                }
            ),
            encoding="utf-8",
        )
        (addon / "lua" / "autorun" / "example.lua").write_text(
            "print('synthetic fixture')\n", encoding="utf-8"
        )
        (addon / "custom" / "allowed.bin").write_bytes(b"not whitelisted\x00\xff")
        (addon / "custom" / "ignored.tmp").write_bytes(b"ignored")
        return addon

    def test_create_verify_and_extract_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            archive = root / "example.gma"

            result, _, _ = self.run_main(
                [
                    "create",
                    "--folder",
                    str(addon),
                    "--out",
                    str(archive),
                    "--timestamp",
                    "1234567890",
                    "--author",
                    "Test Author",
                    "--quiet",
                ]
            )
            self.assertEqual(result, 0)

            gma = gmapack.read_gma(str(archive))
            try:
                self.assertEqual(gma.title, "Synthetic addon")
                self.assertEqual(gma.timestamp, 1234567890)
                self.assertEqual(
                    [entry["name"] for entry in gma.files],
                    ["custom/allowed.bin", "lua/autorun/example.lua"],
                )
            finally:
                gma.fh.close()

            result, output, _ = self.run_main(["verify", "--file", str(archive)])
            self.assertEqual(result, 0)
            self.assertIn("0 CRC failure(s)", output)

            extracted = root / "extracted"
            result, _, _ = self.run_main(
                ["extract", "--file", str(archive), "--out", str(extracted), "--quiet"]
            )
            self.assertEqual(result, 0)
            self.assertEqual(
                (extracted / "custom" / "allowed.bin").read_bytes(),
                b"not whitelisted\x00\xff",
            )
            self.assertEqual(
                (extracted / "lua" / "autorun" / "example.lua").read_text(
                    encoding="utf-8"
                ),
                "print('synthetic fixture')\n",
            )
            metadata = json.loads((extracted / "addon.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["title"], "Synthetic addon")
            self.assertEqual(metadata["tags"], ["fun"])

    def test_strict_mode_rejects_unlisted_extension(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            archive = root / "strict.gma"

            with self.assertRaisesRegex(SystemExit, "rejected by whitelist"):
                self.run_main(
                    [
                        "create",
                        "--folder",
                        str(addon),
                        "--out",
                        str(archive),
                        "--strict",
                        "--quiet",
                    ]
                )
            self.assertFalse(archive.exists())

    def test_existing_output_inside_addon_is_not_packed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            archive = addon / "build.gma"

            self.run_main(
                [
                    "create",
                    "--folder",
                    str(addon),
                    "--out",
                    str(archive),
                    "--quiet",
                ]
            )
            self.run_main(
                [
                    "create",
                    "--folder",
                    str(addon),
                    "--out",
                    str(archive),
                    "--force",
                    "--quiet",
                ]
            )

            gma = gmapack.read_gma(str(archive))
            try:
                self.assertNotIn("build.gma", [entry["name"] for entry in gma.files])
            finally:
                gma.fh.close()

    def test_create_requires_force_to_replace_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            archive = root / "existing.gma"
            archive.write_bytes(b"keep me")

            with self.assertRaisesRegex(SystemExit, "use --force"):
                self.run_main(
                    ["create", "--folder", str(addon), "--out", str(archive), "--quiet"]
                )
            self.assertEqual(archive.read_bytes(), b"keep me")

    def test_failed_publish_cleans_unique_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            output_directory = root / "cannot-replace-directory"
            output_directory.mkdir()
            unrelated_part = root / "existing.part"
            unrelated_part.write_bytes(b"keep me")

            result, _, error = self.run_main(
                [
                    "create",
                    "--folder",
                    str(addon),
                    "--out",
                    str(output_directory),
                    "--force",
                    "--quiet",
                ]
            )
            self.assertEqual(result, 1)
            self.assertIn("error:", error)
            self.assertEqual(unrelated_part.read_bytes(), b"keep me")
            self.assertEqual(list(root.glob(".gmapack-*.part")), [])

    def test_nocrc_archive_verifies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            archive = root / "nocrc.gma"

            self.run_main(
                [
                    "create",
                    "--folder",
                    str(addon),
                    "--out",
                    str(archive),
                    "--nocrc",
                    "--quiet",
                ]
            )
            result, output, _ = self.run_main(["verify", "--file", str(archive)])
            self.assertEqual(result, 0)
            self.assertIn("stored 00000000", output)

    def test_fixed_timestamp_builds_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            first = root / "first.gma"
            second = root / "second.gma"
            common = [
                "create",
                "--folder",
                str(addon),
                "--timestamp",
                "1234567890",
                "--author",
                "Test Author",
                "--quiet",
            ]

            self.run_main(common + ["--out", str(first)])
            self.run_main(common + ["--out", str(second)])
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_git_pointer_file_is_never_packed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            (addon / ".git").write_text(
                "gitdir: C:/private/worktree/path\n", encoding="utf-8"
            )
            archive = root / "safe.gma"

            self.run_main(
                ["create", "--folder", str(addon), "--out", str(archive), "--quiet"]
            )
            gma = gmapack.read_gma(str(archive))
            try:
                self.assertNotIn(".git", [entry["name"] for entry in gma.files])
            finally:
                gma.close()

    def test_linked_source_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            outside = root / "outside"
            outside.mkdir()
            (outside / "private.txt").write_text("private", encoding="utf-8")
            link = addon / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest("creating symlinks is unavailable: %s" % error)

            with self.assertRaisesRegex(SystemExit, "refusing linked directory"):
                self.run_main(
                    [
                        "create",
                        "--folder",
                        str(addon),
                        "--out",
                        str(root / "unsafe.gma"),
                        "--quiet",
                    ]
                )

    def test_verify_reports_addon_crc_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "bad-crc.gma"
            write_test_gma(archive, [("lua/test.lua", b"test")])
            data = bytearray(archive.read_bytes())
            data[-1] ^= 0xFF
            archive.write_bytes(data)

            result, output, _ = self.run_main(["verify", "--file", str(archive)])
            self.assertEqual(result, 1)
            self.assertIn("MISMATCH", output)

    def test_verify_rejects_truncated_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "truncated.gma"
            write_test_gma(archive, [("lua/test.lua", b"test payload")])
            archive.write_bytes(archive.read_bytes()[:-6])

            with self.assertRaisesRegex(SystemExit, "truncated archive"):
                self.run_main(["verify", "--file", str(archive)])

    def test_verify_rejects_trailing_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "trailing.gma"
            write_test_gma(archive, [("lua/test.lua", b"test")])
            archive.write_bytes(archive.read_bytes() + b"unexpected")

            result, output, _ = self.run_main(["verify", "--file", str(archive)])
            self.assertEqual(result, 1)
            self.assertIn("trailing byte", output)

    def test_lzma_wrapped_archive_is_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "plain.gma"
            compressed = root / "compressed.gma"
            write_test_gma(archive, [("lua/test.lua", b"test")])
            raw_archive = archive.read_bytes()
            wrapper = bytearray(lzma.compress(raw_archive, format=lzma.FORMAT_ALONE))
            wrapper[5:13] = struct.pack("<Q", len(raw_archive))
            compressed.write_bytes(wrapper)

            result, output, _ = self.run_main(["verify", "--file", str(compressed)])
            self.assertEqual(result, 0)
            self.assertIn("LZMA-compressed", output)

    def test_lzma_wrapper_rejects_truncation_and_trailing_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "plain.gma"
            write_test_gma(archive, [("lua/test.lua", b"test")])
            wrapper = lzma.compress(archive.read_bytes(), format=lzma.FORMAT_ALONE)

            truncated = root / "truncated-wrapper.gma"
            truncated.write_bytes(wrapper[:-1])
            with self.assertRaisesRegex(SystemExit, "truncated LZMA stream"):
                self.run_main(["verify", "--file", str(truncated)])

            trailing = root / "trailing-wrapper.gma"
            trailing.write_bytes(wrapper + b"unexpected")
            with self.assertRaisesRegex(SystemExit, "trailing data"):
                self.run_main(["verify", "--file", str(trailing)])

    def test_unsupported_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "future.gma"
            write_test_gma(archive, [("lua/test.lua", b"test")])
            data = bytearray(archive.read_bytes())
            data[4] = 4
            archive.write_bytes(data)

            with self.assertRaisesRegex(SystemExit, "unsupported GMAD version"):
                self.run_main(["list", "--file", str(archive)])

    def test_extract_refuses_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "unsafe.gma"
            output = root / "output"
            write_test_gma(archive, [("../escape.txt", b"nope")])

            result, _, error = self.run_main(
                ["extract", "--file", str(archive), "--out", str(output), "--quiet"]
            )
            self.assertEqual(result, 1)
            self.assertIn("refusing unsafe archive path", error)
            self.assertFalse((root / "escape.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Windows device names are platform-specific")
    def test_extract_refuses_windows_device_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for number, name in enumerate(
                ("con.txt", "CONIN$.log", "COM¹.txt", "bad?.txt", "trailing. ")
            ):
                with self.subTest(name=name):
                    archive = root / ("unsafe-%d.gma" % number)
                    output = root / ("output-%d" % number)
                    write_test_gma(archive, [(name, b"nope")])

                    result, _, error = self.run_main(
                        [
                            "extract",
                            "--file",
                            str(archive),
                            "--out",
                            str(output),
                            "--quiet",
                        ]
                    )
                    self.assertEqual(result, 1)
                    self.assertIn("refusing unsafe archive path", error)

    def test_duplicate_archive_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "duplicate.gma"
            write_test_gma(
                archive,
                [("lua/test.lua", b"first"), ("LUA/TEST.LUA", b"second")],
            )

            with self.assertRaisesRegex(SystemExit, "duplicate path"):
                self.run_main(["list", "--file", str(archive)])

    def test_extract_checks_file_crc_before_replacement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "corrupt.gma"
            output = root / "output"
            write_test_gma(archive, [("lua/test.lua", b"test")])
            gma = gmapack.read_gma(str(archive))
            try:
                data_offset = gma.files[0]["offset"]
            finally:
                gma.close()
            data = bytearray(archive.read_bytes())
            data[data_offset] ^= 0xFF
            data[-4:] = struct.pack("<I", zlib.crc32(data[:-4]) & 0xFFFFFFFF)
            archive.write_bytes(data)

            result, _, error = self.run_main(
                ["extract", "--file", str(archive), "--out", str(output), "--quiet"]
            )
            self.assertEqual(result, 1)
            self.assertIn("CRC-mismatched", error)
            self.assertFalse((output / "lua" / "test.lua").exists())

    def test_extract_rejects_bad_addon_crc_and_trailing_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "bad-addon-crc.gma"
            write_test_gma(archive, [("lua/test.lua", b"test")])
            data = bytearray(archive.read_bytes())
            data[-1] ^= 0xFF
            archive.write_bytes(data)

            with self.assertRaisesRegex(SystemExit, "addon CRC mismatch"):
                self.run_main(
                    ["extract", "--file", str(archive), "--out", str(root / "bad")]
                )

            trailing = root / "trailing.gma"
            write_test_gma(trailing, [("lua/test.lua", b"test")])
            trailing.write_bytes(trailing.read_bytes() + b"unexpected")
            with self.assertRaisesRegex(SystemExit, "trailing byte"):
                self.run_main(
                    [
                        "extract",
                        "--file",
                        str(trailing),
                        "--out",
                        str(root / "trailing"),
                    ]
                )

    def test_extract_requires_force_to_replace_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "safe.gma"
            output = root / "output"
            existing = output / "lua" / "test.lua"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"keep me")
            write_test_gma(archive, [("lua/test.lua", b"replacement")])

            result, _, error = self.run_main(
                ["extract", "--file", str(archive), "--out", str(output), "--quiet"]
            )
            self.assertEqual(result, 1)
            self.assertIn("use --force", error)
            self.assertEqual(existing.read_bytes(), b"keep me")

            result, _, _ = self.run_main(
                [
                    "extract",
                    "--file",
                    str(archive),
                    "--out",
                    str(output),
                    "--force",
                    "--quiet",
                ]
            )
            self.assertEqual(result, 0)
            self.assertEqual(existing.read_bytes(), b"replacement")

    def test_extract_accepts_current_directory_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "safe.gma"
            output = root / "output"
            output.mkdir()
            write_test_gma(archive, [("lua/test.lua", b"test")])

            previous = Path.cwd()
            try:
                os.chdir(output)
                result, _, _ = self.run_main(
                    ["extract", "--file", str(archive), "--out", ".", "--quiet"]
                )
            finally:
                os.chdir(previous)

            self.assertEqual(result, 0)
            self.assertEqual((output / "lua" / "test.lua").read_bytes(), b"test")

    def test_addon_json_must_be_an_object(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            addon = Path(temp_dir) / "addon"
            addon.mkdir()
            (addon / "addon.json").write_text("[]", encoding="utf-8")
            (addon / "test.bin").write_bytes(b"test")

            with self.assertRaisesRegex(SystemExit, "JSON object"):
                self.run_main(["create", "--folder", str(addon), "--quiet"])

    def test_empty_required_content_entry_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            with self.assertRaisesRegex(SystemExit, "cannot be empty"):
                self.run_main(
                    [
                        "create",
                        "--folder",
                        str(addon),
                        "--out",
                        str(root / "invalid.gma"),
                        "--require",
                        "",
                        "--quiet",
                    ]
                )

    def test_writer_enforces_reader_string_limit(self):
        with mock.patch.object(gmapack, "MAX_STRING_SIZE", 8):
            with self.assertRaisesRegex(SystemExit, "safety limit"):
                gmapack.w_str(io.BytesIO(), "too much text")

    def test_missing_input_has_clean_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.gma"
            result, _, error = self.run_main(["verify", "--file", str(missing)])
            self.assertEqual(result, 1)
            self.assertIn("error:", error)
            self.assertNotIn("Traceback", error)

    def test_unicode_output_path_survives_legacy_console_encoding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            addon = self.make_addon(root)
            archive = root / "snowman-☃.gma"
            buffer = io.BytesIO()
            legacy_stdout = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
            with contextlib.redirect_stdout(legacy_stdout):
                result = gmapack.main(
                    [
                        "create",
                        "--folder",
                        str(addon),
                        "--out",
                        str(archive),
                        "--quiet",
                    ]
                )
                legacy_stdout.flush()
                rendered = buffer.getvalue()
            legacy_stdout.close()

            self.assertEqual(result, 0)
            self.assertTrue(archive.exists())
            self.assertIn(b"\\u2603", rendered)

    def test_list_escapes_terminal_control_characters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "control.gma"
            write_test_gma(archive, [("lua/\x1b[31m.lua", b"test")])

            result, output, _ = self.run_main(["list", "--file", str(archive)])
            self.assertEqual(result, 0)
            self.assertIn("lua/\\x1b[31m.lua", output)
            self.assertNotIn("\x1b", output)


if __name__ == "__main__":
    unittest.main()
