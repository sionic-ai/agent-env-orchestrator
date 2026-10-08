# Terraform: optional guard rails for an existing Kubernetes cluster

`modules/aeo-namespace` prepares **one namespace** in a cluster you already operate:

| resource | purpose |
| -------- | ------- |
| `kubernetes_namespace_v1` | namespace labelled for Pod Security Admission (`enforce=restricted` by default) |
| `kubernetes_resource_quota_v1` | pods/CPU/memory caps; `requests.nvidia.com/gpu = 0`; no LoadBalancer/NodePort services |
| `kubernetes_limit_range_v1` | default requests/limits and a per-container max |
| `kubernetes_network_policy_v1` | default deny of all ingress **and** egress (including DNS) |
| `kubernetes_default_service_account_v1` | `automount_service_account_token = false` |

It does not create clusters, cloud resources, node pools, GPUs, secrets or Helm releases.
**aeo does not run environments on Kubernetes yet.** This module is groundwork for a future
backend (see `docs/compatibility.md`).

## Usage

```bash
cd examples/existing-cluster
terraform init
terraform apply -var kube_context=<your-context>      # reads ~/.kube/config
```

`examples/existing-cluster/.terraform.lock.hcl` pins `hashicorp/kubernetes` 3.3.0 with
hashes for linux/darwin × amd64/arm64.

## What was verified (2026-10-08)

On a local kind v0.33.0 cluster (Kubernetes v1.37.0) with the **Calico v3.33.0** CNI, using
Terraform 1.16.5:

- `terraform apply` created all 5 resources, and `terraform destroy` removed them.
- A privileged pod was rejected by Pod Security (`violates PodSecurity "restricted:latest"`).
- A pod requesting `nvidia.com/gpu: 1` was rejected (`exceeded quota: aeo-quota`).
- A compliant pod got the LimitRange defaults and **no** service-account token. Its TCP
  connections to the API server and to a CoreDNS pod, and its DNS lookups, all failed. The
  same pod in `default` reached all three.

NetworkPolicy only takes effect if your CNI enforces it. With kind's default CNI (kindnet),
the same policy did **not** block egress in our test. Check this on your own cluster.

CI runs `terraform fmt -check`, `init -backend=false` and `validate`. It does not apply
against a cluster.
