mock_provider "proxmox" {}

variables {
  project_id      = 240
  transit_address = "10.60.2.240/24"
  team_ids        = ["1", "2"]
  custom_networks = {
    labone = { owner_team_id = "254", network_id = 100, uplink_last_octet = 99, node_name = "proxmox4" }
    labtwo = { owner_team_id = "254", network_id = 101, uplink_last_octet = 100, node_name = "proxmox4" }
  }
  scenario_boxes = {
    source = { team_id = "254", network_key = "labone", name = "source", ip_last_octet = 20, template = "approved-base", node_name = "proxmox4" }
    clone  = { team_id = "254", network_key = "labtwo", name = "clone", ip_last_octet = 20, template = "approved-base", node_name = "proxmox4" }
  }
  scenario_containers = {
    source = { team_id = "254", network_key = "labone", name = "svc-source", ip_last_octet = 21, template = "debian-13", node_name = "proxmox4" }
    clone  = { team_id = "254", network_key = "labtwo", name = "svc-clone", ip_last_octet = 21, template = "debian-13", node_name = "proxmox4" }
  }
}

run "same_host_octets_on_separate_subnets" {
  command = plan
}

run "vm_container_collision_on_same_subnet" {
  command = plan
  variables {
    scenario_containers = {
      collision = { team_id = "254", network_key = "labone", name = "collision", ip_last_octet = 20, template = "debian-13", node_name = "proxmox4" }
    }
  }
  expect_failures = [var.scenario_containers]
}

run "vm_vm_collision_on_same_subnet" {
  command = plan
  variables {
    scenario_boxes = {
      source = { team_id = "254", network_key = "labone", name = "source", ip_last_octet = 20, template = "approved-base", node_name = "proxmox4" }
      clone  = { team_id = "254", network_key = "labone", name = "clone", ip_last_octet = 20, template = "approved-base", node_name = "proxmox4" }
    }
  }
  expect_failures = [var.scenario_containers]
}
