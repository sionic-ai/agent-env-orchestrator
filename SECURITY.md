# Security policy

## Supported versions

Only the latest release on the `main` branch gets security fixes while the project is pre-1.0.

## What aeo does and does not protect against

aeo isolates agents with standard container controls on a local Docker daemon: no network,
dropped capabilities, read-only root filesystem, non-root user, resource limits, and a
separate read-only verifier. It is **not** a VM-grade sandbox for deliberately malicious code
and is not a multi-tenant service. See [docs/security.md](docs/security.md) for the threat
model and known limitations. Reports that only show a documented limitation (e.g. the
workspace volume has no disk quota) are welcome as issues but are not vulnerabilities.

## Reporting a vulnerability

Please **do not** open a public issue for a vulnerability. Use GitHub's private reporting:
on the repository page, go to **Security → Report a vulnerability**. Please include:

- the aeo version or commit and your Docker version and OS;
- a minimal environment spec / asset tree / command that shows the problem;
- what you expected to happen and what happened.

We aim to acknowledge reports within 5 working days. Please give us reasonable time to fix
the issue before you disclose it publicly.

Do not include real credentials, private datasets or private model endpoints in reports.
