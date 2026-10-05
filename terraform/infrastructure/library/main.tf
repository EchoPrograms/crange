terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "= 0.104.0"
    }
  }
}

provider "proxmox" {}

variable "ssh_public_key" {
  type        = string
  description = "Controller SSH public key authorized for library administration."
  validation {
    condition     = can(regex("^ssh-(ed25519|rsa) ", trimspace(var.ssh_public_key)))
    error_message = "Supply an SSH public key."
  }
}

variable "library_volume" {
  type        = string
  default     = "ssd-lvm"
  description = "New allocation datastore, or the existing imported library volume ID."
}

variable "template_file_id" {
  type        = string
  description = "Debian 12 archive compatible with proxmox5's installed container tools."
}

resource "proxmox_virtual_environment_container" "library" {
  vm_id         = 145
  node_name     = "proxmox5"
  pool_id       = "crange-iac"
  description   = "Dedicated Debian NFSv4 image library managed by Terraform and Ansible"
  unprivileged  = false
  started       = true
  start_on_boot = true
  tags          = ["crange-infrastructure", "crange-library", "range-lan"]

  console {
    enabled   = true
    tty_count = 2
    type      = "tty"
  }

  operating_system {
    template_file_id = var.template_file_id
    type             = "debian"
  }
  cpu {
    cores = 1
  }
  memory {
    dedicated = 768
    swap      = 0
  }
  disk {
    datastore_id = "ssd-lvm"
    size         = 8
  }
  mount_point {
    volume = var.library_volume
    size   = "400G"
    path   = "/srv/crange-library"
    backup = true
  }
  startup {
    order      = 1
    up_delay   = 20
    down_delay = 30
  }
  network_interface {
    name     = "eth0"
    bridge   = "vmbr0"
    firewall = true
  }
  network_interface {
    name     = "eth1"
    bridge   = "craccess"
    mtu      = 1450
    firewall = true
  }
  initialization {
    hostname = "crange-library"
    dns {
      servers = ["1.1.1.1"]
    }
    ip_config {
      ipv4 {
        address = "10.0.0.249/24"
        gateway = "10.0.0.1"
      }
    }
    ip_config {
      ipv4 {
        address = "10.60.0.30/24"
      }
    }
    user_account {
      keys = [trimspace(var.ssh_public_key)]
    }
  }
  lifecycle {
    prevent_destroy = true
    # The API cannot recover these creation-only fields during import.
    # Pool membership and installed SSH keys are verified independently.
    ignore_changes = [pool_id, operating_system[0].template_file_id, initialization[0].user_account]
  }
}

output "container_id" {
  value = proxmox_virtual_environment_container.library.vm_id
}

output "nfs_server" {
  value = "10.0.0.249"
}

output "management_address" {
  value = "10.60.0.30"
}
