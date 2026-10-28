from dataclasses import replace
import os
import unittest
from unittest.mock import Mock, patch

import host_reconnect as host
from network_checks import ProbeResult, ProbeState
import wifi_failover as failover
from wifi_control import WifiSnapshot


A, B = "ZJUWLAN", "ZJUWLAN-Secure"
SAVED = frozenset((A, B))


def snapshot(profile=B, **overrides):
    return replace(WifiSnapshot(profile, True, True, True, True, SAVED), **overrides)


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.config = failover.FailoverConfig(A, B, "WLAN", 60, 2, 8, 30, 300,
                                              ("https://one.example/", "https://two.example/"))
        self.log, self.auth = Mock(), Mock()
        self.monitor = failover.WifiFailover(self.config, self.log, self.auth)
        self.clock = patch.object(failover.time, "monotonic", return_value=0).start()
        self.wifi = patch.object(failover, "wifi_snapshot", return_value=snapshot()).start()
        self.connect = patch.object(failover, "connect_wifi", return_value=True).start()
        self.probe = patch.object(failover, "proxy_available",
                                  return_value=ProbeResult(ProbeState.ONLINE, "ok")).start()
        self.addCleanup(patch.stopall)

    def check(self, at, state=ProbeState.FAILED, wifi=None):
        self.clock.return_value = at
        self.probe.return_value = ProbeResult(state, "test")
        if wifi is not None:
            self.wifi.return_value = wifi
        return self.monitor.check_once()[0]

    def switch_to_a(self):
        self.assertEqual(self.check(0), "proxy_failed")
        self.connect.assert_not_called()
        self.assertEqual(self.check(60), "switching")
        self.connect.assert_called_once_with(A, "WLAN")

    def test_start_on_b_does_not_authenticate_or_prefer_a(self):
        for now in (0, 60, 120):
            self.assertEqual(self.check(now, ProbeState.ONLINE), "online")
        self.auth.assert_not_called()
        self.connect.assert_not_called()

    def test_two_failures_switch_b_to_a_and_confirm_before_authentication(self):
        self.switch_to_a()
        self.assertEqual(self.check(65, wifi=snapshot(A, link_ready=False)), "waiting_link")
        self.auth.assert_not_called()
        self.assertEqual(self.check(70, ProbeState.ONLINE, snapshot(A)), "online")
        self.auth.assert_called_once()
        self.assertFalse(self.monitor.switched)
        self.assertEqual(self.monitor.last_profile, A)

    def test_a_to_b_never_authenticates_on_b(self):
        self.check(0, wifi=snapshot(A))
        self.assertEqual(self.check(60), "switching")
        self.connect.assert_called_once_with(B, "WLAN")
        self.auth.reset_mock()
        self.assertEqual(self.check(65, ProbeState.ONLINE, snapshot(B)), "online")
        self.auth.assert_not_called()
        self.assertEqual(self.check(125, ProbeState.ONLINE), "online")
        self.connect.assert_called_once()

    def test_success_breaks_consecutive_failure_count(self):
        self.check(0)
        self.check(60, ProbeState.ONLINE)
        self.assertEqual(self.check(120), "proxy_failed")
        self.connect.assert_not_called()
        self.assertEqual(self.monitor.failures, 1)

    def test_unknown_and_unavailable_break_consecutive_failure_count(self):
        for state in (ProbeState.UNKNOWN, ProbeState.UNAVAILABLE):
            with self.subTest(state=state):
                self.monitor._reset()
                self.check(0)
                self.check(60, state)
                self.check(120)
                self.connect.assert_not_called()
                self.assertEqual(self.monitor.failures, 1)

    def test_calls_between_probe_times_do_not_accumulate_or_probe(self):
        self.check(0)
        for now in (1, 5, 59):
            self.assertEqual(self.check(now), "waiting_probe")
        self.probe.assert_called_once_with(self.config.urls, 8)
        self.connect.assert_not_called()

    def test_both_networks_fail_cooldown_then_retry_other(self):
        self.switch_to_a()
        self.assertEqual(self.check(65, wifi=snapshot(A)), "proxy_failed")
        self.assertEqual(self.check(125), "cooldown")
        self.assertEqual(self.monitor.cooldown_until, 425)
        for now in (185, 245, 305, 365):
            self.assertEqual(self.check(now), "cooldown")
        self.connect.assert_called_once()
        self.assertEqual(self.check(425), "switching")
        self.assertEqual(self.connect.call_args.args, (B, "WLAN"))

    def reach_switch_limit(self, profile=B):
        for now in range(0, 6000, 60):
            state = self.check(now, wifi=snapshot(profile))
            if state == "switching":
                profile = A if profile == B else B
            elif state == "switch_limit":
                self.assertEqual(self.connect.call_count, self.monitor.config.proxy_max_switches)
                return now, profile
        self.fail("持续代理故障未达到切网保护上限")

    def test_five_switches_stop_oscillation_but_keep_probing(self):
        now, profile = self.reach_switch_limit()
        self.assertEqual([call.args[0] for call in self.connect.call_args_list], [A, B, A, B, A])
        probes = self.probe.call_count
        for step in range(1, 101):
            self.assertEqual(self.check(now + 60 * step, wifi=snapshot(profile)), "switch_limit")
        self.assertEqual(self.connect.call_count, 5)
        self.assertEqual(self.monitor.proxy_switches, 5)
        self.assertEqual(self.probe.call_count, probes + 100)
        self.assertEqual(self.monitor.next_probe, now + 6060)
        self.assertEqual(self.check(now + 6001), "waiting_probe")
        self.assertEqual(self.probe.call_count, probes + 100)

    def test_switch_limit_applies_starting_from_a(self):
        self.reach_switch_limit(A)
        self.assertEqual([call.args[0] for call in self.connect.call_args_list], [B, A, B, A, B])

    def test_custom_switch_limit_is_honored(self):
        self.monitor.config = replace(self.config, proxy_max_switches=2)
        self.reach_switch_limit()
        self.assertEqual(self.monitor.proxy_switches, 2)

    def test_proxy_recovery_rearms_switch_budget(self):
        now, profile = self.reach_switch_limit()
        self.assertEqual(self.check(now + 60, ProbeState.ONLINE), "online")
        self.assertEqual(self.monitor.proxy_switches, 0)
        self.assertEqual(self.monitor.failures, 0)
        self.assertEqual(self.check(now + 120), "proxy_failed")
        self.assertEqual(self.check(now + 180), "switching")
        self.assertEqual(self.connect.call_count, 6)
        self.assertEqual(self.monitor.proxy_switches, 1)
        self.assertEqual(self.connect.call_args.args[0], B if profile == A else A)

    def test_unknown_and_disabled_proxy_do_not_rearm_switch_budget(self):
        now, _ = self.reach_switch_limit()
        self.assertEqual(self.check(now + 60, ProbeState.UNKNOWN), "proxy_unknown")
        self.assertEqual(self.check(now + 120, ProbeState.UNAVAILABLE), "proxy_unavailable")
        self.assertEqual(self.monitor.proxy_switches, 5)
        self.assertEqual(self.check(now + 180), "switch_limit")
        self.assertEqual(self.connect.call_count, 5)

    def test_connection_state_resets_do_not_rearm_switch_budget(self):
        now, profile = self.reach_switch_limit()
        self.assertEqual(self.check(now + 60, wifi=snapshot(radio_on=False)), "radio_off")
        self.assertEqual(self.check(now + 120, wifi=snapshot(profile)), "switch_limit")
        self.assertEqual(self.check(now + 180, wifi=snapshot("Other")), "unmanaged_wifi")
        self.assertEqual(self.check(now + 240, wifi=snapshot(profile)), "switch_limit")
        other = B if profile == A else A
        self.assertEqual(self.check(now + 300, wifi=snapshot(other)), "switch_limit")
        self.assertEqual(self.monitor.proxy_switches, 5)
        self.assertEqual(self.connect.call_count, 5)

    def test_failed_switch_requests_also_exhaust_switch_budget(self):
        self.connect.return_value = False
        for now in range(0, 6000, 60):
            if self.check(now) == "switch_limit":
                break
        else:
            self.fail("失败的连接请求未触发切网保护上限")
        self.assertEqual(self.monitor.proxy_switches, 5)
        self.assertEqual(self.connect.call_count, 5)
        self.assertEqual(self.check(now + 600), "switch_limit")
        self.assertEqual(self.connect.call_count, 5)

    def test_final_switch_timeout_still_rolls_back_without_using_budget(self):
        self.monitor.proxy_switches = 4
        self.switch_to_a()
        self.assertEqual(self.monitor.proxy_switches, 5)
        offline = snapshot("", connected=False, link_ready=False, default_route=False)
        self.assertEqual(self.check(90, wifi=offline), "rolling_back")
        self.assertEqual(self.connect.call_args.args, (B, "WLAN"))
        self.assertEqual(self.check(95, wifi=snapshot(B)), "switch_limit")
        self.assertEqual(self.monitor.proxy_switches, 5)
        self.assertEqual(self.connect.call_count, 2)

    def test_switch_limit_does_not_block_disconnection_recovery(self):
        now, profile = self.reach_switch_limit()
        offline = snapshot("", connected=False, link_ready=False, default_route=False)
        self.assertEqual(self.check(now + 60, wifi=offline), "reconnecting")
        self.assertEqual(self.connect.call_args.args, (profile, "WLAN"))
        self.assertEqual(self.check(now + 65, wifi=snapshot(profile)), "switch_limit")
        self.assertEqual(self.monitor.proxy_switches, 5)
        self.assertEqual(self.connect.call_count, 6)

    def test_request_guard_cannot_exceed_switch_budget(self):
        self.check(0, ProbeState.ONLINE)
        self.monitor.proxy_switches = 5
        self.assertEqual(self.monitor._request(A, switching=True)[0], "switch_limit")
        self.connect.assert_not_called()

    def test_cancelled_switch_does_not_use_budget(self):
        self.check(0)
        self.wifi.side_effect = [snapshot(B), snapshot("Other")]
        self.assertEqual(self.check(60), "unmanaged_wifi")
        self.assertEqual(self.monitor.proxy_switches, 0)
        self.connect.assert_not_called()

    def test_cooldown_recovery_stays_on_current_network(self):
        self.switch_to_a()
        self.check(65, wifi=snapshot(A))
        self.check(125)
        self.assertEqual(self.check(185, ProbeState.ONLINE), "online")
        self.assertEqual(self.monitor.cooldown_until, 0)
        self.check(500, ProbeState.ONLINE)
        self.connect.assert_called_once()

    def test_failed_switch_request_is_rate_limited(self):
        self.connect.return_value = False
        self.check(0)
        self.assertEqual(self.check(60), "rollback_ready")
        self.check(120)
        self.check(180)
        self.connect.assert_called_once()

    def test_target_without_ip_rolls_back_before_cooldown(self):
        self.switch_to_a()
        self.assertEqual(self.check(89, wifi=snapshot(A, link_ready=False)), "waiting_link")
        self.assertEqual(self.check(90), "rolling_back")
        self.assertEqual(self.connect.call_args.args, (B, "WLAN"))
        self.assertEqual(self.check(95, ProbeState.ONLINE, snapshot(B)), "rollback_online")
        self.assertEqual(self.check(155), "cooldown")
        self.assertEqual(self.check(215), "cooldown")
        self.assertEqual(self.connect.call_count, 2)
        self.auth.assert_not_called()

    def test_original_network_during_switch_is_not_accepted_as_target(self):
        self.switch_to_a()
        self.assertEqual(self.check(65), "waiting_link")
        self.assertEqual(self.check(90), "rollback_ready")
        self.connect.assert_called_once()
        self.auth.assert_not_called()

    def test_enterprise_auth_failure_returns_to_a_and_authenticates_only_after_ready(self):
        self.check(0, wifi=snapshot(A))
        self.check(60)
        disconnected = snapshot("", connected=False, link_ready=False, default_route=False)
        self.auth.reset_mock()
        self.assertEqual(self.check(90, wifi=disconnected), "rolling_back")
        self.assertEqual(self.connect.call_args.args, (A, "WLAN"))
        self.auth.assert_not_called()
        self.assertEqual(self.check(95, ProbeState.ONLINE, snapshot(A)), "rollback_online")
        self.auth.assert_called_once()
        self.assertGreater(self.monitor.connection_cooldown_until, 95)

    def test_rollback_unknown_state_is_retained_and_resumed_safely(self):
        self.switch_to_a()
        self.wifi.side_effect = [snapshot("", connected=False, link_ready=False), None]
        self.assertEqual(self.check(90), "rollback_waiting")
        self.assertEqual(self.monitor.rollback_due, B)
        self.connect.assert_called_once()
        self.wifi.side_effect = None
        self.assertEqual(self.check(95, wifi=snapshot("", connected=False, link_ready=False)), "rolling_back")
        self.assertEqual(self.connect.call_args.args, (B, "WLAN"))

    def test_rollback_failure_retries_source_during_cooldown_not_failed_target(self):
        self.check(0, wifi=snapshot(A))
        self.check(60)
        self.connect.return_value = False
        disconnected = snapshot("", connected=False, link_ready=False, default_route=False)
        self.assertEqual(self.check(90, wifi=disconnected), "rollback_failed")
        self.assertEqual(self.monitor.last_profile, A)
        self.assertEqual(self.check(100), "waiting_reconnect")
        self.assertEqual(self.connect.call_count, 2)
        self.connect.return_value = True
        self.assertEqual(self.check(120), "reconnecting")
        self.assertEqual(self.connect.call_args.args, (A, "WLAN"))
        self.assertGreater(self.monitor.connection_cooldown_until, 120)

    def test_manual_other_network_during_rollback_is_not_overwritten(self):
        self.switch_to_a()
        self.wifi.side_effect = [snapshot("", connected=False, link_ready=False), snapshot("Other")]
        self.assertEqual(self.check(90), "unmanaged_wifi")
        self.connect.assert_called_once()

    def test_rollback_does_not_restart_wifi_if_source_already_connected(self):
        self.switch_to_a()
        self.assertEqual(self.check(90, wifi=snapshot(B)), "rollback_ready")
        self.connect.assert_called_once()
        self.assertEqual(self.check(91, ProbeState.ONLINE), "online")
        self.check(151)
        self.assertEqual(self.check(211), "cooldown")
        self.connect.assert_called_once()

    def test_late_target_connection_during_rollback_check_is_not_disconnected(self):
        self.switch_to_a()
        self.wifi.side_effect = [snapshot("", connected=False, link_ready=False), snapshot(A)]
        self.assertEqual(self.check(90), "rollback_ready")
        self.connect.assert_called_once()
        self.assertEqual(self.monitor.last_profile, A)
        self.wifi.side_effect = None
        self.assertEqual(self.check(91, ProbeState.ONLINE, snapshot(A)), "online")

    def test_rollback_timeout_retries_only_source_and_is_rate_limited(self):
        self.check(0, wifi=snapshot(A))
        self.check(60)
        disconnected = snapshot("", connected=False, link_ready=False, default_route=False)
        self.assertEqual(self.check(90, wifi=disconnected), "rolling_back")
        self.assertEqual(self.check(120), "rollback_failed")
        self.assertEqual(self.check(125), "waiting_reconnect")
        self.assertEqual(self.connect.call_count, 2)
        self.assertEqual(self.check(150), "reconnecting")
        self.assertEqual(self.connect.call_args.args, (A, "WLAN"))

    def test_user_radio_off_cancels_rollback(self):
        self.switch_to_a()
        self.wifi.side_effect = [snapshot("", connected=False, link_ready=False), snapshot(radio_on=False)]
        self.assertEqual(self.check(90), "radio_off")
        self.connect.assert_called_once()
        self.assertEqual(self.monitor.rollback_due, "")

    def test_unknown_wifi_state_never_connects_and_breaks_streak(self):
        self.check(0)
        self.wifi.return_value = None
        self.assertEqual(self.check(60), "wifi_unknown")
        self.assertEqual(self.monitor.failures, 0)
        self.connect.assert_not_called()

    def test_unknown_radio_state_never_connects(self):
        self.assertEqual(self.check(0, wifi=snapshot(radio_on=None)), "wifi_unknown")
        self.connect.assert_not_called()
        self.probe.assert_not_called()

    def test_radio_off_does_not_reconnect_or_authenticate(self):
        self.assertEqual(self.check(0, wifi=snapshot(connected=False, radio_on=False)), "radio_off")
        self.connect.assert_not_called()
        self.auth.assert_not_called()
        self.probe.assert_not_called()

    def test_other_network_and_subsequent_disconnect_remain_unmanaged(self):
        self.check(0)
        self.assertEqual(self.check(60, wifi=snapshot("Other")), "unmanaged_wifi")
        self.assertEqual(self.check(120, wifi=snapshot("", connected=False)), "unmanaged_wifi")
        self.connect.assert_not_called()
        self.assertEqual(self.check(180, ProbeState.ONLINE, snapshot(B)), "online")

    def test_other_network_cancels_pending_switch(self):
        self.switch_to_a()
        self.assertEqual(self.check(65, wifi=snapshot("Other")), "unmanaged_wifi")
        self.assertEqual(self.monitor.pending, "")
        self.assertEqual(self.monitor.failures, 0)

    def test_disconnection_restores_last_managed_network_before_authentication(self):
        self.check(0, ProbeState.ONLINE)
        self.assertEqual(self.check(60, wifi=snapshot("", connected=False, link_ready=False)), "reconnecting")
        self.connect.assert_called_once_with(B, "WLAN")
        self.auth.assert_not_called()
        self.assertEqual(self.check(65, ProbeState.ONLINE, snapshot(B)), "online")

    def test_two_restore_timeouts_try_other_saved_network_without_proxy(self):
        self.check(0, ProbeState.ONLINE)
        offline = snapshot("", connected=False, link_ready=False, default_route=False)
        self.assertEqual(self.check(60, wifi=offline), "reconnecting")
        self.assertEqual(self.check(90), "connect_timeout")
        self.assertEqual(self.check(120), "reconnecting")
        self.assertEqual(self.check(150), "connect_timeout")
        self.assertEqual(self.check(180), "reconnecting")
        self.assertEqual(self.connect.call_args.args, (A, "WLAN"))
        self.assertEqual(self.connect.call_count, 3)
        self.auth.assert_not_called()

    def test_restore_request_errors_also_try_other_saved_network(self):
        self.check(0, ProbeState.ONLINE)
        self.connect.return_value = False
        offline = snapshot("", connected=False, link_ready=False, default_route=False)
        self.check(60, wifi=offline)
        self.check(90)
        self.check(120)
        self.assertEqual(self.connect.call_args.args, (A, "WLAN"))

    def test_unknown_ip_is_not_treated_as_disconnected(self):
        self.assertEqual(self.check(0, wifi=snapshot(link_ready=False, ip_known=False)), "ip_unknown")
        self.connect.assert_not_called()
        self.auth.assert_not_called()

    def test_windows_authentication_in_progress_gets_a_bounded_wait(self):
        joining = snapshot("", connected=False, link_ready=False, connecting=True)
        self.assertEqual(self.check(0, wifi=joining), "waiting_windows")
        self.assertEqual(self.check(29), "waiting_windows")
        self.connect.assert_not_called()
        self.assertEqual(self.check(30), "reconnecting")

    def test_boot_mode_maintains_a_authentication_without_proxy(self):
        self.monitor.probe_enabled = False
        self.assertEqual(self.check(0, wifi=snapshot(A)), "boot_online")
        self.auth.assert_called_once()
        self.probe.assert_not_called()

    def test_boot_mode_b_never_uses_portal_or_proxy(self):
        self.monitor.probe_enabled = False
        self.assertEqual(self.check(0, wifi=snapshot(B)), "boot_online")
        self.auth.assert_not_called()
        self.probe.assert_not_called()

    def test_missing_saved_profile_never_requests_connection(self):
        self.assertEqual(self.check(0, wifi=snapshot(profiles=frozenset((B,)))), "missing_profile")
        self.connect.assert_not_called()

    def test_ethernet_default_route_suspends_failover(self):
        self.check(0)
        self.assertEqual(self.check(60, wifi=snapshot(default_route=False)), "other_default_route")
        self.assertEqual(self.monitor.failures, 0)
        self.connect.assert_not_called()

    def test_manual_change_between_managed_networks_resets_streak(self):
        self.check(0)
        self.assertEqual(self.check(60, wifi=snapshot(A)), "proxy_failed")
        self.assertEqual(self.monitor.failures, 1)
        self.connect.assert_not_called()

    def test_user_changes_network_during_probe_request_is_cancelled(self):
        self.check(0)
        self.wifi.side_effect = [snapshot(B), snapshot("Other")]
        self.assertEqual(self.check(60), "unmanaged_wifi")
        self.connect.assert_not_called()

    def test_user_turns_radio_off_during_probe_request_is_cancelled(self):
        self.check(0)
        self.wifi.side_effect = [snapshot(B), snapshot(connected=False, radio_on=False)]
        self.assertEqual(self.check(60), "radio_off")
        self.connect.assert_not_called()

    def test_managed_network_changes_during_probe_request_is_cancelled(self):
        self.check(0)
        self.wifi.side_effect = [snapshot(B), snapshot(A)]
        self.assertEqual(self.check(60), "wifi_changed")
        self.connect.assert_not_called()

    def test_route_changes_during_probe_request_is_cancelled(self):
        self.check(0)
        self.wifi.side_effect = [snapshot(B), snapshot(default_route=False)]
        self.assertEqual(self.check(60), "other_default_route")
        self.connect.assert_not_called()


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(failover.failover_enabled())
            config = failover.FailoverConfig.from_env("WLAN")
        self.assertEqual((config.interval, config.threshold, config.cooldown), (60, 2, 300))
        self.assertEqual(config.proxy_max_switches, 5)

    def test_proxy_switch_limit_can_be_configured(self):
        with patch.dict(os.environ, {"PROXY_MAX_SWITCHES": "3"}, clear=True):
            self.assertEqual(failover.FailoverConfig.from_env("WLAN").proxy_max_switches, 3)

    def test_invalid_configuration_is_rejected_without_side_effects(self):
        for overrides in ({"PROXY_CHECK_INTERVAL": "0"}, {"PROXY_FAILURE_THRESHOLD": "-1"},
                          {"PROXY_MAX_SWITCHES": "0"}, {"PROXY_MAX_SWITCHES": "-1"},
                          {"PROXY_MAX_SWITCHES": "101"}, {"PROXY_MAX_SWITCHES": "nan"},
                          {"PROXY_CHECK_TIMEOUT": "nan"}, {"ZJU_WIFI_PROFILE_B": A},
                          {"PROXY_CHECK_URLS": "http://one.example,https://two.example"}):
            with self.subTest(overrides=overrides), patch.dict(os.environ, overrides, clear=True):
                with self.assertRaises(ValueError):
                    failover.FailoverConfig.from_env("WLAN")
        with self.assertRaises(ValueError):
            failover.FailoverConfig.from_env("")


class HostFailoverIntegrationTests(unittest.TestCase):
    def setUp(self):
        patch.dict(os.environ, {"ZJU_WIFI_FAILOVER": "1", "UU_REMOTE_MONITOR": "0"}, clear=True).start()
        self.monitor = host.HostMonitor(interface="WLAN")
        self.addCleanup(patch.stopall)

    def test_b_does_not_construct_portal_client_even_when_proxy_fails(self):
        with patch.object(failover, "wifi_snapshot", return_value=snapshot()), \
                patch.object(failover, "proxy_available", return_value=ProbeResult(ProbeState.FAILED, "timeout")), \
                patch.object(self.monitor, "client") as client, patch.object(host, "internet_available") as direct:
            self.assertEqual(self.monitor.check_once()[0], "proxy_failed")
        client.assert_not_called()
        direct.assert_not_called()

    def test_a_authentication_expiry_is_recovered_before_proxy_probe(self):
        self.monitor.username, self.monitor.password = "sample", "dummy"
        client = Mock()
        client.is_online.return_value = False
        with patch.object(self.monitor, "client", return_value=client), \
                patch.object(failover, "wifi_snapshot", return_value=snapshot(A)), \
                patch.object(failover, "proxy_available", return_value=ProbeResult(ProbeState.ONLINE, "ok")):
            self.assertEqual(self.monitor.check_once()[0], "online")
        client.reconnect.assert_called_once_with("sample", "dummy", initial_status=client.status.return_value)

    def test_portal_failure_does_not_prevent_proxy_failure_detection(self):
        from srun_client import PortalUnavailable
        with patch.object(self.monitor, "client", side_effect=PortalUnavailable("timeout")), \
                patch.object(failover, "wifi_snapshot", return_value=snapshot(A)), \
                patch.object(failover, "proxy_available", return_value=ProbeResult(ProbeState.FAILED, "timeout")):
            self.assertEqual(self.monitor.check_once()[0], "proxy_failed")

    def test_diagnose_is_local_only_and_never_uses_portal_or_external_probe(self):
        with patch.object(self.monitor, "client") as client, \
                patch.object(host, "internet_available") as direct, \
                patch("network_checks.proxy_available") as proxy, \
                patch("network_checks.system_proxy", return_value=None), \
                patch("wifi_control.wifi_snapshot", return_value=snapshot()):
            self.monitor.diagnose()
        client.assert_not_called()
        direct.assert_not_called()
        proxy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
