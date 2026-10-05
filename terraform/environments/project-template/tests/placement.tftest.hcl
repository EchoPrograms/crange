mock_provider "proxmox" {}

variables {
  project_id      = 240
  transit_address = "10.60.2.240/24"
  team_ids        = ["1", "2"]
  jump_boxes = {
    test = { team_id = "254", jump_number = 1, node_name = "proxmox4" }
  }
  scenario_containers = {
    stock = { team_id = "254", name = "stock", ip_last_octet = 25, template = "debian-13", node_name = "proxmox4" }
  }
}

run "legacy_placements_preserved" {
  command = plan
  assert {
    condition     = output.routers.project.placement.node_name == "proxmox4" && output.scenario_containers.stock.placement.template_id != null
    error_message = "An empty override map must retain legacy nodes and captured CT source."
  }
}

run "resolved_router_and_stock_container" {
  command = plan
  variables {
    resolved_placements = {
      "jumpbox:test"    = { node_name = "proxmox2", datastore_id = "local-lvm", template_id = 302, template_node = "proxmox2", clone_mode = "full" }
      "router:project"  = { node_name = "proxmox2", datastore_id = "local-lvm", template_file_id = "library:vztmpl/router.tar.zst", clone_mode = "archive" }
      "container:stock" = { node_name = "proxmox3", datastore_id = "local-lvm", template_file_id = "library:vztmpl/debian.tar.zst", clone_mode = "archive", disk_gb = 16 }
    }
  }
  assert {
    condition     = output.routers.project.placement.node_name == "proxmox2" && output.scenario_containers.stock.placement.node_name == "proxmox3" && output.scenario_containers.stock.placement.clone_mode == "archive" && output.jump_boxes.test.placement.template_id == 302 && output.jump_boxes.test.placement.clone_mode == "full"
    error_message = "Reviewed placements must select the resolved node and an approved stock archive rather than cloning a captured CT."
  }
}

run "verified_local_linked_container" {
  command = plan
  variables {
    resolved_placements = {
      "container:stock" = { node_name = "proxmox3", datastore_id = "local-lvm", template_id = 303, template_node = "proxmox3", clone_mode = "linked", verified_local_cache = true }
    }
  }
  assert {
    condition     = output.scenario_containers.stock.placement.clone_mode == "linked" && output.scenario_containers.stock.placement.node_name == "proxmox3"
    error_message = "An explicitly verified local CT cache must use a local linked clone."
  }
}
