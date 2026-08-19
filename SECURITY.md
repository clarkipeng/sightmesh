# Security policy

## Experimental status and supported versions

SightMesh is experimental. Security fixes are provided for the latest tagged release and the current `main` branch on a best-effort basis. Older releases are not supported. The tested platform and dependency versions are listed in [docs/compatibility.md](docs/compatibility.md).

## Threat boundary

SightMesh launches local Claude Code and Codex processes with the permissions the operator grants them. Those agents may execute arbitrary commands, read accessible files, modify repositories, or invoke configured tools. Git worktrees isolate source changes from one another; they are not sandboxes and do not isolate credentials, processes, or the host.

The local user, cdesktop, provider CLIs, Repowire, Git, configured tools, and their on-disk state are inside the trusted boundary. Loopback services reduce network exposure but do not defend against malicious software running as the same user. Review agent prompts, approvals, diffs, and third-party dependencies before granting unattended permissions.

Credential pools are only for accounts the operator owns and authenticated through each provider's normal interface. SightMesh does not support extracting or replaying browser credentials, copying auth headers or cookies, sharing a login between users, or evading quotas or rate limits. Pool secrets and account-specific homes are local sensitive data; protect the host account and backups accordingly.

## Report a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's **Security → Report a vulnerability** private reporting flow for this repository. Include the affected version or commit, impact, minimal reproduction, and any suggested mitigation. Do not include live credentials, private transcripts, or unrelated user data.

You should receive an acknowledgement within seven days. Triage and remediation are best effort; there is no security SLA during the experimental period. Coordinated disclosure is requested until a fix or mitigation is available.
