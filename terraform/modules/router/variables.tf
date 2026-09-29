variable "router" {
  type = object({
    hostname           = string
    node_name          = string
    upstream_vnet      = string
    upstream_address   = string
    upstream_gateway   = string
    downstream_vnet    = string
    downstream_address = string

    tags = optional(list(string), [])

    cores        = optional(number, 1)
    memory_mb    = optional(number, 512)
    disk_gb      = optional(number, 8)
    mtu          = optional(number, 1450)
    datastore_id = optional(string, "local-lvm")
    dns_servers  = optional(list(string), ["10.60.0.1"])

    template_file_id = optional(
      string,
      "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst"
    )
  })

  validation {
    condition = (
      can(cidrnetmask(var.router.upstream_address)) &&
      can(cidrnetmask(var.router.downstream_address))
    )
    error_message = "Both interface addresses must use IPv4 CIDR notation."
  }

  validation {
    condition     = var.router.upstream_vnet != var.router.downstream_vnet
    error_message = "Upstream and downstream must use different VNets."
  }
}

variable "ssh_public_key" {
  type = string
}
