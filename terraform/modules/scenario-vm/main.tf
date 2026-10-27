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
  name       = "${local.vnet}-${var.box.name}"
  ip         = "10.${var.box.project_id}.${local.network_id}.${var.box.ip_last_octet}"
  gateway    = "10.${var.box.project_id}.${local.network_id}.1"
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
  machine       = var.box.machine != null ? var.box.machine : var.template_profile.machine
  scsi_hardware = var.template_profile.scsi_hardware
  boot_order    = var.box.boot_order != null ? var.box.boot_order : var.template_profile.boot_order

  clone {
    vm_id        = var.box.template_id
    node_name    = var.box.template_node
    full         = var.box.full_clone
    datastore_id = var.box.full_clone ? var.box.datastore_id : null
  }

  dynamic "disk" {
    for_each = var.box.disk_gb == null ? [] : [var.box.disk_gb]
    content {
      interface    = var.box.disk_interface
      datastore_id = var.box.datastore_id
      size         = disk.value
    }
  }

  cpu {
    cores   = var.box.cores
    sockets = 1
    type    = var.template_profile.cpu_type
  }

  memory {
    dedicated = var.box.memory_mb
  }

  operating_system {
    type = var.template_profile.os_type
  }

  agent {
    enabled = var.template_profile.agent_enabled
    timeout = "5m"
  }

  cdrom {
    interface = "ide2"
    file_id   = "none"
  }

  dynamic "network_device" {
    for_each = var.template_profile.network_models
    content {
      bridge   = local.vnet
      model    = network_device.value
      firewall = true
      mtu      = 1450
    }
  }

  dynamic "initialization" {
    for_each = var.template_profile.cloud_init ? [1] : []
    content {
      upgrade      = var.template_profile.upgrade_packages
      datastore_id = var.box.datastore_id
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

output "placement" {
  value = {
    image_id      = var.box.image_id
    node_name     = proxmox_virtual_environment_vm.scenario.node_name
    datastore_id  = try(proxmox_virtual_environment_vm.scenario.disk[0].datastore_id, var.box.datastore_id)
    template_id   = var.box.template_id
    template_node = var.box.template_node
    clone_mode    = var.box.full_clone ? "full" : "linked"
  }
}
