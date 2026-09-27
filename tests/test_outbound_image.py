"""Outbound image send (protocol 1.3) and text-only send regression."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helper_loader import REPO_ROOT, helper


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x18\x00\x00\x00" + b"WEBP" + b"\x00" * 16
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16
HEIF = b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 16


class OutboundImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._bridge = tempfile.TemporaryDirectory(prefix="grokbot-image-bridge-")
        self.addCleanup(self._bridge.cleanup)
        self._images = tempfile.TemporaryDirectory(prefix="grokbot-image-files-")
        self.addCleanup(self._images.cleanup)
        bridge = os.path.realpath(self._bridge.name)
        os.chmod(bridge, 0o700)
        self.image_root = Path(os.path.realpath(self._images.name))
        os.chmod(self.image_root, 0o700)
        self._old_bridge = os.environ.get("IMESSAGE_BRIDGE_DIR")
        self._old_bridge_alias = os.environ.get("COWORK_IMESSAGE_BRIDGE_DIR")
        os.environ["IMESSAGE_BRIDGE_DIR"] = bridge
        os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = bridge
        self.addCleanup(self._restore_bridge)

    def _restore_bridge(self) -> None:
        if self._old_bridge is None:
            os.environ.pop("IMESSAGE_BRIDGE_DIR", None)
        else:
            os.environ["IMESSAGE_BRIDGE_DIR"] = self._old_bridge
        if self._old_bridge_alias is None:
            os.environ.pop("COWORK_IMESSAGE_BRIDGE_DIR", None)
        else:
            os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = self._old_bridge_alias

    def _write(self, name: str, data: bytes) -> str:
        path = self.image_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def _preview(self, path: str, text: str | None = "caption", to: str = "+14155551234") -> dict:
        params: dict = {"to": to, "service": "iMessage", "image": {"path": path}}
        if text is not None:
            params["text"] = text
        return helper.action_send_preview(params, None, {}, [])

    def _send(self, preview: dict, **overrides):
        image = dict(preview["preview"]["image"])
        image.update(overrides)
        params = {
            "to": preview["preview"]["to"],
            "text": preview["preview"]["text"],
            "service": preview["preview"]["service"],
            "send_nonce": preview["send_nonce"],
            "image": image,
        }
        return helper.action_send(params, None, {}, [])

    def test_text_only_send_script_is_unchanged(self) -> None:
        to = "alice@example.com"
        text = 'Hello "World"!\nLine 2 with \\backslash'
        nonce = helper.mint_send_nonce(to, text, "iMessage")
        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ) as confirm, mock.patch.object(
            helper, "_run_osascript", return_value=(0, "", "")
        ) as send:
            result = helper.action_send(
                {"to": to, "text": text, "send_nonce": nonce},
                None,
                {},
                [],
            )

        self.assertNotIn("image", result["sent"])
        confirm.assert_called_once_with(
            to=to,
            resolved_name="",
            service="iMessage",
            text=text,
        )
        script = send.call_args[0][0]
        self.assertNotIn("POSIX file", script)
        self.assertNotIn("/tmp/", script)
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        self.assertIn(f'set msgBody to "{escaped}"', script)
        self.assertIn(f'send msgBody to buddy "{to}" of svc', script)

    def test_text_only_still_rejects_empty_body(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty"):
            helper.action_send_preview(
                {"to": "+14155551234", "text": ""},
                None,
                {},
                [],
            )

    def test_text_nonce_rejects_added_image_identity(self) -> None:
        nonce = helper.mint_send_nonce("+14155551234", "hello", "iMessage")
        image = {
            "path": "/tmp/a.png",
            "filename": "a.png",
            "mime": "image/png",
            "size": 8,
            "sha256": "ab" * 32,
        }
        with self.assertRaisesRegex(helper.SendGateError, "differs"):
            helper.consume_send_nonce(nonce, "+14155551234", "hello", "iMessage", image)
        with self.assertRaises(helper.SendGateError):
            helper.consume_send_nonce(nonce, "+14155551234", "hello", "iMessage")

    def test_supported_image_types_preview(self) -> None:
        samples = {
            "photo.png": (PNG, "image/png"),
            "photo.jpg": (JPEG, "image/jpeg"),
            "photo.jpeg": (JPEG, "image/jpeg"),
            "anim.gif": (GIF, "image/gif"),
            "still.webp": (WEBP, "image/webp"),
            "shot.heic": (HEIC, "image/heic"),
            "shot.heif": (HEIF, "image/heif"),
        }
        for name, (data, mime) in samples.items():
            with self.subTest(name=name):
                path = self._write(name, data)
                preview = self._preview(path)
                image = preview["preview"]["image"]
                self.assertEqual(image["path"], path)
                self.assertEqual(image["filename"], name)
                self.assertEqual(image["mime"], mime)
                self.assertEqual(image["size"], len(data))
                self.assertEqual(image["sha256"], hashlib.sha256(data).hexdigest())
                self.assertTrue(preview["send_nonce"])

    def test_image_send_stages_file_and_shows_attachment_in_confirm(self) -> None:
        path = self._write("photo.png", PNG)
        preview = self._preview(path, text='cap "tion"')
        seen = {}

        def fake_osascript(script: str, timeout: float = 15):
            match = re.search(r'POSIX file "([^"]+)"', script)
            self.assertIsNotNone(match)
            staged = match.group(1)
            seen["staged"] = staged
            seen["script"] = script
            self.assertTrue(os.path.isfile(staged))
            self.assertEqual(Path(staged).read_bytes(), PNG)
            self.assertEqual(stat.S_IMODE(os.stat(staged).st_mode), 0o600)
            self.assertNotIn(path, script)
            return (0, "", "")

        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ) as confirm, mock.patch.object(helper, "_run_osascript", side_effect=fake_osascript):
            result = self._send(preview)

        image = preview["preview"]["image"]
        confirm.assert_called_once_with(
            to="+14155551234",
            resolved_name="",
            service="iMessage",
            text='cap "tion"',
            image=image,
        )
        self.assertIn("send (POSIX file", seen["script"])
        self.assertIn('set msgBody to "cap \\"tion\\""', seen["script"])
        self.assertIn("send msgBody to targetBuddy", seen["script"])
        self.assertFalse(os.path.exists(seen["staged"]))
        self.assertEqual(result["sent"]["image"], image)
        self.assertEqual(result["sent"]["text_length"], len('cap "tion"'))

    def test_image_only_omits_caption_send(self) -> None:
        path = self._write("only.png", PNG)
        preview = self._preview(path, text=None)
        self.assertEqual(preview["preview"]["text"], "")
        self.assertEqual(preview["preview"]["text_length"], 0)

        def fake_osascript(script: str, timeout: float = 15):
            self.assertNotIn("msgBody", script)
            self.assertIn("POSIX file", script)
            return (0, "", "")

        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ), mock.patch.object(helper, "_run_osascript", side_effect=fake_osascript):
            result = self._send(preview)
        self.assertEqual(result["sent"]["text_length"], 0)
        self.assertEqual(result["sent"]["image"]["filename"], "only.png")

    def test_cancelled_image_send_does_not_call_messages(self) -> None:
        path = self._write("photo.png", PNG)
        preview = self._preview(path)
        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=False
        ), mock.patch.object(helper, "_run_osascript") as send:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                self._send(preview)
        send.assert_not_called()

    def test_osascript_failure_removes_staged_file(self) -> None:
        path = self._write("photo.png", PNG)
        preview = self._preview(path)
        staged = {}

        def fake_osascript(script: str, timeout: float = 15):
            staged["path"] = re.search(r'POSIX file "([^"]+)"', script).group(1)
            self.assertTrue(os.path.isfile(staged["path"]))
            return (1, "", "messages failed")

        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ), mock.patch.object(helper, "_run_osascript", side_effect=fake_osascript):
            with self.assertRaisesRegex(RuntimeError, "osascript send failed"):
                self._send(preview)
        self.assertFalse(os.path.exists(staged["path"]))

    def test_changed_bytes_do_not_send_and_restored_bytes_do(self) -> None:
        path = self._write("photo.png", PNG)
        preview = self._preview(path)
        Path(path).write_bytes(PNG + b"tamper")
        with mock.patch.object(helper, "_run_osascript") as send, mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ):
            with self.assertRaisesRegex(ValueError, "does not match"):
                self._send(preview)
        send.assert_not_called()

        Path(path).write_bytes(PNG)
        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ), mock.patch.object(helper, "_run_osascript", return_value=(0, "", "")) as send:
            result = self._send(preview)
        send.assert_called_once()
        self.assertEqual(result["sent"]["image"]["sha256"], hashlib.sha256(PNG).hexdigest())

    def test_echoed_identity_mismatch_is_rejected(self) -> None:
        path = self._write("photo.png", PNG)
        preview = self._preview(path)
        with mock.patch.object(helper, "_run_osascript") as send:
            with self.assertRaisesRegex(ValueError, "does not match"):
                self._send(preview, sha256="ab" * 32)
        send.assert_not_called()

    def test_text_preview_cannot_be_replayed_as_image_send(self) -> None:
        path = self._write("photo.png", PNG)
        text_preview = helper.action_send_preview(
            {"to": "+14155551234", "text": "hello"},
            None,
            {},
            [],
        )
        image_preview = self._preview(path, text="hello")
        params = {
            "to": "+14155551234",
            "text": "hello",
            "service": "iMessage",
            "send_nonce": text_preview["send_nonce"],
            "image": image_preview["preview"]["image"],
        }
        with mock.patch.object(helper, "_run_osascript") as send:
            with self.assertRaises(helper.SendGateError):
                helper.action_send(params, None, {}, [])
        send.assert_not_called()

    def test_image_preview_cannot_be_sent_as_text_only(self) -> None:
        path = self._write("photo.png", PNG)
        preview = self._preview(path, text="hello")
        with mock.patch.object(helper, "_run_osascript") as send:
            with self.assertRaises(helper.SendGateError):
                helper.action_send(
                    {
                        "to": "+14155551234",
                        "text": "hello",
                        "service": "iMessage",
                        "send_nonce": preview["send_nonce"],
                    },
                    None,
                    {},
                    [],
                )
        send.assert_not_called()

    def test_image_nonce_is_single_use(self) -> None:
        path = self._write("photo.png", PNG)
        preview = self._preview(path)
        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ), mock.patch.object(helper, "_run_osascript", return_value=(0, "", "")):
            self._send(preview)
        with mock.patch.object(helper, "_run_osascript") as send:
            with self.assertRaises(helper.SendGateError):
                self._send(preview)
        send.assert_not_called()

    def test_rejects_missing_unreadable_non_image_and_mismatched_types(self) -> None:
        missing = str(self.image_root / "missing.png")
        with self.assertRaisesRegex(ValueError, "not found"):
            self._preview(missing)

        self._write("empty.png", b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            self._preview(str(self.image_root / "empty.png"))

        self._write("notes.png", b"this is not an image")
        with self.assertRaisesRegex(ValueError, "not a supported image"):
            self._preview(str(self.image_root / "notes.png"))

        self._write("actually-jpeg.png", JPEG)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self._preview(str(self.image_root / "actually-jpeg.png"))

        self._write("noext", PNG)
        with self.assertRaisesRegex(ValueError, "extension"):
            self._preview(str(self.image_root / "noext"))

        directory = self.image_root / "dir.png"
        directory.mkdir()
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            self._preview(str(directory))

        target = self._write("real.png", PNG)
        link = self.image_root / "link.png"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self._preview(str(link))

        with self.assertRaisesRegex(ValueError, "absolute"):
            helper.action_send_preview(
                {"to": "+14155551234", "text": "x", "image": {"path": "photo.png"}},
                None,
                {},
                [],
            )

        spaced = self._write("My Photo.png", PNG)
        spaced_preview = self._preview(spaced, text="caption")
        self.assertEqual(spaced_preview["preview"]["image"]["filename"], "My Photo.png")

        linked_parent = self.image_root / "linked-dir"
        real_parent = self.image_root / "real-dir"
        real_parent.mkdir()
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        (real_parent / "inside.png").write_bytes(PNG)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self._preview(str(linked_parent / "inside.png"))

    def test_rejects_oversized_image_without_reading_payload(self) -> None:
        path = self.image_root / "big.png"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.ftruncate(fd, helper.MAX_OUTBOUND_IMAGE_BYTES + 1)
        finally:
            os.close(fd)
        with self.assertRaisesRegex(ValueError, "too large"):
            self._preview(str(path))

    def test_group_and_blocked_recipients_are_rejected(self) -> None:
        path = self._write("photo.png", PNG)
        with self.assertRaisesRegex(ValueError, "group chat"):
            self._preview(path, to="chat123456")

        with self.assertRaisesRegex(ValueError, "blocked_chats"):
            helper.action_send(
                {
                    "to": "+14155551234",
                    "text": "caption",
                    "image": {"path": str(self.image_root / "missing.png")},
                    "send_nonce": "unused",
                },
                None,
                {},
                ["+14155551234"],
            )

    def test_confirmation_payload_includes_image_metadata(self) -> None:
        binary = self.image_root / "grokbot-imessage-confirm"
        binary.write_bytes(b"prefix " + helper._IMAGE_CONFIRM_MARKER + b" suffix")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        image = {
            "filename": "photo.png",
            "mime": "image/png",
            "size": len(PNG),
            "path": "/Users/matthew/Pictures/photo.png",
            "sha256": "ab" * 32,
        }
        with mock.patch.object(helper, "CONFIRM_HELPER_PATH", binary), mock.patch.object(
            helper.subprocess, "run", return_value=completed
        ) as run:
            approved = helper._run_send_confirmation(
                to="+14155551234",
                resolved_name="Alex Example",
                service="iMessage",
                text="caption",
                image=image,
            )
        self.assertTrue(approved)
        import json

        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["text"], "caption")
        self.assertEqual(payload["image"]["filename"], "photo.png")
        self.assertEqual(payload["image"]["mime"], "image/png")
        self.assertEqual(payload["image"]["size"], len(PNG))
        self.assertEqual(payload["image"]["path"], "/Users/matthew/Pictures/photo.png")
        self.assertNotIn("sha256", payload["image"])

    def test_old_confirmation_helper_cannot_approve_an_image(self) -> None:
        binary = self.image_root / "old-confirm"
        binary.write_bytes(b"text-only confirm helper")
        with mock.patch.object(helper, "CONFIRM_HELPER_PATH", binary):
            with self.assertRaisesRegex(RuntimeError, "does not support image sends"):
                helper._run_send_confirmation(
                    to="+14155551234",
                    resolved_name="",
                    service="iMessage",
                    text="caption",
                    image={
                        "filename": "photo.png",
                        "mime": "image/png",
                        "size": 4,
                        "path": "/tmp/photo.png",
                    },
                )

    def test_confirm_helper_source_shows_attachment_and_defaults_to_cancel(self) -> None:
        source = (REPO_ROOT / "bin" / "confirm_imessage_send.m").read_text()
        self.assertIn("IMESSAGE_CONFIRM_IMAGE_V1", source)
        self.assertIn("Attachment:", source)
        self.assertIn("Image Send", source)
        self.assertIn('@"(no caption)"', source)
        cancel_pos = source.index('addButtonWithTitle:@"Cancel"')
        send_pos = source.index('addButtonWithTitle:@"Send"')
        self.assertLess(cancel_pos, send_pos)
        self.assertIn('cancelButton.keyEquivalent = @"\\r"', source)


if __name__ == "__main__":
    unittest.main()
