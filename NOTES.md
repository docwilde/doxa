# Docker hardened boundary follow-on

- Base: `feat/beta36-followons` at `1ee7775a`; isolated branch `feat/docker-hardened-next`.
- Scope: use the existing kernel per-send pidfd/credential frame reader in the production Docker `HookBroker` path. Require listener options before accepting bytes and fail the hook closed when unavailable.
- Admission remains closed: no authenticated exact-container Engine observation, writer-to-container binding, process-movement proof, durable production quota service or provider egress proof.
- Validation: `cargo test --locked -p doxa-isolation broker -- --nocapture` passed 19 focused tests; `cargo test --locked -p doxa-isolation` passed 91 tests with four explicitly ignored task-local Docker fixtures. `git diff --check` passed.
- Operational limit: no task-local rootless Engine/image was provided, so the actual Docker hook command was not exercised inside a container. Linux `SO_PASSPIDFD` or `SO_PASSCRED` absence now blocks Docker hook startup.
