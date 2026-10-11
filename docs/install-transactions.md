# Native installer rollback

The release installer records a private protected Codex publication receipt before
switching its `codex-current` pointer. If subsequent DOXA binary staging or handoff
fails, cleanup restores both frontend files and the previous provider pointer.
The receipt contains artifact paths and inode identities, never authentication.
Rollback refuses a pointer changed by another publication, including a repeated
publication of identical bytes. An incomplete rollback keeps backups and the
receipt and reports their paths for review.

The installer and protected-provider script must come from the same selected
revision and support `--rollback-receipt`. Older revisions without that option
refuse protected installation before publication; existing
`DOXA_INSTALL_CODEX_PROTECTED=0` explicitly selects installation without changing
the private provider. Protected Codex availability then depends on a separately
reviewed compatible installation.

Local tests inject staging ENOSPC and mid-handoff failure into the actual shell
installer with local build stand-ins and actual protected-provider publication.
They also verify changed-pointer refusal and private receipt requirements. These
fixtures do not prove power-loss/SIGKILL recovery or arbitrary concurrent frontend
install transactions.
