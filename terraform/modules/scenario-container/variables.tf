variable "box" {
  type = object({
    project_id           = number
    team_id              = number
    network_id           = optional(number)
    network_vnet         = optional(string)
    name                 = string
    ip_last_octet        = number
    datastore_id         = optional(string, "local-lvm")
    image_id             = optional(string, "")
    node_name            = string
    template_id          = optional(number)
    template_node        = optional(string)
    template_file_id     = optional(string)
    disk_gb              = optional(number, 8)
    full_clone           = optional(bool, true)
    verified_local_cache = optional(bool, false)
    cores                = number
    memory_mb            = number
    started              = bool
  })
}
