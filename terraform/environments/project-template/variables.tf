variable "resolved_placements" {
  description = "Controller-owned reviewed placement overrides. Empty preserves legacy placement; changing existing placement requires an explicit reviewed replacement."
  type = map(object({
    node_name            = string
    datastore_id         = string
    template_id          = optional(number)
    template_node        = optional(string)
    template_file_id     = optional(string)
    clone_mode           = optional(string)
    verified_local_cache = optional(bool, false)
    disk_gb              = optional(number)
    disk_interface       = optional(string)
    image_id             = optional(string, "")
    machine              = optional(string)
    boot_order           = optional(list(string))
  }))
  default = {}
  validation {
    condition = alltrue([for key, placement in var.resolved_placements :
      can(regex("^(router:(project|staging|team:[0-9]+|custom:[a-zA-Z0-9_-]+)|jumpbox:[a-zA-Z0-9_-]+|vm:[a-zA-Z0-9_.-]+|container:[a-zA-Z0-9_.-]+)$", key)) &&
      contains(["proxmox1", "proxmox2", "proxmox3", "proxmox4", "proxmox5", "proxmox6"], placement.node_name) &&
      can(regex("^[a-zA-Z0-9][a-zA-Z0-9_-]*$", placement.datastore_id)) &&
      (placement.clone_mode == null ? true : contains(["full", "linked", "archive"], placement.clone_mode)) &&
      (placement.disk_interface == null ? true : startswith(key, "vm:") && can(regex("^(scsi|sata|virtio|ide)[0-9]+$", placement.disk_interface)))
    ])
    error_message = "Resolved placements must use known resource keys, approved nodes, valid datastore IDs and full, linked or archive clone modes."
  }
}
