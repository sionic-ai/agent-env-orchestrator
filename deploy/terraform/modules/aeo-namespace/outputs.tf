output "namespace" {
  description = "Name of the created namespace."
  value       = kubernetes_namespace_v1.this.metadata[0].name
}

output "network_policy" {
  description = "Name of the default-deny NetworkPolicy."
  value       = kubernetes_network_policy_v1.default_deny.metadata[0].name
}
