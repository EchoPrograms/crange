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
  team       = format("t%03d", var.box.team_id)
  network_id = coalesce(var.box.network_id, var.box.team_id)
  vnet       = coalesce(var.box.network_vnet, "${local.project}${local.team}")
  name       = format("%s-jump-%02d", local.vnet, var.box.jump_number)

  ip      = "10.${var.box.project_id}.${local.network_id}.${10 + var.box.jump_number}"
  gateway = "10.${var.box.project_id}.${local.network_id}.1"
}

resource "proxmox_virtual_environment_vm" "jump" {
  name        = local.name
  description = "Managed by Terraform: cyber range controller bootstrap"
  node_name   = var.box.node_name
  pool_id     = "crange-iac"

  tags    = ["range-lan", "jumpbox", local.project, local.vnet, "terraform"]
  started = var.box.started
  on_boot = false

  bios          = var.template_profile.bios
  machine       = var.template_profile.machine
  scsi_hardware = var.template_profile.scsi_hardware
  boot_order    = ["scsi0"]

  clone {
    vm_id        = var.box.template_id
    node_name    = var.box.template_node
    full         = var.box.full_clone
    datastore_id = var.box.full_clone ? var.box.datastore_id : null
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
    enabled = true
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
    datastore_id = var.box.datastore_id
    interface    = "ide0"

    dns {
      servers = var.box.dns_servers
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
      error_message = "With local-lvm, use a template on the destination node for linked clones, or select a full clone."
    }
  }
}

output "vm_id" {
  value = proxmox_virtual_environment_vm.jump.vm_id
}

output "ip_address" {
  value = local.ip
}

output "inventory_name" {
  value = format("%s_jump%02d", local.vnet, var.box.jump_number)
}

output "reported_ipv4_addresses" {
  value = proxmox_virtual_environment_vm.jump.ipv4_addresses
}

output "placement" {
  value = {
    image_id      = var.box.image_id
    node_name     = proxmox_virtual_environment_vm.jump.node_name
    datastore_id  = try(proxmox_virtual_environment_vm.jump.disk[0].datastore_id, var.box.datastore_id)
    template_id   = var.box.template_id
    template_node = var.box.template_node
    clone_mode    = var.box.full_clone ? "full" : "linked"
  }
}
