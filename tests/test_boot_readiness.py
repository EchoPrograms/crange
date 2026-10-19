from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange


class BootReadinessTests(unittest.TestCase):
    def test_startup_retries_preserve_probe_settings(self):
        check = {'type': 'tcp', 'port': 22, 'timeout': 10}
        with patch.object(crange, 'check_endpoint', side_effect=[crange.CrangeError('not ready'), None]) as probe, \
             patch.object(crange.time, 'monotonic', side_effect=[0, 11]), \
             patch.object(crange.time, 'sleep') as sleep:
            crange.wait_for_boot_health(240, '254', '10.240.101.20', check)
        self.assertEqual(probe.call_count, 2)
        self.assertTrue(all(call.args == (240, '254', '10.240.101.20', check)
                            for call in probe.call_args_list))
        sleep.assert_called_once_with(3)

    def test_service_that_never_starts_still_fails(self):
        with patch.object(crange, 'check_endpoint', side_effect=crange.CrangeError('connection refused')) as probe, \
             patch.object(crange.time, 'monotonic', side_effect=[0, 11, 181]), \
             patch.object(crange.time, 'sleep'):
            with self.assertRaisesRegex(crange.CrangeError, 'not ready within 180s.*connection refused'):
                crange.wait_for_boot_health(240, '254', '10.240.101.20', {'type': 'tcp', 'port': 22})
        self.assertEqual(probe.call_count, 2)

    def test_windows_policy_startup_can_complete_after_linux_budget(self):
        check={'type':'tcp','port':389,'timeout':10}
        with patch.object(crange,'check_endpoint',side_effect=[crange.CrangeError('booting'),None]) as probe,patch.object(crange.time,'monotonic',side_effect=[0,240]),patch.object(crange.time,'sleep'):
            crange.wait_for_boot_health(100,'254','10.100.100.10',check,startup_timeout=crange.guest_startup_timeout({'os_type':'win11'}))
        self.assertEqual(probe.call_count,2)
        self.assertTrue(all(call.args[-1]==check for call in probe.call_args_list))
        self.assertEqual(crange.guest_startup_timeout({'os_type':'l26'}),180)

    def test_later_probes_cannot_extend_a_guests_original_startup_deadline(self):
        with patch.object(crange,'check_endpoint',side_effect=crange.CrangeError('not ready')) as probe,patch.object(crange.time,'monotonic',side_effect=[590,601]),patch.object(crange.time,'sleep') as sleep:
            with self.assertRaisesRegex(crange.CrangeError,'within 600s'):
                crange.wait_for_boot_health(100,'254','10.100.100.10',{'type':'tcp','port':389},startup_timeout=600,startup_deadline=600)
        self.assertEqual(probe.call_count,1);sleep.assert_not_called()
