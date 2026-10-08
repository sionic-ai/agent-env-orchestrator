# Example: apply the aeo namespace guard rails to an existing cluster from your kubeconfig.
#
#   terraform init
#   terraform apply -var kube_context=my-context
#
# No credentials are stored here; the provider reads your local kubeconfig.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "3.3.0"
    }
  }
}

variable "kubeconfig_path" {
  description = "Path to the kubeconfig file."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "kubeconfig context to use (required, to avoid applying to the wrong cluster)."
  type        = string
}

provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

module "aeo_namespace" {
  source = "../../modules/aeo-namespace"

  namespace = "aeo-envs"
}

output "namespace" {
  value = module.aeo_namespace.namespace
}
