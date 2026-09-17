from __future__ import annotations

import socket
import unittest
from unittest import mock

from aimem import dashboard


class TestDashboardPublicSafety(unittest.TestCase):
    def test_loopback_server_startup_does_not_use_reverse_dns(self):
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("reverse DNS must not run")):
            server = dashboard.LocalThreadingHTTPServer((dashboard.HOST, 0), dashboard.Handler)
            try:
                self.assertEqual(server.server_name, dashboard.HOST)
                self.assertEqual(server.server_address[0], dashboard.HOST)
                self.assertGreater(server.server_port, 0)
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
