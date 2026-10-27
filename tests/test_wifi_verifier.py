from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from network_checks import ProbeResult, ProbeState
import verify_wifi_failover as verify
from wifi_control import WifiSnapshot
import wifi_failover


A, B = "ZJUWLAN", "ZJUWLAN-Secure"


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.config = wifi_failover.FailoverConfig(A, B, "WLAN", 60, 2, 8, 30, 300,
                                                  ("https://one.example/", "https://two.example/"))
        self.now = 0.0
        self.snapshot = WifiSnapshot(B, True, True, True, True, frozenset((A, B)))
        patch.object(verify.time, "monotonic", side_effect=lambda: self.now).start()
        patch.object(verify.time, "sleep", side_effect=self.sleep).start()
        patch.object(wifi_failover, "wifi_snapshot", side_effect=lambda interface: self.snapshot).start()
        patch.object(verify, "wifi_snapshot", side_effect=lambda interface: self.snapshot).start()
        patch.object(wifi_failover, "proxy_available", return_value=ProbeResult(ProbeState.ONLINE, "ok")).start()
        self.connect = patch.object(verify, "connect_wifi", side_effect=self.switch).start()
        patch.object(verify, "check_targets", return_value=[{"state": "online"}]).start()
        patch.object(verify, "event").start()
        self.auth = Mock(return_value=True)
        self.machine = wifi_failover.WifiFailover(self.config, Mock(), self.auth)
        monitor = Mock(failover=self.machine)
        monitor.check_once.side_effect = self.machine.check_once
        patch.object(verify, "HostMonitor", return_value=monitor).start()
        self.addCleanup(patch.stopall)

    def sleep(self, seconds):
        self.now += seconds

    def switch(self, profile, interface):
        self.snapshot = replace(self.snapshot, profile=profile)
        return True

    def test_b_to_a_uses_real_interval_and_one_switch_then_real_probe(self):
        result = verify.verify_leg(self.config, B, A)
        self.assertTrue(result["passed"])
        self.assertEqual(result["failure_interval_seconds"], 60)
        self.assertEqual(result["real_connection_requests"], [A])
        self.auth.assert_called_once()

    def test_a_to_b_never_authenticates_after_b_is_connected(self):
        self.snapshot = replace(self.snapshot, profile=A)
        result = verify.verify_leg(self.config, A, B)
        self.assertTrue(result["passed"])
        self.assertEqual(self.auth.call_count, 2)
        self.connect.assert_called_once_with(B, "WLAN")

    def test_failed_connection_aborts_instead_of_repeated_switching(self):
        self.connect.side_effect = None
        self.connect.return_value = False
        with self.assertRaises(verify.VerificationError):
            verify.verify_leg(self.config, B, A)
        self.connect.assert_called_once()

    def test_restore_never_overrides_user_other_network_or_radio_off(self):
        for state in (replace(self.snapshot, profile="Other"), replace(self.snapshot, radio_on=False)):
            self.snapshot = state
            verify.restore_if_needed(self.config, B)
        self.connect.assert_not_called()

    def test_restore_prefers_last_working_a_not_original_failed_b(self):
        self.snapshot = replace(self.snapshot, profile="", connected=False, link_ready=False)

        def connect(profile, interface):
            self.snapshot = replace(self.snapshot, profile=profile, connected=True, link_ready=True)
            return True

        self.connect.side_effect = connect
        result = verify.restore_if_needed(self.config, A)
        self.assertTrue(result["restored"])
        self.assertEqual(result["profile"], A)
        self.connect.assert_called_once_with(A, "WLAN")

    def test_restore_preserves_any_current_network_that_passes_real_probe(self):
        result = verify.restore_if_needed(self.config, A)
        self.assertTrue(result["restored"])
        self.assertEqual(result["profile"], B)
        self.connect.assert_not_called()

    def test_verifier_allows_safe_rollback_when_b_enterprise_auth_fails(self):
        self.snapshot = replace(self.snapshot, profile=A)

        def connect(profile, interface):
            if profile == B:
                self.snapshot = replace(self.snapshot, profile="", connected=False, link_ready=False)
            else:
                self.snapshot = replace(self.snapshot, profile=A, connected=True, link_ready=True)
            return True

        self.connect.side_effect = connect
        with self.assertRaises(verify.VerificationError):
            verify.verify_leg(self.config, A, B)
        self.assertEqual([call.args[0] for call in self.connect.call_args_list], [B, A])
        self.assertEqual(self.snapshot.profile, A)
        self.assertTrue(self.snapshot.link_ready)

    def test_restore_falls_back_once_when_preferred_connection_request_fails(self):
        self.snapshot = replace(self.snapshot, profile="", connected=False, link_ready=False)

        def connect(profile, interface):
            if profile == A:
                return False
            self.snapshot = replace(self.snapshot, profile=B, connected=True, link_ready=True)
            return True

        self.connect.side_effect = connect
        result = verify.restore_if_needed(self.config, A)
        self.assertTrue(result["restored"])
        self.assertEqual(result["profile"], B)
        self.assertEqual([call.args[0] for call in self.connect.call_args_list], [A, B])


if __name__ == "__main__":
    unittest.main()
