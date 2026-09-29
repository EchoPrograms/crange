terraform {
  required_version = ">= 1.8.0, < 2.0.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "= 0.104.0"
    }
  }
}

provider "proxmox" {}

resource "proxmox_sdn_vnet" "transit" {
  id            = "crtrans"
  zone          = "crange"
  alias         = "Cyber range project transit"
  tag           = 10212
  vlan_aware    = false
  isolate_ports = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "proxmox_sdn_applier" "apply" {
  on_create  = true
  on_destroy = false

  lifecycle {
    replace_triggered_by = [
      proxmox_sdn_vnet.transit
    ]
  }

  depends_on = [
    proxmox_sdn_vnet.transit
  ]
}

output "transit_vnet" {
  value = proxmox_sdn_vnet.transit.id
}
