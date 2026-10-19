"""Selected release reviews must not reserve or plan unrelated guests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange
import crange_rollouts as rollouts
from crange_jobs import command


class ReleaseReviewTests(unittest.TestCase):
    def fixture(self):
        cfg = {'project_id': 100, 'team_ids': ['1','2'],
               'scenario_boxes': {'one': {'team_id':'1','release_scope':'team','release_id':'p100-1'},
                                  'stage': {'team_id':'254'}, 'manual': {'team_id':'1'}},
               'scenario_containers': {}, 'custom_networks': {},
               'jump_boxes': {'jump': {'team_id':'1'}}}
        rollouts.declare(cfg, '1', {'release_id':'p100-1','version':'1'}, 'deploy')
        return cfg

    def test_projection_preserves_inputs_and_excludes_manual_and_jump_guests(self):
        cfg=self.fixture();before=copy.deepcopy(cfg)
        scoped=rollouts.review_projection(cfg,['1'])
        self.assertEqual(set(scoped['scenario_boxes']), {'one'})
        self.assertEqual(scoped['jump_boxes'], {})
        scoped['scenario_boxes']['one']['name']='changed'
        self.assertEqual(cfg,before)

    def test_unknown_duplicate_staging_undeclared_and_drifted_teams_rejected(self):
        cfg=self.fixture()
        for teams in ([],['1','1'],['254'],['3'],['2']):
            with self.assertRaises(ValueError):rollouts.review_projection(cfg,teams)
        cfg['scenario_boxes']['one']['name']='drift'
        with self.assertRaisesRegex(ValueError,'drifted'):
            rollouts.review_projection(cfg,['1'])

    def test_targets_include_prior_removed_resources_but_no_manual_guests(self):
        cfg=self.fixture();r=cfg['release_rollouts']['1']
        r['applied']={'resources':{'scenario_boxes':{'removed':{}},'scenario_containers':{},'custom_networks':{'oldnet':{}}}}
        targets=rollouts.review_targets(cfg,['1'])
        self.assertIn('module.scenario_box["removed"]',targets)
        self.assertIn('module.scenario_box["one"]',targets)
        self.assertIn('module.custom_router["oldnet"]',targets)
        self.assertIn('proxmox_sdn_vnet.custom["oldnet"]',targets)
        self.assertFalse(any('jump' in t or 'manual' in t or 'stage' in t for t in targets))

    def test_fixed_api_command_validates_team_values(self):
        self.assertEqual(command('release-stage',100,{'teams':[1,2]}),
                         ['release-stage','100','--teams','1','2'])
        for value in ([],[0],[254],[True],['1;id']):
            with self.assertRaises(ValueError):command('release-stage',100,{'teams':value})

    def test_release_stage_passes_only_owned_images_to_preparation(self):
        cfg=self.fixture()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(crange,'project_dir',return_value=Path(directory)), \
             patch.object(crange,'json_config',return_value=cfg), \
             patch('crange_scheduling.read_settings',return_value=({'enabled':True},{})), \
             patch('crange_scheduling.declarations',return_value=[{'image':'release-image'}]), \
             patch('crange_cache_jobs.resume_project_snapshot_caches') as resume, \
             patch('crange_scheduling.required_preparations',return_value=[]) as required, \
             patch.object(crange,'cmd_plan') as plan:
            crange.cmd_release_stage(SimpleNamespace(project='100',teams=['1']))
        resume.assert_called_once_with(100,{'release-image'})
        self.assertEqual(set(required.call_args.args[1]['scenario_boxes']),{'one'})
        self.assertEqual(plan.call_args.args[0].release_teams,['1'])

    def test_release_review_rejects_actual_terraform_changes_to_unowned_guest(self):
        cfg=self.fixture()
        change={'resource_changes':[{'address':'module.scenario_box["manual"].vm',
            'change':{'actions':['create'],'before':None,'after':{'network_device':[{'bridge':'p100t001'}]}}}]}
        self.assertEqual(rollouts.plan_blockers(change,cfg,['1']),['module.scenario_box["manual"].vm'])

    def test_command_reviews_actual_plan_and_targets_only_recorded_release(self):
        cfg=self.fixture()
        for key, allowed in [('one', True), ('manual', False)]:
            detail={'resource_changes':[{'address':f'module.scenario_box["{key}"].vm',
                'change':{'actions':['create'],'before':None,'after':{'network_device':[{'bridge':'p100t001'}]}}}]}
            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(crange,'project_dir',return_value=Path(directory)), \
                 patch.object(crange,'json_config',return_value=cfg), \
                 patch('crange_scheduling.prepare',return_value=None) as prepare, \
                 patch('crange_scheduling.print_review'), \
                 patch('crange_scheduling.abandon_plan'), \
                 patch('crange_templates.media_tf_overrides',return_value={}) as media, \
                 patch.object(crange,'tf_run',return_value=Mock(stdout=json.dumps(detail))) as tf:
                args=SimpleNamespace(project='100',release_teams=['1'],retain=False)
                if allowed:crange.cmd_plan(args)
                else:
                    with self.assertRaisesRegex(crange.CrangeError,'outside its selected releases'):
                        crange.cmd_plan(args)
                self.assertEqual(set(prepare.call_args.args[1]['scenario_boxes']),{'one'})
                self.assertEqual(media.call_args.args[1],cfg)
                self.assertTrue(media.call_args.kwargs['scoped'])
                plan_call=next(call for call in tf.call_args_list if call.args[1][0]=='plan')
                self.assertIn('-target=module.scenario_box["one"]',plan_call.args[1])
                self.assertFalse(any('manual' in v for v in plan_call.args[1]))

if __name__=='__main__':unittest.main()
