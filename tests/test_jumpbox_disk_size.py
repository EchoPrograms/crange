from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock
import copy
import pytest
import crange
import crange_jumpboxes as module
import crange_scheduling as scheduling
from crange_jobs import command


def setup(monkeypatch,tmp_path):
 cfg={'project_id':100,'jump_boxes':{'linux':{'team_id':'1','image':'parrot','cores':4,'memory_mb':8192,'started':True}},'scenario_boxes':{}}
 monkeypatch.setattr(crange,'project_dir',lambda *a:tmp_path)
 monkeypatch.setattr(crange,'json_config',lambda *a:copy.deepcopy(cfg))
 save=Mock();monkeypatch.setattr(crange,'save_config',save)
 image={'verified':True,'kind':'vm','init_system':'systemd','disk_gb':32,'approved_projects':[100]}
 monkeypatch.setattr(scheduling,'read_settings',lambda:({}, {'images':{'parrot':image}}))
 monkeypatch.setattr(scheduling,'applied_resources',lambda *a:{})
 return cfg,image,save


def test_jumpbox_disk_stages_only_allocation_and_preserves_other_fields(tmp_path,monkeypatch):
 cfg,image,save=setup(monkeypatch,tmp_path)
 module.resize(100,'jumpbox','linux',4,8192,disk=48)
 expected=copy.deepcopy(cfg);expected['jump_boxes']['linux']['disk_gb']=48
 save.assert_called_once_with(tmp_path,expected)
 old=command('jumpbox-resources',100,{'kind':'jumpbox','name':'linux','cores':4,'memory':8192})
 assert '--disk'not in old
 new=command('jumpbox-resources',100,{'kind':'jumpbox','name':'linux','cores':4,'memory':8192,'disk':48})
 assert crange.make_parser().parse_args(new).disk==48


def test_disk_must_fit_approved_linux_image_without_in_place_shrink(tmp_path,monkeypatch):
 cfg,image,save=setup(monkeypatch,tmp_path)
 for size in (31,True,48.5):
  with pytest.raises(crange.CrangeError):module.resize(100,'jumpbox','linux',4,8192,disk=size)
 for changed in ({'verified':False},{'init_system':'windows'},{'approved_projects':[3]}):
  original=copy.deepcopy(image);image.update(changed)
  with pytest.raises(crange.CrangeError):module.resize(100,'jumpbox','linux',4,8192,disk=48)
  image.clear();image.update(original)
 monkeypatch.setattr(scheduling,'applied_resources',lambda *a:{'jumpbox:linux':{'node_name':'proxmox6','vm_id':400}})
 api=Mock();api.vm.return_value={'scsi0':'size=68719476736,file=local-lvm:vm-400-disk-0'}
 monkeypatch.setattr(crange,'ProxmoxAPI',lambda:api)
 with pytest.raises(crange.CrangeError,match='cannot shrink'):module.resize(100,'jumpbox','linux',4,8192,disk=48)
 api.vm.return_value={'scsi0':'local-lvm:vm-400-disk-0,size=32G'}
 module.resize(100,'jumpbox','linux',4,8192,disk=48)
 assert save.call_count==1
