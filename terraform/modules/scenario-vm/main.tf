terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "= 0.104.0"
    }
  }
}

locals {
  project = format("p%03d", var.box.project_id)
  team    = format("t%03d", var.box.team_id)
  vnet    = "${local.project}${local.team}"
  name    = "${local.vnet}-${var.box.name}"
  ip      = "10.${var.box.project_id}.${var.box.team_id}.${var.box.ip_last_octet}"
  gateway = "10.${var.box.project_id}.${var.box.team_id}.1"
}

resource "proxmox_virtual_environment_vm" "scenario" {
  name            = local.name
  description     = "Scenario VM managed by CRANGE Terraform"
  node_name       = var.box.node_name
  pool_id         = "crange-iac"
  tags            = ["range-lan", "scenario", local.project, local.vnet, "terraform"]
  started         = var.box.started
  on_boot         = false
  stop_on_destroy = true

  bios          = var.template_profile.bios
  machine       = var.template_profile.machine
  scsi_hardware = var.template_profile.scsi_hardware
  boot_order    = ["scsi0"]

  clone {
    vm_id     = var.box.template_id
    node_name = var.box.template_node
    full      = var.box.full_clone
  }

  cpu {
    cores   = var.box.cores
    sockets = 1
    type    = "host"
  }

  memory {
    dedicated = var.box.memory_mb
  }

  operating_system {
    type = var.template_profile.os_type
  }

  agent {
    enabled = false
    timeout = "5m"
  }

  cdrom {
    interface = "ide2"
    file_id   = "none"
  }

  network_device {
    bridge   = local.vnet
    model    = "virtio"
    firewall = true
    mtu      = 1450
  }

  initialization {
    datastore_id = "local-lvm"
    interface    = "ide0"

    dns {
      servers = ["10.60.2.1"]
    }

    ip_config {
      ipv4 {
        address = "${local.ip}/24"
        gateway = local.gateway
      }
    }

    user_account {
      username = "user"
      keys     = [trimspace(var.ssh_public_key)]
    }
  }

  lifecycle {
    precondition {
      condition = (
        var.box.full_clone ||
        var.box.node_name == var.box.template_node
      )
      error_message = "With local-lvm, linked clones must stay on the template node; use a full clone to place the VM elsewhere."
    }
  }
}

output "vm_id" {
  value = proxmox_virtual_environment_vm.scenario.vm_id
}

output "ip_address" {
  value = local.ip
}
