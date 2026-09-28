# Native (non-container) Linux kit: fallback only

The primary Linux deployment is containers only
([ADR-023](../../../docs/adr/0023-container-only-linux-deployment.md),
[LINUX_DEPLOYMENT_RUNBOOK.md](../../../docs/deployment/LINUX_DEPLOYMENT_RUNBOOK.md)).

This directory keeps the earlier native kit (a repository checkout, uv, the pinned
CrispASR binary on the host, systemd units) for the case where the NVIDIA
Container Toolkit cannot be used on the host. It needs the full repository and
Python toolchain on the host, which the container path avoids.
