terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "= 0.104.0"
    }
  }
}

resource "proxmox_virtual_environment_container" "router" {
  description   = "Cyber range router managed by Terraform and Ansible"
  node_name     = var.router.node_name
  pool_id       = "crange-iac"
  unprivileged  = true
  started       = true
  start_on_boot = true

  tags = sort(distinct(concat(
    ["range-lan", "network-infrastructure", "terraform"],
    var.router.tags
  )))

  operating_system {
    template_file_id = var.router.template_file_id
    type             = "debian"
  }

  cpu {
    cores = var.router.cores
  }

  memory {
    dedicated = var.router.memory_mb
    swap      = 0
  }

  disk {
    datastore_id = var.router.datastore_id
    size         = var.router.disk_gb
  }

  network_interface {
    name     = "eth0"
    bridge   = var.router.upstream_vnet
    firewall = true
    mtu      = var.router.mtu
  }

  network_interface {
    name     = "eth1"
    bridge   = var.router.downstream_vnet
    firewall = true
    mtu      = var.router.mtu
  }

  initialization {
    hostname = var.router.hostname

    dns {
      servers = var.router.dns_servers
    }

    ip_config {
      ipv4 {
        address = var.router.upstream_address
        gateway = var.router.upstream_gateway
      }
    }

    ip_config {
      ipv4 {
        address = var.router.downstream_address
      }
    }

    user_account {
      keys = [trimspace(var.ssh_public_key)]
    }
  }
}

output "container_id" {
  value = proxmox_virtual_environment_container.router.vm_id
}

output "management_address" {
  value = split("/", var.router.upstream_address)[0]
}

output "downstream_gateway" {
  value = split("/", var.router.downstream_address)[0]
}
