variable "box" {
  type = object({
    project_id    = number
    team_id       = number
    network_id    = optional(number)
    network_vnet  = optional(string)
    jump_number   = number
    datastore_id  = optional(string, "local-lvm")
    image_id      = optional(string, "")
    machine       = optional(string)
    node_name     = string
    template_id   = optional(number, 112)
    template_node = optional(string, "proxmox4")
    full_clone    = optional(bool, false)
    cores         = optional(number, 2)
    memory_mb     = optional(number, 4096)
    started       = optional(bool, true)
    dns_servers   = optional(list(string), ["10.60.0.1"])
  })

  validation {
    condition = (
      var.box.project_id == floor(var.box.project_id) &&
      var.box.project_id >= 1 && var.box.project_id <= 255 &&
      !contains([60, 61], var.box.project_id) &&
      var.box.team_id == floor(var.box.team_id) &&
      var.box.team_id >= 1 && var.box.team_id <= 254 &&
      var.box.jump_number == floor(var.box.jump_number) &&
      var.box.jump_number >= 1 && var.box.jump_number <= 90
    )
    error_message = "Use project 1–255 excluding 60/61, a declared team or staging network, and jump number 1–90; all must be integers."
  }
}

variable "template_profile" {
  type = object({
    bios          = string
    machine       = string
    scsi_hardware = string
    os_type       = string
  })
}

variable "ssh_public_key" {
  type = string
}
