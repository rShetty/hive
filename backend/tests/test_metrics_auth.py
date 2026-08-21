"""Issue #15: /api/metrics must require admin authentication."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."
))
from middleware.monitoring import Metrics  # noqa: E402

_MAIN = open(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "main.py"
)).read()


class TestMetricsAuth(unittest.TestCase):
    def test_metrics_endpoint_has_admin_gate(self):
        segment = _MAIN.split('@app.get("/api/metrics")')[1][:600]
        self.assertIn("get_current_admin_user", segment,
                      "/api/metrics lost its admin gate")

    def test_prometheus_rendering(self):
        m = Metrics()
        m.record_request("/api/health", 200)
        m.record_delegation(True, tokens=1.5)
        text = m.render_prometheus()
        self.assertIn("hive_requests_total 1", text)
        self.assertIn('hive_requests_by_status{status="200"} 1', text)
        self.assertIn("hive_delegations_success_total 1", text)


if __name__ == "__main__":
    unittest.main()
