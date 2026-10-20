variable "box" {
  type = object({
    project_id    = number
    team_id       = number
    network_id    = optional(number)
    network_vnet  = optional(string)
    name          = string
    ip_last_octet = number
    datastore_id  = optional(string, "local-lvm")
    image_id      = optional(string, "")
    machine       = optional(string)
    boot_order    = optional(list(string))
    node_name     = string
    template_id   = number
    template_node = string
    full_clone    = bool
    cores         = number
    memory_mb     = number
    started       = bool
  })
}

variable "template_profile" {
  type = object({
    bios             = string
    machine          = string
    scsi_hardware    = string
    os_type          = string
    agent_enabled    = optional(bool, false)
    upgrade_packages = optional(bool, true)
    cloud_init       = optional(bool, true)
    cpu_type         = optional(string, "host")
    boot_order       = optional(list(string), ["scsi0"])
    network_models   = optional(list(string), ["virtio"])
  })
}

variable "ssh_public_key" {
  type = string
}
