"""Fork opt-in that skips the native macOS send dialog after a valid nonce."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helper_loader import helper


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class NativeConfirmModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._bridge = tempfile.TemporaryDirectory(prefix="grokbot-confirm-mode-")
        self.addCleanup(self._bridge.cleanup)
        self._images = tempfile.TemporaryDirectory(prefix="grokbot-confirm-images-")
        self.addCleanup(self._images.cleanup)
        bridge = Path(os.path.realpath(self._bridge.name))
        os.chmod(bridge, 0o700)
        self.policy = bridge / "contacts"
        self.policy.mkdir(mode=0o700)
        self.image_root = Path(os.path.realpath(self._images.name))
        os.chmod(self.image_root, 0o700)

        self._old_bridge = os.environ.get("IMESSAGE_BRIDGE_DIR")
        self._old_bridge_alias = os.environ.get("COWORK_IMESSAGE_BRIDGE_DIR")
        os.environ["IMESSAGE_BRIDGE_DIR"] = str(bridge)
        os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = str(bridge)
        self.addCleanup(self._restore_bridge)
        patcher = mock.patch.object(helper, "POLICY_ROOT", self.policy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_bridge(self) -> None:
        if self._old_bridge is None:
            os.environ.pop("IMESSAGE_BRIDGE_DIR", None)
        else:
            os.environ["IMESSAGE_BRIDGE_DIR"] = self._old_bridge
        if self._old_bridge_alias is None:
            os.environ.pop("COWORK_IMESSAGE_BRIDGE_DIR", None)
        else:
            os.environ["COWORK_IMESSAGE_BRIDGE_DIR"] = self._old_bridge_alias

    def _write_mode(self, contents: str | None, *, mode: int = 0o644) -> Path:
        path = self.policy / "native_confirm.txt"
        if contents is None:
            path.unlink(missing_ok=True)
            return path
        path.write_text(contents, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def _send_text(self, to: str = "+14155551234", text: str = "hello") -> dict:
        nonce = helper.mint_send_nonce(to, text, "iMessage")
        return helper.action_send(
            {"to": to, "text": text, "send_nonce": nonce},
            None,
            {},
            [],
        )

    def test_missing_file_requires_confirm_and_honours_cancel(self) -> None:
        self.assertEqual(helper.native_confirm_mode(), "required")
        self.assertEqual(helper.action_status({}, None, {}, [])["native_confirm"], "required")
        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=False
        ) as confirm, mock.patch.object(helper, "_run_osascript") as send:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                self._send_text()
        confirm.assert_called_once()
        send.assert_not_called()

    def test_explicit_required_still_confirms(self) -> None:
        self._write_mode("# default\n\nrequired\n")
        self.assertEqual(helper.native_confirm_mode(), "required")
        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ) as confirm, mock.patch.object(
            helper, "_run_osascript", return_value=(0, "", "")
        ) as send:
            result = self._send_text(text='Hello "World"')
        confirm.assert_called_once()
        send.assert_called_once()
        self.assertIn('set msgBody to "Hello \\"World\\""', send.call_args[0][0])
        self.assertEqual(result["sent"]["text_length"], len('Hello "World"'))

    def test_unknown_symlink_and_writable_files_stay_required(self) -> None:
        self._write_mode("yes\n")
        self.assertEqual(helper.native_confirm_mode(), "required")

        self._write_mode("skip\n", mode=0o666)
        self.assertEqual(helper.native_confirm_mode(), "required")

        path = self._write_mode(None)
        target = self.policy / "elsewhere.txt"
        target.write_text("skip\n", encoding="utf-8")
        path.symlink_to(target)
        self.assertEqual(helper.native_confirm_mode(), "required")

        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ) as confirm, mock.patch.object(
            helper, "_run_osascript", return_value=(0, "", "")
        ):
            self._send_text()
        confirm.assert_called_once()

    def test_skip_sends_text_without_invoking_confirm(self) -> None:
        self._write_mode("# chat approval is enough\nSKIP\n")
        self.assertEqual(helper.native_confirm_mode(), "skip")
        status = helper.action_status({}, None, {}, [])
        self.assertEqual(status["native_confirm"], "skip")
        self.assertEqual(status["protocol_version"], "1.4")

        to = "alice@example.com"
        text = "on my way"
        nonce = helper.mint_send_nonce(to, text, "iMessage")
        with mock.patch.object(helper, "_run_send_confirmation") as confirm, mock.patch.object(
            helper, "_run_osascript", return_value=(0, "", "")
        ) as send:
            result = helper.action_send(
                {"to": to, "text": text, "send_nonce": nonce},
                None,
                {"alice@example.com": "Alice"},
                [],
            )
        confirm.assert_not_called()
        script = send.call_args[0][0]
        self.assertNotIn("POSIX file", script)
        self.assertIn(f'send msgBody to buddy "{to}" of svc', script)
        self.assertEqual(result["sent"]["resolved_name"], "Alice")
        self.assertEqual(result["sent"]["to"], to)

    def test_skip_still_rejects_missing_mismatched_and_replayed_nonces(self) -> None:
        self._write_mode("skip\n", mode=0o600)
        with mock.patch.object(helper, "_run_send_confirmation") as confirm, mock.patch.object(
            helper, "_run_osascript"
        ) as send:
            with self.assertRaisesRegex(helper.SendGateError, "missing nonce"):
                helper.action_send(
                    {"to": "+14155551234", "text": "hello"},
                    None,
                    {},
                    [],
                )
            nonce = helper.mint_send_nonce("+14155551234", "hello", "iMessage")
            with self.assertRaisesRegex(helper.SendGateError, "differs from preview"):
                helper.action_send(
                    {"to": "+14155551234", "text": "goodbye", "send_nonce": nonce},
                    None,
                    {},
                    [],
                )
        confirm.assert_not_called()
        send.assert_not_called()

        nonce = helper.mint_send_nonce("+14155551234", "once", "iMessage")
        with mock.patch.object(helper, "_run_send_confirmation") as confirm, mock.patch.object(
            helper, "_run_osascript", return_value=(0, "", "")
        ):
            helper.action_send(
                {"to": "+14155551234", "text": "once", "send_nonce": nonce},
                None,
                {},
                [],
            )
        with mock.patch.object(helper, "_run_send_confirmation") as confirm, mock.patch.object(
            helper, "_run_osascript"
        ) as send:
            with self.assertRaises(helper.SendGateError):
                helper.action_send(
                    {"to": "+14155551234", "text": "once", "send_nonce": nonce},
                    None,
                    {},
                    [],
                )
        confirm.assert_not_called()
        send.assert_not_called()

    def test_skip_still_rejects_blocked_and_group_targets(self) -> None:
        self._write_mode("skip\n")
        with mock.patch.object(helper, "_run_send_confirmation") as confirm, mock.patch.object(
            helper, "_run_osascript"
        ) as send:
            with self.assertRaisesRegex(ValueError, "group chat"):
                helper.action_send(
                    {"to": "chat123456", "text": "hi", "send_nonce": "unused"},
                    None,
                    {},
                    [],
                )
            with self.assertRaisesRegex(ValueError, "blocked_chats"):
                helper.action_send(
                    {
                        "to": "+14155551234",
                        "text": "hi",
                        "send_nonce": "unused",
                    },
                    None,
                    {},
                    ["+14155551234"],
                )
        confirm.assert_not_called()
        send.assert_not_called()

    def test_skip_image_send_stages_without_confirm_binary(self) -> None:
        self._write_mode("skip\n")
        path = self.image_root / "photo.png"
        path.write_bytes(PNG)
        preview = helper.action_send_preview(
            {
                "to": "+14155551234",
                "text": "caption",
                "service": "iMessage",
                "image": {"path": str(path)},
            },
            None,
            {},
            [],
        )
        seen = {}

        def fake_osascript(script: str, timeout: float = 15):
            match = re.search(r'POSIX file "([^"]+)"', script)
            self.assertIsNotNone(match)
            staged = match.group(1)
            seen["staged"] = staged
            self.assertEqual(Path(staged).read_bytes(), PNG)
            self.assertEqual(stat.S_IMODE(os.stat(staged).st_mode), 0o600)
            self.assertNotIn(str(path), script)
            self.assertIn("send msgBody to targetBuddy", script)
            return (0, "", "")

        with mock.patch.object(helper, "_run_send_confirmation") as confirm, mock.patch.object(
            helper, "_confirmation_helper_supports_images"
        ) as marker, mock.patch.object(helper, "_run_osascript", side_effect=fake_osascript):
            result = helper.action_send(
                {
                    "to": preview["preview"]["to"],
                    "text": preview["preview"]["text"],
                    "service": preview["preview"]["service"],
                    "send_nonce": preview["send_nonce"],
                    "image": preview["preview"]["image"],
                },
                None,
                {},
                [],
            )
        confirm.assert_not_called()
        marker.assert_not_called()
        self.assertFalse(os.path.exists(seen["staged"]))
        self.assertEqual(result["sent"]["image"]["sha256"], hashlib.sha256(PNG).hexdigest())

    def test_skip_rejects_tampered_image_before_send(self) -> None:
        self._write_mode("skip\n")
        path = self.image_root / "photo.png"
        path.write_bytes(PNG)
        preview = helper.action_send_preview(
            {"to": "+14155551234", "text": "caption", "image": {"path": str(path)}},
            None,
            {},
            [],
        )
        path.write_bytes(PNG + b"tamper")
        with mock.patch.object(helper, "_run_send_confirmation") as confirm, mock.patch.object(
            helper, "_run_osascript"
        ) as send:
            with self.assertRaisesRegex(ValueError, "does not match"):
                helper.action_send(
                    {
                        "to": "+14155551234",
                        "text": "caption",
                        "service": "iMessage",
                        "send_nonce": preview["send_nonce"],
                        "image": preview["preview"]["image"],
                    },
                    None,
                    {},
                    [],
                )
        confirm.assert_not_called()
        send.assert_not_called()

    def test_required_image_send_still_calls_confirm(self) -> None:
        path = self.image_root / "photo.png"
        path.write_bytes(PNG)
        preview = helper.action_send_preview(
            {"to": "+14155551234", "text": "caption", "image": {"path": str(path)}},
            None,
            {},
            [],
        )
        with mock.patch.object(
            helper, "_run_send_confirmation", return_value=True
        ) as confirm, mock.patch.object(helper, "_run_osascript", return_value=(0, "", "")):
            helper.action_send(
                {
                    "to": "+14155551234",
                    "text": "caption",
                    "service": "iMessage",
                    "send_nonce": preview["send_nonce"],
                    "image": preview["preview"]["image"],
                },
                None,
                {},
                [],
            )
        confirm.assert_called_once()
        self.assertEqual(confirm.call_args.kwargs["image"]["filename"], "photo.png")


if __name__ == "__main__":
    unittest.main()
