# Remote Docker Engine capability preflight (mock evidence)

Remote Docker Engines and Docker Desktop are **not available** as DOXA
session-isolation backends. `Policy::validate` accepts only a canonical,
owner-owned local Unix socket, and Docker requests never fall back to native
execution. This data-only preflight tests the shape of evidence a later remote
backend would need. It makes no Docker, SSH, TCP or Desktop connection and
cannot authorize a session.

## Evaluate a task-local fixture

Put a mock JSON object in an owner-private `0600` file under a private
directory on real-disk `TMPDIR`. The fixture format is pinned by
`scripts/test_remote_engine_fixture.py::complete_fixture`. For example,
from the repository root:

```sh
export TMPDIR=/path/on/real/disk
fixture_dir=$(mktemp -d "$TMPDIR/doxa-remote-engine.XXXXXXXX")
PYTHONPATH=scripts python3 - "$fixture_dir/engine.json" <<'PY'
import json, os, sys
from test_remote_engine_fixture import complete_fixture
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as out:
    json.dump(complete_fixture(), out)
PY
python3 scripts/check_remote_engine_fixture.py "$fixture_dir/engine.json"
```

The command reads at most 64 KiB, requires an owner-private regular file and
directory, and prints no fixture contents. Exit **2** means evidence is
incomplete; exit **3** means the mock fields are internally consistent but
unauthenticated. Both are nonzero. JSON always reports
`production_admissible: false`. The fixture must not contain credentials or
private transcripts.

The evaluator requires:

| Gate | Required mock evidence |
| --- | --- |
| Rootless identity | Exact Engine `name=rootless`, a nonroot daemon UID, and matching worker-to-host UID mapping. |
| Private broker | Session-bound authenticated forward, owner-private broker mount, exclusive capability, and no worker Engine socket. |
| Mount ownership | Exact daemon-host checkout, home, cache and read-only broker bind sources under one private session root; no extra or symlinked sources. |
| Effective controls | Engine cgroup v2 capability plus worker-observed private namespace, finite memory/CPU/PID limits, zero swap and write mapping on all three writable binds. |

A JSON fixture is an **assertion**, not an observation from a trusted daemon
host. Even a complete fixture cannot prove that the remote daemon uses those
paths, that its UID mapping matches the client, or that a private broker can
reach the worker without exposing host state. Docker Desktop adds a Linux VM
whose paths and identity must be checked inside that VM, not inferred from
the desktop client. This fixture supplies none of those proofs.

Before production support, DOXA needs an authenticated daemon-host collector
and session-bound broker transport, exact source inode/owner/mount checks on
the Engine host, a real rootless worker probe of cgroup and UID behavior,
and launch/resume/teardown verification against that same incarnation. Those
checks must run in the runtime admission path rather than rely on operator
JSON. The [Docker isolation plan](plans/session-isolation-docker.md) remains
the boundary contract.

Run the no-network regressions with:

```sh
TMPDIR=/path/on/real/disk python3 -m unittest discover -s scripts -p test_remote_engine_fixture.py
```
