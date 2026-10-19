"""Fixed Project 100 MHL business-service job safety checks."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from scripts import crange_jobs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("crange_mhl_services", ROOT / "scripts/crange_mhl_services.py")
SERVICES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVICES)


def deployed_fixture():
    labels = {
        "mhl-db01": ("MHL-DB01", 30, "mhl_databases"),
        "mhl-app01": ("MHL-APP01", 40, "mhl_applications"),
        "mhl-ci01": ("MHL-CI01", 50, "mhl_build"),
        "mhl-bkp01": ("MHL-BKP01", 60, "mhl_backups"),
    }
    hosts, scoped, applied = {}, {}, {}
    for key, (hostname, octet, group) in labels.items():
        guest_key = f"t254-{key}"
        guest_id = octet + 100
        image = f"p100-image-{key}"
        template_id = 4000 + octet
        fields = {
            "ansible_host": f"10.100.254.{octet}",
            "crange_project_id": 100,
            "crange_box_key": guest_key,
            "crange_guest_id": guest_id,
            "crange_image_id": image,
            "crange_template_id": template_id,
            "crange_template_node": "proxmox1",
            "crange_node_name": "proxmox2",
            "crange_template": "debian-13",
        }
        hosts[f"p100_ct_{guest_key}"] = fields
        scoped[f"p100_ct_{guest_key}"] = fields
        applied[f"container:{guest_key}"] = {
            "guest_id": guest_id,
            "image_id": image,
            "template_id": template_id,
            "template_node": "proxmox1",
            "node_name": "proxmox2",
            "declared_template": "debian-13",
        }
    common = {"ansible_connection": "ssh", "ansible_user": "user", "ansible_become": True,
              "ansible_ssh_private_key_file": SERVICES.KEY}
    canonical = {"all": {"vars": {}, "children": {"scenario_containers": {
        "vars": common, "hosts": hosts, "children": {"staging_containers": {"hosts": scoped}},
    }}}}
    return canonical, applied


def windows_fixture(os_type="win11"):
    boxes, hosts, scoped, applied = {}, {}, {}, {}
    for marker, octet in (("mhl-dc01", 10), ("mhl-file01", 20)):
        key = f"t254-{marker}"
        host_name = f"p100_vm_{key}"
        guest_id, image_id, template_id = octet + 100, f"p100-win-{marker}", 7000 + octet
        box = {"team_id": "254", "template": "mhl-windows-base"}
        boxes[key] = box
        hosts[host_name] = {
            "ansible_host": f"10.100.254.{octet}",
            "crange_project_id": 100, "crange_box_key": key,
            "crange_guest_id": guest_id, "crange_image_id": image_id,
            "crange_template": box["template"], "crange_template_id": template_id,
            "crange_template_node": "proxmox1", "crange_node_name": "proxmox2",
            "crange_guest_network_id": "254",
        }
        scoped[host_name] = hosts[host_name]
        applied[f"vm:{key}"] = {"guest_id": guest_id, "image_id": image_id,
                                 "declared_template": box["template"], "template_id": template_id,
                                 "template_node": "proxmox1", "node_name": "proxmox2"}
    canonical = {"all": {"vars": {}, "children": {"windows_guests": {
        "vars": {"ansible_connection": "winrm", "ansible_port": 5985,
                 "ansible_winrm_transport": "ntlm", "ansible_winrm_message_encryption": "always"},
        "hosts": hosts,
        "children": {"windows_vms": {"children": {"staging_windows": {"hosts": scoped}}},
                     "windows_jumpboxes": {"children": {}}},
    }}}}
    path = {"config": {"project_id": 100, "team_ids": ["1"], "scenario_boxes": boxes},
            "applied": applied,
            "approvals": {"mhl-windows-base": {"profile": {"os_type": os_type, "cloud_init": False}}}}
    return canonical, path


class MhlServiceJobTests(unittest.TestCase):
    def test_service_actions_refresh_inventory_before_touching_guests(self):
        import crange
        from types import SimpleNamespace
        for action, function in ((crange.cmd_mhl_business_configure, 'configure'),
                                 (crange.cmd_mhl_windows_configure, 'configure_windows')):
            calls=[]
            with patch.object(crange,'project_dir',return_value=Path('/project')), \
                 patch.object(crange,'cmd_inventory',side_effect=lambda args:calls.append('inventory')), \
                 patch('crange_mhl_services.'+function,side_effect=lambda *a,**k:calls.append('configuration')):
                action(SimpleNamespace(project='100',network_id=254,yes=True))
            self.assertEqual(calls,['inventory','configuration'])

    def test_release_recipe_hash_binds_guest_source_and_rejects_changed_or_foreign_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            play = root / 'ansible/linux/project-100-business.yml'
            play.parent.mkdir(parents=True)
            play.write_text('fixed playbook')
            task = root / 'ansible/roles/mhl_database/tasks/main.yml'
            task.parent.mkdir(parents=True)
            task.write_text('version one')
            with patch.object(SERVICES, 'ROOT', root):
                digest = SERVICES.recipe_digest('business')
                release = {'project_id':100, 'scenario':{'service_profiles':{'business':{
                    'profile':SERVICES.PROFILE_IDS['business'], 'source_sha256':digest}}}}
                self.assertEqual(SERVICES.verify_release_profiles(release), ['business'])
                with self.assertRaises(SERVICES.ServiceConfigurationError):
                    SERVICES.verify_release_profiles({**release, 'project_id':3})
                task.write_text('version two')
                with self.assertRaisesRegex(SERVICES.ServiceConfigurationError, 'recipe changed'):
                    SERVICES.verify_release_profiles(release)

    def test_job_schema_is_fixed_and_project_scoped(self):
        vectors = [
            crange_jobs.command("mhl-business-configure", 100, {"network_id": 254}),
            crange_jobs.command("mhl-windows-configure", 100, {"network_id": 1}),
            crange_jobs.command("configure", 100, {"target": "windows"}),
        ]
        self.assertEqual(vectors[0], ["mhl-business-configure", "100", "--network-id", "254", "--yes"])
        self.assertEqual(vectors[1], ["mhl-windows-configure", "100", "--network-id", "1", "--yes"])
        for vector in vectors:
            self.assertTrue(all(isinstance(argument, str) for argument in vector), vector)
        self.assertEqual(vectors[2], ["configure", "100", "windows", "--yes"])
        with self.assertRaisesRegex(ValueError, "Project 100"):
            crange_jobs.command("mhl-business-configure", 3, {"network_id": 254})
        with self.assertRaisesRegex(ValueError, "Unexpected or missing"):
            crange_jobs.command("mhl-business-configure", 100, {"network_id": 254, "playbook": "bad.yml"})

    def test_requires_all_four_exact_applied_guest_identities(self):
        canonical, applied = deployed_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "crange.auto.tfvars.json").write_text(json.dumps({"project_id": 100, "team_ids": ["1", "2"]}))
            (path / "crange-applied-placements.json").write_text(json.dumps(applied))
            selected = SERVICES._validate_selected(path, 254, canonical)
            self.assertIs(selected["all"]["vars"]["mhl_test_scope"], False)
            self.assertEqual(set(selected["all"]["children"]), {
                "mhl_databases", "mhl_applications", "mhl_build", "mhl_backups",
            })
            changed = json.loads(json.dumps(canonical))
            changed["all"]["children"]["scenario_containers"]["hosts"]["p100_ct_t254-mhl-db01"]["crange_guest_id"] += 1
            with self.assertRaisesRegex(SERVICES.ServiceConfigurationError, "Applied identity"):
                SERVICES._validate_selected(path, 254, changed)

    def test_root_login_requires_applied_stock_archive_provenance(self):
        canonical, applied = deployed_fixture()
        children = canonical['all']['children']['scenario_containers']
        for host in children['hosts'].values():
            host['ansible_user'] = 'root'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'crange.auto.tfvars.json').write_text(json.dumps({'project_id': 100, 'team_ids': ['1']}))
            record = path / 'crange-applied-placements.json'
            record.write_text(json.dumps(applied))
            with self.assertRaisesRegex(SERVICES.ServiceConfigurationError, 'Applied identity'):
                SERVICES._validate_selected(path, 254, canonical)
            for value in applied.values():
                value['template_file_id'] = 'crange-library:vztmpl/debian.tar.zst'
            record.write_text(json.dumps(applied))
            self.assertEqual(len(SERVICES._validate_selected(path, 254, canonical)['all']['children']), 4)

    def test_captured_root_management_requires_approved_template_profile(self):
        canonical, applied = deployed_fixture()
        for host in canonical['all']['children']['scenario_containers']['hosts'].values():
            host['ansible_user'] = 'root'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'crange.auto.tfvars.json').write_text(json.dumps({'project_id':100,'team_ids':['1']}))
            (path / 'crange-applied-placements.json').write_text(json.dumps(applied))
            with self.assertRaises(SERVICES.ServiceConfigurationError):
                SERVICES._validate_selected(path,254,canonical)
            (path / 'approved-container-templates.json').write_text(json.dumps({'debian-13':{'profile':{'management_user':'root'}}}))
            self.assertEqual(len(SERVICES._validate_selected(path,254,canonical)['all']['children']),4)

    def test_missing_guest_is_actionable_and_secrets_are_private(self):
        canonical, applied = deployed_fixture()
        del canonical["all"]["children"]["scenario_containers"]["hosts"]["p100_ct_t254-mhl-ci01"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "crange.auto.tfvars.json").write_text(json.dumps({"project_id": 100, "team_ids": ["1"]}))
            (path / "crange-applied-placements.json").write_text(json.dumps(applied))
            with self.assertRaisesRegex(SERVICES.ServiceConfigurationError, "all four deployed guests"):
                SERVICES._validate_selected(path, 254, canonical)
            vars_path = SERVICES._private_vars(254, path)
            self.assertEqual(os.stat(vars_path).st_mode & 0o777, 0o600)
            self.assertNotIn(str(vars_path), vars_path.read_text())

    def test_windows_role_binding_is_exact_os_and_network_scoped(self):
        canonical, values = windows_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "crange.auto.tfvars.json").write_text(json.dumps(values["config"]))
            (path / "crange-applied-placements.json").write_text(json.dumps(values["applied"]))
            (path / "approved-scenario-templates.json").write_text(json.dumps(values["approvals"]))
            selected, roles = SERVICES._windows_scope_hosts(path, 254, canonical)
            self.assertTrue(all(row['values']['mhl_test_scope'] is False for row in roles.values()))
            self.assertEqual(set(selected["all"]["children"]), {"mhl_domain_controllers", "mhl_domain_members"})
            self.assertEqual(roles["mhl_file_servers"]["values"]["mhl_dc_dns"], "10.100.254.10")
            (path / "approved-scenario-templates.json").write_text(json.dumps({"mhl-windows-base": {"profile": {"os_type": "l26", "cloud_init": False}}}))
            with self.assertRaisesRegex(SERVICES.ServiceConfigurationError, "approved Windows OS profile"):
                SERVICES._windows_scope_hosts(path, 254, canonical)

    def test_windows_team_release_roles_use_recorded_source_not_key_tokens(self):
        canonical, values = windows_fixture()
        group = canonical["all"]["children"]["windows_guests"]
        hosts, boxes, applied = {}, {}, {}
        for old_host, source in group["hosts"].items():
            old_key = source["crange_box_key"]
            marker = old_key.removeprefix("t254-")
            key = f"release-hashed-{source['crange_guest_id']}-t001"
            host = f"p100_vm_{key}"
            box = dict(values["config"]["scenario_boxes"][old_key])
            box.update(team_id="1", release_scope="team", release_id="fixture-release",
                       release_source_name=marker)
            boxes[key] = box
            hosts[host] = {**source, "ansible_host": source["ansible_host"].replace(".254.", ".1."),
                           "crange_box_key": key, "crange_guest_network_id": "1"}
            applied["vm:"+key] = values["applied"]["vm:"+old_key]
        values["config"]["scenario_boxes"] = boxes
        group["hosts"] = hosts
        group["children"]["windows_vms"]["children"] = {"team_001_windows": {"hosts": hosts}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for filename, value in [("crange.auto.tfvars.json", values["config"]),
                                    ("crange-applied-placements.json", applied),
                                    ("approved-scenario-templates.json", values["approvals"])]:
                (path/filename).write_text(json.dumps(value))
            selected, roles = SERVICES._windows_scope_hosts(path, 1, canonical)
            self.assertEqual(set(roles), {"mhl_domain_controllers", "mhl_file_servers"})
            self.assertEqual(roles["mhl_file_servers"]["values"]["mhl_dc_dns"], "10.100.1.10")
            boxes[next(iter(boxes))]["release_source_name"] = "unrelated-server"
            (path/"crange.auto.tfvars.json").write_text(json.dumps(values["config"]))
            with self.assertRaisesRegex(SERVICES.ServiceConfigurationError, "exactly one"):
                SERVICES._windows_scope_hosts(path, 1, canonical)

    def test_windows_forest_vault_is_stable_for_one_applied_dc_identity(self):
        vault_documents = {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = {"username": "Administrator", "password": "SYNTHETIC-WINDOWS-PASSWORD-ONLY"}
            # Derive the exact expected vault basename without exposing any runtime credentials.
            forest_key = __import__("hashlib").sha256(b"100:254:t254-mhl-dc01:110:p100-win-mhl-dc01").hexdigest()[:16]
            final = root / "project-100" / f"mhl-windows-network-254-{forest_key}.vault.yml"
            def runner(command, **kwargs):
                if command[1] == "encrypt":
                    plaintext = Path(command[-1]).read_text()
                    encrypted = Path(command[command.index("--output") + 1])
                    encrypted.write_text("$ANSIBLE_VAULT;1.1;AES256\nfixture-only\n")
                    vault_documents[str(final)] = plaintext
                    from subprocess import CompletedProcess
                    return CompletedProcess(command, 0, "", "")
                if command[1] == "view":
                    from subprocess import CompletedProcess
                    return CompletedProcess(command, 0, vault_documents[str(final)], "")
                raise AssertionError("unexpected ansible-vault command")
            first = SERVICES._windows_forest_vault(254, "t254-mhl-dc01", 110,
                "p100-win-mhl-dc01", credential, root, run=runner)
            second = SERVICES._windows_forest_vault(254, "t254-mhl-dc01", 110,
                "p100-win-mhl-dc01", credential, root, run=runner)
            self.assertEqual(first[2]["mhl_domain_admin_password"], credential["password"])
            self.assertEqual(first[2]["mhl_dsrm_password"], second[2]["mhl_dsrm_password"])
            self.assertEqual(first[0], second[0])
            self.assertEqual(os.stat(first[0]).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

class DisposableScopeSelectionTests(unittest.TestCase):
    def test_linux_replay_selects_only_exact_test_keys_among_staging_hosts(self):
        import copy
        canonical,applied=deployed_fixture();group=canonical['all']['children']['scenario_containers']
        scope={'release_id':'p100-1.0.0','keys':{}};boxes={}
        for name,host in list(group['hosts'].items()):
            role=host['crange_box_key'].removeprefix('t254-');key='test-'+role+'-t254';scope['keys'][role]=key
            clone=copy.deepcopy(host);clone.update(crange_box_key=key,crange_guest_id=host['crange_guest_id']+200,
                                                ansible_host=host['ansible_host'].replace('.254.','.100.'))
            group['hosts']['p100_ct_'+key]=clone;group['children']['staging_containers']['hosts']['p100_ct_'+key]=clone
            identity=copy.deepcopy(applied['container:'+host['crange_box_key']]);identity['guest_id']=clone['crange_guest_id'];applied['container:'+key]=identity
            boxes[key]={'release_scope':'test','release_id':scope['release_id'],'image':clone['crange_image_id']}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'crange.auto.tfvars.json').write_text(json.dumps({'project_id':100,'scenario_containers':boxes}))
            (root/'crange-applied-placements.json').write_text(json.dumps(applied))
            selected=SERVICES._validate_selected(root,100,canonical,scope)
            self.assertIs(selected['all']['vars']['mhl_test_scope'], True)
            with self.assertRaises(SERVICES.ServiceConfigurationError):
                SERVICES._validate_selected(root,100,canonical)
            changed=copy.deepcopy(canonical)
            first=next(h for h in changed['all']['children']['scenario_containers']['hosts'].values() if h['crange_box_key'] in scope['keys'].values())
            first['crange_image_id']='foreign-image'
            with self.assertRaises(SERVICES.ServiceConfigurationError):
                SERVICES._validate_selected(root,100,changed,scope)
        hosts=[value for group in selected['all']['children'].values() for value in group['hosts'].values()]
        self.assertEqual({h['crange_box_key'] for h in hosts},set(scope['keys'].values()))
        self.assertTrue(all(h['ansible_host'].startswith('10.100.100.') for h in hosts))

    def test_windows_replay_selects_only_exact_test_keys_among_staging_hosts(self):
        import copy
        canonical,files=windows_fixture();group=canonical['all']['children']['windows_guests'];scoped=group['children']['windows_vms']['children']['staging_windows']['hosts']
        scope={'release_id':'p100-1.0.0','keys':{}}
        for name,host in list(group['hosts'].items()):
            role=host['crange_box_key'].removeprefix('t254-');key='test-'+role+'-t254';scope['keys'][role]=key
            clone=copy.deepcopy(host);clone.update(crange_box_key=key,crange_guest_id=host['crange_guest_id']+200,
                ansible_host=host['ansible_host'].replace('.254.','.100.'),crange_guest_network_id='100')
            group['hosts']['p100_vm_'+key]=clone;scoped['p100_vm_'+key]=clone
            box=copy.deepcopy(files['config']['scenario_boxes'][host['crange_box_key']]);box.update(release_scope='test',release_id=scope['release_id'],image=clone['crange_image_id'])
            files['config']['scenario_boxes'][key]=box
            identity=copy.deepcopy(files['applied']['vm:'+host['crange_box_key']]);identity['guest_id']=clone['crange_guest_id'];files['applied']['vm:'+key]=identity
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for filename,content in [('crange.auto.tfvars.json',files['config']),('crange-applied-placements.json',files['applied']),('approved-scenario-templates.json',files['approvals'])]:
                (root/filename).write_text(json.dumps(content))
            _,bindings=SERVICES._windows_scope_hosts(root,100,canonical,scope)
            self.assertTrue(all(row['values']['mhl_test_scope'] is True for row in bindings.values()))
        self.assertEqual({b['box_key'] for b in bindings.values()},set(scope['keys'].values()))
