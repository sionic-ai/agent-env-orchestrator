variable "namespace" {
  description = "Name of the namespace to create for agent environments."
  type        = string
  default     = "aeo-envs"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$", var.namespace))
    error_message = "namespace must be a valid DNS-1123 label."
  }
}

variable "labels" {
  description = "Extra labels added to every object created by this module."
  type        = map(string)
  default     = {}
}

variable "quota" {
  description = "Namespace-wide ResourceQuota hard limits."
  type = object({
    pods            = number
    requests_cpu    = string
    requests_memory = string
    limits_cpu      = string
    limits_memory   = string
  })
  default = {
    pods            = 20
    requests_cpu    = "8"
    requests_memory = "16Gi"
    limits_cpu      = "8"
    limits_memory   = "16Gi"
  }
}

variable "container_defaults" {
  description = "LimitRange defaults and per-container maximums."
  type = object({
    default_cpu            = string
    default_memory         = string
    default_request_cpu    = string
    default_request_memory = string
    max_cpu                = string
    max_memory             = string
  })
  default = {
    default_cpu            = "1"
    default_memory         = "512Mi"
    default_request_cpu    = "250m"
    default_request_memory = "256Mi"
    max_cpu                = "4"
    max_memory             = "8Gi"
  }
}

variable "pod_security_level" {
  description = "Pod Security Admission level enforced on the namespace."
  type        = string
  default     = "restricted"

  validation {
    condition     = contains(["restricted", "baseline"], var.pod_security_level)
    error_message = "pod_security_level must be restricted or baseline."
  }
}
