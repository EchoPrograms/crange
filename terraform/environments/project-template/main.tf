terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "= 0.104.0"
    }
  }
}

provider "proxmox" {}

variable "project_id" {
  type = number

  validation {
    condition     = var.project_id == floor(var.project_id) && var.project_id >= 1 && var.project_id <= 255 && !contains([60, 61], var.project_id)
    error_message = "project_id must be an integer from 1 to 255, excluding 60 and 61."
  }
}

variable "transit_address" {
  type        = string
  description = "Unique project-router address on the shared 10.60.2.0/24 transit VNet, in CIDR form."

  validation {
    condition = (
      can(cidrhost(var.transit_address, 0)) &&
      can(regex("^10[.]60[.]2[.]([2-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-4])/24$", var.transit_address))
    )
    error_message = "Use an unused host address from 10.60.2.2 through 10.60.2.254 with /24 prefix."
  }
}

variable "team_ids" {
  type = set(string)

  validation {
    condition = length(var.team_ids) > 0 && length(var.team_ids) <= 252 && alltrue([
      for id in var.team_ids :
      can(tonumber(id)) &&
      tonumber(id) == floor(tonumber(id)) &&
      tonumber(id) >= 1 &&
      tonumber(id) <= 253
    ])
    error_message = "team_ids must contain one or more integer team IDs from 1 to 253, with at most 252 teams so staging has a transit address."
  }
}

variable "router_node" {
  type    = string
  default = "proxmox4"
}

variable "jump_boxes" {
  type = map(object({
    team_id     = string
    network_key = optional(string, "")
    jump_number = number
    node_name   = string
    cores       = optional(number, 2)
    memory_mb   = optional(number, 4096)
    disk_gb     = optional(number)
    full_clone  = optional(bool, false)
    started     = optional(bool, true)
  }))
  default = {}

  validation {
    condition = alltrue([
      for box in values(var.jump_boxes) : box.disk_gb == null ? true : (
        box.disk_gb == floor(box.disk_gb) && box.disk_gb >= 8 && box.disk_gb <= 1024
      )
    ])
    error_message = "Optional jump-box disks must be integer GiB sizes from 8 to 1024."
  }

  validation {
    condition = alltrue([
      for box in values(var.jump_boxes) :
      (contains(var.team_ids, box.team_id) || box.team_id == "254") &&
      box.jump_number == floor(box.jump_number) &&
      box.jump_number >= 1 &&
      box.jump_number <= 90
    ])
    error_message = "Each jump box must reference a listed team and use jump number 1–90."
  }
}

variable "custom_networks" {
  description = "Additional routed /24 networks owned by a project team or staging."
  type = map(object({
    owner_team_id     = string
    network_id        = number
    uplink_last_octet = number
    node_name         = string
    cores             = optional(number, 1)
    memory_mb         = optional(number, 512)
  }))
  default = {}

  validation {
    condition = (
      alltrue([
        for key, net in var.custom_networks :
        can(regex("^[a-z][a-z0-9-]{0,23}$", key)) &&
        (contains(var.team_ids, net.owner_team_id) || net.owner_team_id == "254") &&
        net.network_id == floor(net.network_id) && net.network_id >= 1 && net.network_id <= 253 &&
        !contains([for team in var.team_ids : tonumber(team)], net.network_id) &&
        net.uplink_last_octet == floor(net.uplink_last_octet) && net.uplink_last_octet >= 2 && net.uplink_last_octet <= 100 &&
        contains(["auto", "proxmox1", "proxmox2", "proxmox3", "proxmox4", "proxmox5", "proxmox6"], net.node_name)
      ]) &&
      length(distinct([for net in values(var.custom_networks) : net.network_id])) == length(var.custom_networks) &&
      length(distinct([for net in values(var.custom_networks) : "${net.owner_team_id}.${net.uplink_last_octet}"])) == length(var.custom_networks)
    )
    error_message = "Custom networks need unique unused project network IDs, a declared parent team or staging, unique static uplinks .2-.100, and an approved node."
  }
}

variable "scenario_boxes" {
  description = "Project scenario VMs; Team 0 is network ID 254 and static addresses are .2-.100."
  type = map(object({
    team_id       = string
    network_key   = optional(string, "")
    name          = string
    ip_last_octet = number
    template      = string
    node_name     = string
    cores         = optional(number, 2)
    memory_mb     = optional(number, 4096)
    disk_gb       = optional(number)
    full_clone    = optional(bool, false)
    started       = optional(bool, true)
    guac_protocol = optional(string, "ssh")
    health_checks = optional(list(object({
      type    = string
      port    = optional(number, 0)
      path    = optional(string, "/")
      status  = optional(number, 200)
      timeout = optional(number, 10)
    })), [])
  }))
  default = {}

  validation {
    condition = alltrue([
      for box in values(var.scenario_boxes) :
      (contains(var.team_ids, box.team_id) || box.team_id == "254") &&
      box.ip_last_octet == floor(box.ip_last_octet) &&
      box.ip_last_octet >= 2 && box.ip_last_octet <= 100 &&
      can(regex("^[a-z][a-z0-9-]{0,23}$", box.name)) &&
      box.cores >= 1 && box.cores <= 16 &&
      box.memory_mb >= 512 && box.memory_mb <= 65536 && contains(["ssh", "rdp"], box.guac_protocol) &&
      (box.disk_gb == null ? true : (box.disk_gb == floor(box.disk_gb) && box.disk_gb >= 8 && box.disk_gb <= 1024))
    ])
    error_message = "Scenario boxes must target a project team or Team 0 (254), use .2-.100, safe names, 1-16 cores, and 512-65536 MB RAM."
  }

  validation {
    condition = alltrue([
      for box in values(var.scenario_boxes) : alltrue([
        for check in box.health_checks :
        contains(["ping", "tcp", "http"], check.type) &&
        (check.type == "ping" || (check.port >= 1 && check.port <= 65535)) &&
        check.timeout >= 1 && check.timeout <= 60 &&
        (check.type == "tcp" || (startswith(check.path, "/") && check.status >= 100 && check.status <= 599))
      ])
    ])
    error_message = "Health checks must use ping, TCP, or HTTP with valid timeout and service values."
  }

}

variable "scenario_containers" {
  description = "Project scenario LXC containers cloned from platform-approved CT templates."
  type = map(object({
    team_id       = string
    network_key   = optional(string, "")
    name          = string
    ip_last_octet = number
    template      = string
    node_name     = string
    cores         = optional(number, 1)
    memory_mb     = optional(number, 1024)
    disk_gb       = optional(number)
    started       = optional(bool, true)
    guac_protocol = optional(string, "ssh")
    health_checks = optional(list(object({
      type    = string
      port    = optional(number, 0)
      path    = optional(string, "/")
      status  = optional(number, 200)
      timeout = optional(number, 5)
    })), [])
  }))
  default = {}
  validation {
    condition = alltrue([
      for box in values(var.scenario_containers) :
      (contains(var.team_ids, box.team_id) || box.team_id == "254") &&
      box.ip_last_octet == floor(box.ip_last_octet) && box.ip_last_octet >= 2 && box.ip_last_octet <= 100 &&
      can(regex("^[a-z][a-z0-9-]{0,23}$", box.name)) &&
      box.cores >= 1 && box.cores <= 16 && box.memory_mb >= 512 && box.memory_mb <= 65536 &&
      contains(["ssh", "rdp"], box.guac_protocol) &&
      contains(["auto", "proxmox1", "proxmox2", "proxmox3", "proxmox4", "proxmox5", "proxmox6"], box.node_name) &&
      (box.network_key == "" || contains(keys(var.custom_networks), box.network_key))
    ])
    error_message = "Scenario CTs need a declared team or staging network, static IP .2-.100, safe name, approved node, 1-16 cores and 512-65536 MB."
  }

  validation {
    condition = alltrue([for box in values(var.scenario_containers) : alltrue([
      for check in box.health_checks : contains(["ping", "tcp", "http"], check.type) &&
      (check.type == "ping" || (check.port >= 1 && check.port <= 65535))
    ])])
    error_message = "Container health checks must use ping, TCP, or HTTP with valid ports."
  }


  validation {
    condition = length(distinct(concat(
      [for box in values(var.scenario_boxes) : "${box.network_key == "" ? box.team_id : tostring(var.custom_networks[box.network_key].network_id)}.${box.ip_last_octet}"],
      [for box in values(var.scenario_containers) : "${box.network_key == "" ? box.team_id : tostring(var.custom_networks[box.network_key].network_id)}.${box.ip_last_octet}"],
      [for box in values(var.jump_boxes) : "${box.network_key == "" ? box.team_id : tostring(var.custom_networks[box.network_key].network_id)}.${10 + box.jump_number}"],
      [for net in values(var.custom_networks) : "${net.owner_team_id}.${net.uplink_last_octet}"]
    ))) == length(var.scenario_boxes) + length(var.scenario_containers) + length(var.jump_boxes) + length(var.custom_networks)
    error_message = "Scenario VMs, CTs, jump boxes, and custom-router static addresses must be unique on their parent networks."
  }
}

locals {
  project      = format("p%03d", var.project_id)
  transit_vnet = "${local.project}tr"
  zone         = "crange"
  teams        = { for id in var.team_ids : id => tonumber(id) }
  staging_vnet = format("%st%03d", local.project, 254)
  staging_transit_host = min([
    for host in range(2, 255) : host
    if !contains([for team in values(local.teams) : team + 1], host)
  ]...)
  ssh_public_key               = trimspace(file(pathexpand("~/.ssh/id_ed25519_range.pub")))
  template_profile             = jsondecode(file("${path.module}/template-profile.json"))
  approved_scenario_templates  = jsondecode(file("${path.module}/approved-scenario-templates.json"))
  approved_container_templates = jsondecode(file("${path.module}/approved-container-templates.json"))

}

resource "proxmox_sdn_applier" "finalizer" {
  on_create  = false
  on_destroy = true
}

resource "proxmox_sdn_vnet" "transit" {
  id            = local.transit_vnet
  zone          = local.zone
  alias         = "${local.project} transit"
  tag           = 100000 + var.project_id * 256
  vlan_aware    = false
  isolate_ports = false

  depends_on = [proxmox_sdn_applier.finalizer]
}

resource "proxmox_sdn_vnet" "team" {
  for_each = local.teams

  id            = format("%st%03d", local.project, each.value)
  zone          = local.zone
  alias         = format("%s Team %d", local.project, each.value)
  tag           = 100000 + var.project_id * 256 + each.value
  vlan_aware    = false
  isolate_ports = false

  depends_on = [proxmox_sdn_applier.finalizer]
}

resource "proxmox_sdn_vnet" "staging" {
  id            = local.staging_vnet
  zone          = local.zone
  alias         = "${local.project} Team 0 staging"
  tag           = 100000 + var.project_id * 256 + 254
  vlan_aware    = false
  isolate_ports = false

  depends_on = [proxmox_sdn_applier.finalizer]
}

resource "proxmox_sdn_vnet" "custom" {
  for_each = var.custom_networks

  id            = format("%sc%03d", local.project, each.value.network_id)
  zone          = local.zone
  alias         = format("%s custom %s", local.project, each.key)
  tag           = 100000 + var.project_id * 256 + each.value.network_id
  vlan_aware    = false
  isolate_ports = false

  depends_on = [proxmox_sdn_applier.finalizer]
}

# A single revision resource remains present when a for_each VNet set is empty.
# Terraform cannot use an empty resource collection as a replacement trigger.
resource "terraform_data" "sdn_revision" {
  input = {
    transit = proxmox_sdn_vnet.transit
    teams   = proxmox_sdn_vnet.team
    staging = proxmox_sdn_vnet.staging
    custom  = proxmox_sdn_vnet.custom
  }
}

resource "proxmox_sdn_applier" "apply" {
  on_create  = true
  on_destroy = false

  lifecycle {
    replace_triggered_by = [terraform_data.sdn_revision]
  }

  depends_on = [
    proxmox_sdn_vnet.transit,
    proxmox_sdn_vnet.team,
    proxmox_sdn_vnet.staging,
    proxmox_sdn_vnet.custom
  ]
}

# Guest creation requires the SDN bridges, not another guest to be refreshed
# or running. Router/service configuration order is enforced by Ansible.
# Keep scoped staging/release targets independent of unrelated team routers.
module "project_router" {
  source = "../../modules/router"

  ssh_public_key = local.ssh_public_key

  router = merge({
    hostname           = "${local.project}-router"
    node_name          = var.router_node
    upstream_vnet      = "crtrans"
    upstream_address   = var.transit_address
    upstream_gateway   = "10.60.2.1"
    downstream_vnet    = proxmox_sdn_vnet.transit.id
    downstream_address = "10.${var.project_id}.0.1/24"
    dns_servers        = ["10.60.2.1"]
    template_file_id   = "crange-library:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
    tags               = [local.project]
    }, { for field, value in try(var.resolved_placements["router:project"], {}) : field => value if value != null && field != "clone_mode" }, try(var.resolved_placements["router:project"].clone_mode, null) == null ? {} : {
    full_clone = var.resolved_placements["router:project"].clone_mode == "full"
  })

  depends_on = [proxmox_sdn_applier.apply]
}

module "team_router" {
  for_each = local.teams
  source   = "../../modules/router"

  ssh_public_key = local.ssh_public_key

  router = merge({
    hostname           = format("%st%03d-router", local.project, each.value)
    node_name          = var.router_node
    upstream_vnet      = proxmox_sdn_vnet.transit.id
    upstream_address   = "10.${var.project_id}.0.${each.value + 1}/24"
    upstream_gateway   = "10.${var.project_id}.0.1"
    downstream_vnet    = proxmox_sdn_vnet.team[each.key].id
    downstream_address = "10.${var.project_id}.${each.value}.1/24"
    dns_servers        = ["10.60.2.1"]
    template_file_id   = "crange-library:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
    tags               = [local.project, format("%st%03d", local.project, each.value)]
    }, { for field, value in try(var.resolved_placements["router:team:${each.key}"], {}) : field => value if value != null && field != "clone_mode" }, try(var.resolved_placements["router:team:${each.key}"].clone_mode, null) == null ? {} : {
    full_clone = var.resolved_placements["router:team:${each.key}"].clone_mode == "full"
  })

  depends_on = [proxmox_sdn_applier.apply]
}

module "staging_router" {
  source = "../../modules/router"

  ssh_public_key = local.ssh_public_key

  router = merge({
    hostname           = "${local.project}t254-router"
    node_name          = var.router_node
    upstream_vnet      = proxmox_sdn_vnet.transit.id
    upstream_address   = "10.${var.project_id}.0.${local.staging_transit_host}/24"
    upstream_gateway   = "10.${var.project_id}.0.1"
    downstream_vnet    = proxmox_sdn_vnet.staging.id
    downstream_address = "10.${var.project_id}.254.1/24"
    dns_servers        = ["10.60.2.1"]
    template_file_id   = "crange-library:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
    tags               = [local.project, local.staging_vnet]
    }, { for field, value in try(var.resolved_placements["router:staging"], {}) : field => value if value != null && field != "clone_mode" }, try(var.resolved_placements["router:staging"].clone_mode, null) == null ? {} : {
    full_clone = var.resolved_placements["router:staging"].clone_mode == "full"
  })

  depends_on = [proxmox_sdn_applier.apply]
}

module "custom_router" {
  for_each = var.custom_networks
  source   = "../../modules/router"

  ssh_public_key = local.ssh_public_key

  router = merge({
    hostname           = format("%s-custom-%s-router", local.project, each.key)
    node_name          = each.value.node_name
    upstream_vnet      = each.value.owner_team_id == "254" ? proxmox_sdn_vnet.staging.id : proxmox_sdn_vnet.team[each.value.owner_team_id].id
    upstream_address   = "10.${var.project_id}.${each.value.owner_team_id}.${each.value.uplink_last_octet}/24"
    upstream_gateway   = "10.${var.project_id}.${each.value.owner_team_id}.1"
    downstream_vnet    = proxmox_sdn_vnet.custom[each.key].id
    downstream_address = "10.${var.project_id}.${each.value.network_id}.1/24"
    cores              = each.value.cores
    memory_mb          = each.value.memory_mb
    dns_servers        = ["10.60.2.1"]
    template_file_id   = "crange-library:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
    tags               = [local.project, proxmox_sdn_vnet.custom[each.key].id]
    }, { for field, value in try(var.resolved_placements["router:custom:${each.key}"], {}) : field => value if value != null && field != "clone_mode" }, try(var.resolved_placements["router:custom:${each.key}"].clone_mode, null) == null ? {} : {
    full_clone = var.resolved_placements["router:custom:${each.key}"].clone_mode == "full"
  })

  depends_on = [proxmox_sdn_applier.apply]
}

module "jumpbox" {
  for_each = var.jump_boxes
  source   = "../../modules/jumpbox"

  ssh_public_key = local.ssh_public_key

  box = merge({
    project_id    = var.project_id
    team_id       = tonumber(each.value.team_id)
    jump_number   = each.value.jump_number
    network_id    = each.value.network_key == "" ? tonumber(each.value.team_id) : var.custom_networks[each.value.network_key].network_id
    network_vnet  = each.value.network_key == "" ? (each.value.team_id == "254" ? proxmox_sdn_vnet.staging.id : proxmox_sdn_vnet.team[each.value.team_id].id) : proxmox_sdn_vnet.custom[each.value.network_key].id
    node_name     = each.value.node_name
    template_id   = 112
    template_node = "proxmox4"
    full_clone    = each.value.full_clone
    cores         = each.value.cores
    memory_mb     = each.value.memory_mb
    disk_gb       = each.value.disk_gb
    started       = each.value.started
    dns_servers   = ["10.60.2.1"]
    }, { for field, value in try(var.resolved_placements["jumpbox:${each.key}"], {}) : field => value if value != null && field != "clone_mode" }, try(var.resolved_placements["jumpbox:${each.key}"].clone_mode, null) == null ? {} : {
    full_clone = var.resolved_placements["jumpbox:${each.key}"].clone_mode == "full"
  })

  template_profile = local.template_profile

  depends_on = [proxmox_sdn_applier.apply]
}

module "scenario_box" {
  for_each = var.scenario_boxes
  source   = "../../modules/scenario-vm"

  ssh_public_key   = local.ssh_public_key
  template_profile = merge(local.template_profile, try(local.approved_scenario_templates[each.value.template].profile, {}))

  box = merge({
    project_id    = var.project_id
    team_id       = tonumber(each.value.team_id)
    network_id    = each.value.network_key == "" ? tonumber(each.value.team_id) : var.custom_networks[each.value.network_key].network_id
    network_vnet  = each.value.network_key == "" ? (each.value.team_id == "254" ? proxmox_sdn_vnet.staging.id : proxmox_sdn_vnet.team[each.value.team_id].id) : proxmox_sdn_vnet.custom[each.value.network_key].id
    name          = each.value.name
    ip_last_octet = each.value.ip_last_octet
    node_name     = each.value.node_name
    template_id   = local.approved_scenario_templates[each.value.template].template_id
    template_node = local.approved_scenario_templates[each.value.template].template_node
    full_clone    = each.value.full_clone
    cores         = each.value.cores
    memory_mb     = each.value.memory_mb
    disk_gb       = each.value.disk_gb
    started       = each.value.started
    }, { for field, value in try(var.resolved_placements["vm:${each.key}"], {}) : field => value if value != null && field != "clone_mode" }, try(var.resolved_placements["vm:${each.key}"].clone_mode, null) == null ? {} : {
    full_clone = var.resolved_placements["vm:${each.key}"].clone_mode == "full"
  })

  depends_on = [proxmox_sdn_applier.apply]
}

module "scenario_container" {
  ssh_public_key = local.ssh_public_key
  for_each       = var.scenario_containers
  source         = "../../modules/scenario-container"

  box = merge({
    project_id       = var.project_id
    team_id          = tonumber(each.value.team_id)
    network_id       = each.value.network_key == "" ? tonumber(each.value.team_id) : var.custom_networks[each.value.network_key].network_id
    network_vnet     = each.value.network_key == "" ? (each.value.team_id == "254" ? proxmox_sdn_vnet.staging.id : proxmox_sdn_vnet.team[each.value.team_id].id) : proxmox_sdn_vnet.custom[each.value.network_key].id
    name             = each.value.name
    ip_last_octet    = each.value.ip_last_octet
    node_name        = each.value.node_name
    template_id      = try(local.approved_container_templates[each.value.template].template_id, null)
    template_node    = try(local.approved_container_templates[each.value.template].template_node, null)
    template_file_id = try(local.approved_container_templates[each.value.template].template_file_id, null)
    cores            = each.value.cores
    memory_mb        = each.value.memory_mb
    disk_gb          = each.value.disk_gb
    started          = each.value.started
    }, { for field, value in try(var.resolved_placements["container:${each.key}"], {}) : field => value if value != null && field != "clone_mode" }, try(var.resolved_placements["container:${each.key}"].clone_mode, null) == null ? {} : {
    full_clone           = var.resolved_placements["container:${each.key}"].clone_mode == "full"
    verified_local_cache = try(var.resolved_placements["container:${each.key}"].verified_local_cache, false)
  })
  depends_on = [proxmox_sdn_applier.apply]
}

output "vnets" {
  value = {
    transit = proxmox_sdn_vnet.transit.id
    teams   = { for id, vnet in proxmox_sdn_vnet.team : id => vnet.id }
    staging = proxmox_sdn_vnet.staging.id
  }
}

output "routers" {
  value = {
    project = {
      placement = module.project_router.placement
      id        = module.project_router.container_id
      address   = module.project_router.management_address
    }
    teams = {
      for id, router in module.team_router :
      id => {
        placement = router.placement
        id        = router.container_id
        address   = router.management_address
        lan       = "10.${var.project_id}.${id}.0/24"
      }
    }
    staging = {
      placement = module.staging_router.placement
      id        = module.staging_router.container_id
      address   = module.staging_router.management_address
      lan       = "10.${var.project_id}.254.0/24"
    }
    custom_networks = {
      for key, net in var.custom_networks :
      key => {
        placement        = module.custom_router[key].placement
        id               = module.custom_router[key].container_id
        address          = module.custom_router[key].management_address
        lan              = "10.${var.project_id}.${net.network_id}.0/24"
        owner_team_id    = net.owner_team_id
        upstream_address = "10.${var.project_id}.${net.owner_team_id}.${net.uplink_last_octet}"
        network_id       = net.network_id
        vnet             = proxmox_sdn_vnet.custom[key].id
      }
    }
  }
}

output "jump_boxes" {
  value = {
    for name, box in module.jumpbox :
    name => {
      placement    = box.placement
      id           = box.vm_id
      address      = box.ip_address
      team_id      = var.jump_boxes[name].team_id
      network_id   = var.jump_boxes[name].network_key == "" ? tonumber(var.jump_boxes[name].team_id) : var.custom_networks[var.jump_boxes[name].network_key].network_id
      network_vnet = var.jump_boxes[name].network_key == "" ? (var.jump_boxes[name].team_id == "254" ? proxmox_sdn_vnet.staging.id : proxmox_sdn_vnet.team[var.jump_boxes[name].team_id].id) : proxmox_sdn_vnet.custom[var.jump_boxes[name].network_key].id
      jump_number  = var.jump_boxes[name].jump_number
    }
  }
}

output "scenario_containers" {
  value = {
    for name, container in module.scenario_container :
    name => {
      placement     = container.placement
      id            = container.container_id
      address       = container.ip_address
      team_id       = var.scenario_containers[name].team_id
      network_id    = var.scenario_containers[name].network_key == "" ? tonumber(var.scenario_containers[name].team_id) : var.custom_networks[var.scenario_containers[name].network_key].network_id
      network_vnet  = var.scenario_containers[name].network_key == "" ? (var.scenario_containers[name].team_id == "254" ? proxmox_sdn_vnet.staging.id : proxmox_sdn_vnet.team[var.scenario_containers[name].team_id].id) : proxmox_sdn_vnet.custom[var.scenario_containers[name].network_key].id
      guac_protocol = var.scenario_containers[name].guac_protocol
    }
  }
}

# OPNsense static route/gateway desired state. This API integration manages routing only.
variable "opnsense_transit_interface" {
  type        = string
  description = "OPNsense interface ID connected to RangeTransit (currently opt2)."
  default     = "opt2"
}

output "project_id" {
  value = var.project_id
}

output "opnsense_route" {
  value = {
    project_id      = var.project_id
    gateway_name    = upper(local.project)
    gateway_address = split("/", var.transit_address)[0]
    interface       = var.opnsense_transit_interface
    network         = format("10.%d.0.0/16", var.project_id)
  }
}

output "scenario_boxes" {
  value = {
    for name, box in module.scenario_box :
    name => {
      placement     = box.placement
      id            = box.vm_id
      address       = box.ip_address
      team_id       = var.scenario_boxes[name].team_id
      network_id    = var.scenario_boxes[name].network_key == "" ? tonumber(var.scenario_boxes[name].team_id) : var.custom_networks[var.scenario_boxes[name].network_key].network_id
      guac_protocol = var.scenario_boxes[name].guac_protocol
    }
  }
}

variable "release_rollouts" {
  description = "Controller-owned release declaration, apply and health history."
  type        = any
  default     = {}
}
