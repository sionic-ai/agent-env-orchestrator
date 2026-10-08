# Guard rails for running agent environments in an EXISTING Kubernetes cluster.
#
# This module only prepares a namespace. aeo does not schedule work on Kubernetes yet; see
# docs/compatibility.md. Nothing here creates clusters, cloud resources, GPUs or secrets.

locals {
  labels = merge(
    {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "agent-env-orchestrator"
    },
    var.labels,
  )
}

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
    labels = merge(local.labels, {
      "pod-security.kubernetes.io/enforce" = var.pod_security_level
      "pod-security.kubernetes.io/audit"   = "restricted"
      "pod-security.kubernetes.io/warn"    = "restricted"
    })
  }
}

resource "kubernetes_resource_quota_v1" "this" {
  metadata {
    name      = "aeo-quota"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
    labels    = local.labels
  }

  spec {
    hard = {
      "pods"            = tostring(var.quota.pods)
      "requests.cpu"    = var.quota.requests_cpu
      "requests.memory" = var.quota.requests_memory
      "limits.cpu"      = var.quota.limits_cpu
      "limits.memory"   = var.quota.limits_memory
      # CPU-only by design: refuse GPU requests in this namespace.
      "requests.nvidia.com/gpu" = "0"
      # Agent pods should not need these; keep them at zero.
      "services.loadbalancers" = "0"
      "services.nodeports"     = "0"
    }
  }
}

resource "kubernetes_limit_range_v1" "this" {
  metadata {
    name      = "aeo-limits"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
    labels    = local.labels
  }

  spec {
    limit {
      type = "Container"
      default = {
        cpu    = var.container_defaults.default_cpu
        memory = var.container_defaults.default_memory
      }
      default_request = {
        cpu    = var.container_defaults.default_request_cpu
        memory = var.container_defaults.default_request_memory
      }
      max = {
        cpu    = var.container_defaults.max_cpu
        memory = var.container_defaults.max_memory
      }
    }
  }
}

# Default deny: selects every pod and allows no ingress and no egress (not even DNS).
# Only effective if the cluster's CNI enforces NetworkPolicy.
resource "kubernetes_network_policy_v1" "default_deny" {
  metadata {
    name      = "aeo-default-deny-all"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
    labels    = local.labels
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

# Pods using the default service account get no API token mounted.
resource "kubernetes_default_service_account_v1" "this" {
  metadata {
    namespace = kubernetes_namespace_v1.this.metadata[0].name
    labels    = local.labels
  }
  automount_service_account_token = false
}
