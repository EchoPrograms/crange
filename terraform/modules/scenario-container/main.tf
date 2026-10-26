terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "= 0.104.0"
    }
  }
}

locals {
  project    = format("p%03d", var.box.project_id)
  network_id = coalesce(var.box.network_id, var.box.team_id)
  vnet       = coalesce(var.box.network_vnet, "${local.project}t${format("%03d", var.box.team_id)}")
  name       = "${local.vnet}-${var.box.name}"
  ip         = "10.${var.box.project_id}.${local.network_id}.${var.box.ip_last_octet}"
  gateway    = "10.${var.box.project_id}.${local.network_id}.1"
}

resource "proxmox_virtual_environment_container" "scenario" {
  description   = "Scenario CT managed by CRANGE Terraform"
  node_name     = var.box.node_name
  pool_id       = "crange-iac"
  unprivileged  = true
  started       = var.box.started
  start_on_boot = false
  tags          = ["range-lan", "scenario", local.project, local.vnet, "terraform"]

  dynamic "clone" {
    for_each = var.box.template_file_id == null ? [1] : []
    content {
      vm_id     = var.box.template_id
      node_name = var.box.template_node
      full      = var.box.full_clone
      # Proxmox rejects a datastore argument for linked container clones;
      # the source template's datastore is used implicitly. Full clones may
      # select the reviewed destination datastore.
      datastore_id = var.box.full_clone ? var.box.datastore_id : null
    }
  }
  dynamic "operating_system" {
    for_each = var.box.template_file_id == null ? [] : [1]
    content {
      template_file_id = var.box.template_file_id
      type             = "debian"
    }
  }
  dynamic "disk" {
    for_each = var.box.template_file_id == null && var.box.disk_gb == null ? [] : [1]
    content {
      datastore_id = var.box.datastore_id
      size         = coalesce(var.box.disk_gb, 8)
    }
  }
  lifecycle {
    precondition {
      condition     = var.box.template_file_id != null || (var.box.template_id != null && var.box.template_node == var.box.node_name && (var.box.full_clone || var.box.verified_local_cache))
      error_message = "Captured CT releases require a verified template on the destination node and a full clone or explicitly verified local linked cache; stock OS archives must be explicitly approved."
    }
  }

  features {
    nesting = true
  }

  cpu {
    cores = var.box.cores
  }
  memory {
    dedicated = var.box.memory_mb
    swap      = 0
  }

  network_interface {
    name     = "eth0"
    bridge   = local.vnet
    firewall = true
    mtu      = 1450
  }

  # Cloned CT templates already have a root filesystem; use cloud-init CT network settings
  # supported by provider 0.104.0.
  initialization {
    dynamic "user_account" {
      for_each = var.box.template_file_id == null ? [] : [1]
      content {
        keys = [trimspace(var.ssh_public_key)]
      }
    }
    hostname = local.name
    dns {
      servers = ["10.60.2.1"]
    }
    ip_config {
      ipv4 {
        address = "${local.ip}/24"
        gateway = local.gateway
      }
    }
  }
}

output "container_id" {
  value = proxmox_virtual_environment_container.scenario.vm_id
}

output "ip_address" {
  value = local.ip
}

output "placement" {
  value = {
    image_id         = var.box.image_id
    node_name        = proxmox_virtual_environment_container.scenario.node_name
    datastore_id     = try(proxmox_virtual_environment_container.scenario.disk[0].datastore_id, var.box.datastore_id)
    template_id      = var.box.template_id
    template_node    = var.box.template_node
    template_file_id = var.box.template_file_id
    clone_mode       = var.box.template_file_id == null ? (var.box.full_clone ? "full" : "linked") : "archive"
  }
}
