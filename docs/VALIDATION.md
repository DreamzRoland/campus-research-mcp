# Public snapshot validation

Validated on 2026-11-08 against the sanitized publication tree, not against a
copy containing production credentials.

- 277 Linux offline regression tests passed using Python 3.12 and the project's
  installed dependency baseline.
- STDIO MCP discovery found 40 tools; local ping and input-limit checks passed.
- No real API key was loaded and no publisher download was performed by this
  offline acceptance run. This is not proof of a new user's API/full-text rights.
- Gitleaks 8.30.1 filesystem scan reported no findings. The scanner archive was
  checked against the official release checksum list.
- Additional checks covered private deployment markers, personal paths,
  unexpected email addresses, hosted workspace/file identifiers and local
  documentation links. Loopback addresses and synthetic URL/security fixtures
  are intentional.
- The publication starts with a new Git history and a GitHub noreply commit
  identity. Private deployment evidence and the original Git history are absent.

The test workflow reruns the offline suite on GitHub. New environment dependency
installation and external-source access can fail independently of this snapshot
validation; read the workflow result and source-specific status before use.
