variable "box" {
  type = object({
    project_id    = number
    team_id       = number
    network_id    = optional(number)
    network_vnet  = optional(string)
    name          = string
    ip_last_octet = number
    node_name     = string
    template_id   = number
    template_node = string
    cores         = number
    memory_mb     = number
    started       = bool
  })
}
