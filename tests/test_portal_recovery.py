import unittest
from unittest.mock import patch

from srun_client import PortalError, SrunClient


class PortalRecoveryTests(unittest.TestCase):
    def test_api_login_uses_server_ip_and_verifies_authentication(self):
        class ServerClient(SrunClient):
            authenticated = False
            submitted = None

            def request(self, endpoint, params=None, origin=None):
                self.origin = "http://net.zju.edu.cn"
                if endpoint == "rad_user_info":
                    if self.authenticated:
                        return {"error": "ok", "user_name": "sample-account", "domain": ""}
                    return {"error": "not_online_error"}
                if endpoint == "get_challenge":
                    return {"error": "ok", "challenge": "0123456789abcdef", "client_ip": "192.0.2.20"}
                if endpoint == "srun_portal":
                    self.submitted = params
                    self.authenticated = True
                    return {"error": "ok"}
                raise AssertionError(endpoint)

        client = ServerClient(["http://net.zju.edu.cn/srun_portal_pc?ac_id=1"])
        self.assertTrue(client.reconnect("sample-account", "dummy-password"))
        self.assertEqual(client.submitted["ip"], "192.0.2.20")
        self.assertNotIn("dummy-password", client.submitted["password"])
        self.assertNotIn("dummy-password", client.submitted["info"])

    def test_success_response_without_online_state_does_not_pass(self):
        class NoAuthenticationClient(SrunClient):
            def request(self, endpoint, params=None, origin=None):
                self.origin = "http://net.zju.edu.cn"
                if endpoint == "rad_user_info":
                    return {"error": "not_online_error"}
                if endpoint == "get_challenge":
                    return {"error": "ok", "challenge": "0123456789abcdef", "client_ip": "192.0.2.20"}
                return {"error": "ok"}

        client = NoAuthenticationClient(["http://net.zju.edu.cn/"], acid="82")
        with patch("srun_client.time.sleep"), self.assertRaisesRegex(PortalError, "未确认上线"):
            client.reconnect("sample-account", "dummy-password")


if __name__ == "__main__":
    unittest.main()
