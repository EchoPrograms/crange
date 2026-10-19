import copy
import pathlib
import sys
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from crange_mhl_test_scope import select,ROLES


def fixture():
    net={'network_id':100,'owner_team_id':'254','release_scope':'test','release_id':'p100-1.0.0'}
    cfg={'project_id':100,'custom_networks':{'test-primary':copy.deepcopy(net)}}
    release={'project_id':100,'release_id':'p100-1.0.0','scenario':{'templates':{}}}
    record={'release_id':'p100-1.0.0','networks':{'test-primary':copy.deepcopy(net)}}
    for group,roles in ROLES.items():
        field='scenario_boxes' if group=='boxes' else 'scenario_containers'
        cfg[field]={};release['scenario'][group]=[];record[group]={}
        for name,octet in roles.items():
            key='test-'+name+'-t254';template='captured-'+name
            release['scenario']['templates'][template]={'image_id':template}
            release['scenario'][group].append({'name':name,'template':template,'ip_last_octet':octet})
            cfg[field][key]={'team_id':'254','release_id':'p100-1.0.0','release_scope':'test',
                'release_source_name':name,'network_id':100,'network_key':'test-primary',
                'ip_last_octet':octet,'template':template,'image':template}
            record[group][key]={'address':f'10.100.100.{octet}'}
    return cfg,release,record


class TestScopeTests(unittest.TestCase):
    def test_exact_cohort_infers_subnet_and_every_key(self):
        cfg,release,record=fixture();scope=select(cfg,release,record)
        self.assertEqual(scope['network_id'],100);self.assertEqual(len(scope['keys']),6)
        self.assertNotIn('t254-mhl-dc01',scope['keys'].values())

    def test_foreign_staging_team_image_address_or_source_refused(self):
        mutations=[('team_id','1'),('release_scope','team'),('release_id','foreign'),
                   ('image','wrong'),('ip_last_octet',12),('network_key','staging'),
                   ('release_source_name','not-in-release')]
        for field,value in mutations:
            cfg,release,record=fixture();cfg['scenario_boxes']['test-mhl-dc01-t254'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):select(cfg,release,record)
        cfg,release,record=fixture();record['boxes']['test-mhl-dc01-t254']['address']='10.100.254.10'
        with self.assertRaises(ValueError):select(cfg,release,record)

    def test_partial_cohort_or_changed_network_record_refused(self):
        cfg,release,record=fixture();record['containers'].pop('test-mhl-db01-t254')
        with self.assertRaises(ValueError):select(cfg,release,record)
        cfg,release,record=fixture();cfg['custom_networks']['test-primary']['network_id']=101
        with self.assertRaises(ValueError):select(cfg,release,record)

    def test_non_project100_profile_refused(self):
        cfg,release,record=fixture();cfg['project_id']=3
        with self.assertRaises(ValueError):select(cfg,release,record)
