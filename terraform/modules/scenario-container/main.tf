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

  clone {
    vm_id        = var.box.template_id
    node_name    = var.box.template_node
    full         = true
    datastore_id = "local-lvm"
  }

  lifecycle {
    precondition {
      condition     = var.box.node_name == var.box.template_node
      error_message = "CT release templates must be deployed on their template node with local-lvm."
    }
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
