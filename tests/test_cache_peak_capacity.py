import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from crange_distribution import preparation_peak_capacity, DistributionError


class CachePeakCapacityTests(unittest.TestCase):
    def test_serial_peak_retains_base_without_overlapping_probe_and_future_guests(self):
        cache={'memory_mb':4096,'cpu_cores':2,'disk_gb':134.4,'base_disk_gb':64}
        result=preparation_peak_capacity(cache,{'memory_mb':4096,'cpu_cores':2,'disk_gb':70.4})
        self.assertEqual(result['memory_mb'],4096)
        self.assertEqual(result['cpu_cores'],2)
        self.assertAlmostEqual(result['disk_gb'],134.4)
        result=preparation_peak_capacity(cache,{'memory_mb':8192,'cpu_cores':4,'disk_gb':140.8})
        self.assertEqual(result['memory_mb'],8192)
        self.assertAlmostEqual(result['disk_gb'],204.8)

    def test_large_probe_still_determines_peak_even_for_a_small_future_guest(self):
        cache={'memory_mb':4096,'cpu_cores':2,'disk_gb':134.4,'base_disk_gb':64}
        self.assertEqual(preparation_peak_capacity(cache,{'memory_mb':512,'cpu_cores':1,'disk_gb':8})['disk_gb'],134.4)

    def test_malformed_additional_demand_is_rejected(self):
        for value in (True,-1,float('inf'),'64'):
            with self.subTest(value=value),self.assertRaises(DistributionError):
                preparation_peak_capacity({'memory_mb':512,'cpu_cores':1,'disk_gb':16.8,'base_disk_gb':8},
                                          {'disk_gb':value})
