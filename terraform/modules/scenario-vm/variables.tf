variable "box" {
  type = object({
    project_id    = number
    team_id       = number
    name          = string
    ip_last_octet = number
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
    bios          = string
    machine       = string
    scsi_hardware = string
    os_type       = string
  })
}

variable "ssh_public_key" {
  type = string
}
