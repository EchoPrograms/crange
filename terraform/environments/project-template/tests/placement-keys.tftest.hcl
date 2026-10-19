# Release-test keys retain semantic versions; only VM/CT keys permit dots.
mock_provider "proxmox" {}

run "semantic_version_guest_keys" {
  command = plan
  variables {
    resolved_placements = {
      "vm:test0.1.0-mhl-dc01-t254" = { node_name = "proxmox5", datastore_id = "crange-thin", clone_mode = "linked", template_id = 139, verified_local_cache = true }
      "container:test0.1.0-mhl-app01-t254" = { node_name = "proxmox5", datastore_id = "crange-thin", clone_mode = "linked", template_id = 140, verified_local_cache = true }
    }
  }
}

run "router_keys_remain_strict" {
  command = plan
  variables {
    resolved_placements = {
      "router:custom:bad.network" = { node_name = "proxmox5", datastore_id = "crange-thin", clone_mode = "archive" }
    }
  }
  expect_failures = [var.resolved_placements]
}

run "path_keys_remain_invalid" {
  command = plan
  variables {
    resolved_placements = {
      "vm:../../escape" = { node_name = "proxmox5", datastore_id = "crange-thin", clone_mode = "linked" }
    }
  }
  expect_failures = [var.resolved_placements]
}
