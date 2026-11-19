# Agent rules — WhisperLive workspace

## Process creation

- **Always inform the operator before creating a systemd unit or a container.**
  State what will be created, where it runs, and how to remove it — then wait
  for approval.
- **All services must run under uid 1000.** systemd *user* units already do;
  containers must be rootless with the uid mapping to host uid 1000.
- **Containers must use the default userns and default user** — no
  `userns_mode`, `user:`, or `--user` overrides. Under rootless Podman the
  default mapping already runs container root as host uid 1000.
