import ctypes
import json
import shutil
import struct
import subprocess
import unittest
from unittest.mock import Mock, patch
import uuid

import wifi_control as wifi


GUID_TEXT = "50f42a96-3d17-40a6-aff2-a07717c82ced"


class RadioTests(unittest.TestCase):
    def test_radio_state_is_not_inferred_from_disconnected_state(self):
        for entries, expected in ((((0, 1, 1),), True), (((0, 2, 1),), False),
                                  (((0, 1, 2),), False), (((0, 0, 1),), None),
                                  (((0, 2, 1), (1, 1, 1)), True)):
            data = struct.pack("<I", len(entries)) + b"".join(struct.pack("<III", *row) for row in entries)
            with self.subTest(entries=entries):
                self.assertIs(wifi._radio_on(data), expected)

    def test_invalid_radio_payload_is_unknown(self):
        for data in (b"", b"\x01", struct.pack("<I", 0), struct.pack("<I", 65),
                     struct.pack("<I", 1) + b"\x00"):
            self.assertIsNone(wifi._radio_on(data))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.native = Mock()
        self.context = patch.object(wifi, "WlanReader").start()
        self.context.return_value.__enter__.return_value = self.native
        patch.object(wifi.sys, "platform", "win32").start()
        self.row = wifi.InterfaceInfo()
        self.row.guid.data[:] = uuid.UUID(GUID_TEXT).bytes_le
        self.row.state = 1
        self.native.interfaces.return_value = [self.row]
        connection = wifi.ConnectionPrefix()
        connection.state = 1
        connection.profile = "ZJUWLAN-Secure"
        self.native.query.side_effect = lambda guid, opcode: (
            struct.pack("<IIII", 1, 0, 1, 1) if opcode == 4 else bytes(connection))
        self.native.profiles.return_value = frozenset(("ZJUWLAN", "ZJUWLAN-Secure"))
        self.ip = patch.object(wifi, "_ip_state", return_value={
            "name": "WLAN", "ready": True, "default_route": True}).start()
        self.alias = patch.object(wifi, "_interface_alias", return_value="WLAN", create=True).start()
        self.addCleanup(patch.stopall)

    def test_snapshot_contains_profile_and_no_keys(self):
        result = wifi.wifi_snapshot("wlan")
        self.assertEqual(result.profile, "ZJUWLAN-Secure")
        self.assertTrue(result.connected and result.link_ready and result.radio_on and result.default_route)
        self.ip.assert_called_once_with(GUID_TEXT)
        self.context.return_value.__exit__.assert_called_once()

    def test_interface_mismatch_does_not_return_other_adapter(self):
        self.assertIsNone(wifi.wifi_snapshot("Other"))
        self.native.query.assert_not_called()

    def test_disconnected_does_not_query_connection_or_infer_radio_off(self):
        self.row.state = 4
        result = wifi.wifi_snapshot("WLAN")
        self.assertFalse(result.connected)
        self.assertEqual(result.profile, "")
        self.assertTrue(result.radio_on)
        self.native.query.assert_called_once()
        self.assertEqual(self.native.query.call_args.args[1], 4)
        self.assertEqual(bytes(self.native.query.call_args.args[0].data), bytes(self.row.guid.data))
        self.ip.assert_not_called()

    def test_disconnection_is_readable_even_if_ip_query_would_fail(self):
        self.row.state = 4
        self.ip.side_effect = OSError("no matching default route")
        result = wifi.wifi_snapshot("WLAN")
        self.assertIsNotNone(result)
        self.assertFalse(result.connected or result.link_ready or result.default_route)
        self.assertTrue(result.radio_on)
        self.ip.assert_not_called()

    def test_connected_ip_query_failure_preserves_wireless_state_without_reconnecting(self):
        self.ip.side_effect = OSError("CIM unavailable")
        result = wifi.wifi_snapshot("WLAN")
        self.assertIsNotNone(result)
        self.assertTrue(result.connected)
        self.assertFalse(result.ip_known)

    def test_native_connection_in_progress_is_not_mistaken_for_idle_disconnect(self):
        self.row.state = 7
        result = wifi.wifi_snapshot("WLAN")
        self.assertTrue(result.connecting)
        self.assertTrue(result.radio_on)
        self.ip.assert_not_called()

    def test_read_failure_returns_unknown_and_closes_handle(self):
        self.native.query.side_effect = OSError("read denied")
        self.assertIsNone(wifi.wifi_snapshot("WLAN"))
        self.context.return_value.__exit__.assert_called_once()

    def test_connection_changes_during_snapshot_are_unknown_not_unmanaged(self):
        connection = wifi.ConnectionPrefix()
        connection.state = 4
        self.native.query.side_effect = lambda guid, opcode: (
            struct.pack("<IIII", 1, 0, 1, 1) if opcode == 4 else bytes(connection))
        self.assertIsNone(wifi.wifi_snapshot("WLAN"))

    def test_bad_unrelated_adapter_does_not_hide_requested_adapter(self):
        self.native.interfaces.return_value = [self.row, self.row]
        self.alias.side_effect = [OSError("adapter removed"), "WLAN"]
        self.assertEqual(wifi.wifi_snapshot("WLAN").profile, "ZJUWLAN-Secure")

    def test_unsupported_platform_does_not_load_wlan_library(self):
        with patch.object(wifi.sys, "platform", "linux"):
            self.assertIsNone(wifi.wifi_snapshot("WLAN"))
        self.context.assert_not_called()


class NativeResourceTests(unittest.TestCase):
    def test_interface_list_is_copied_before_freeing_native_buffer(self):
        row = wifi.InterfaceInfo()
        row.state = 1
        row.description = "test-adapter"
        data = struct.pack("<II", 1, 0) + bytes(row)
        buffer = ctypes.create_string_buffer(data)
        reader = wifi.WlanReader()
        reader.handle, reader.dll = None, Mock()

        def enumerate_interfaces(handle, reserved, out):
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
            return 0

        reader.dll.WlanEnumInterfaces.side_effect = enumerate_interfaces
        reader.dll.WlanFreeMemory.side_effect = lambda pointer: ctypes.memset(buffer, 0, len(data))
        result = reader.interfaces()
        self.assertEqual(result[0].description, "test-adapter")
        self.assertEqual(result[0].state, 1)
        reader.dll.WlanFreeMemory.assert_called_once()

    def test_api_error_still_releases_returned_buffer(self):
        reader = wifi.WlanReader()
        reader.handle, reader.dll = None, Mock()

        def failed_enumeration(handle, reserved, out):
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = 1234
            return 5

        reader.dll.WlanEnumInterfaces.side_effect = failed_enumeration
        with self.assertRaises(OSError):
            reader.interfaces()
        reader.dll.WlanFreeMemory.assert_called_once()


class PowerShellRouteTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
    def test_real_powershell_no_default_route_produces_offline_snapshot(self):
        # Run the actual generated script. Only route/interface data is stubbed;
        # no production network settings are changed.
        result = subprocess.CompletedProcess([], 0, "{}")
        real_run = subprocess.run
        with patch.object(wifi.subprocess, "run", return_value=result) as capture:
            wifi._ip_state(GUID_TEXT)
        script = capture.call_args.args[0][-1]
        prelude = r"""
        function Get-NetAdapter { param([switch]$IncludeHidden)
            [pscustomobject]@{InterfaceGuid='50f42a96-3d17-40a6-aff2-a07717c82ced'; Name='WLAN'; ifIndex=42; Status='Disconnected'} }
        function Get-NetIPConfiguration { param($InterfaceIndex)
            [pscustomobject]@{IPv4DefaultGateway=$null; IPv4Address=@()} }
        function Get-NetRoute { param($DestinationPrefix,$PolicyStore,$AddressFamily)
            if ($DestinationPrefix) { throw 'No matching MSFT_NetRoute objects' }
            @() }
        """
        actual = real_run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", prelude + script],
                          capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(json.loads(actual.stdout), {"name": "WLAN", "ready": False, "default_route": False})

    def test_guid_is_canonical_and_no_shell_is_used(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"name": "WLAN", "ready": True,
                                                               "default_route": True}))
        with patch.object(wifi.subprocess, "run", return_value=result) as run:
            self.assertEqual(wifi._ip_state("{" + GUID_TEXT + "}")["name"], "WLAN")
        self.assertIn("[guid]'" + GUID_TEXT + "'", run.call_args.args[0][-1])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_invalid_guid_does_not_execute_powershell(self):
        with patch.object(wifi.subprocess, "run") as run, self.assertRaises(ValueError):
            wifi._ip_state("'; Stop-Process something")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
