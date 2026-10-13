# Disposable guest read-only peer proof, 2026-10-13

Source baseline 4e7625bd plus fake-peer lifetime-barrier correction. The fixture
originally exited after accept, before executable hashing, producing ESRCH/ENOENT;
it now stays alive through the pinned-peer check. Actual socket was independently
normalized by the coordinator to reviewed 1001:1001, mode0600 without restart.

Guest command:

```sh
python3 -I /root/isolation-proof/broker-primitives/prove_broker_guest.py --socket /run/user/1001/doxa-proof/docker.sock --exe /usr/bin/dockerd --config /etc/doxa-proof/engine-daemon.json
```

Result: exit0. Actual Engine UID1001 PID27821, socket device56/inode688 and root
config/executable hashes stayed unchanged. `/info` was read through the broker's
actual pinned Engine transport. UID1000 could not open the Engine socket. A fake
Python peer with the *same Engine UID1001*, same reviewed real config/binary path
pins and its own unique private socket was rejected by actual SO_PEERPIDFD plus
peer executable hash; its JSON/UID cannot manufacture the executable identity.

Only `/root/isolation-proof/broker-primitives` proof copies and a unique temporary
negative-peer socket were written. The socket was cleaned up. No Engine restart,
config change, container operation, provider call or live workstation provisioning
occurred. These facts do **not** prove composed container origin, quota, filter,
gateway, root callback or real-provider admission. Evidence explicitly reports
`admissible_as_hardened:false`; integration proofs belong to the coordinator.

ROOT-generated Git metadata marker regression, baseline20508042:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I /root/isolation-proof/broker-git-safe-20508042/prove_git_safe_guest.py
```

Exit0. Actual Git2.53.0 runs as UID1001/GID1001. Malicious repository clean,
smudge and process filters execute markers through unsafe diff/apply; eight
reviewed commands using ROOT-generated readonly metadata produce no markers.
The safe ordinary apply updates worktree bytes; the original index SHA remains
unchanged. ROOT:EngineGID0550 directories and0440 single-link files verified
(21 entries,663 logical bytes). `apply --cached`/`--index` are refused. First
attempt failed because fixture policy omitted session_id; fixed that fixture
field and reran. Both attempts removed their unique /run/doxa proof scope.
Only versioned proof source copies remain under /root/isolation-proof. No Engine
API, quota installation, provider or workstation provisioning was performed.
Evidence: git-safe-20261013.json, `admissible_as_hardened:false`.

Actual UID1000 client proof, latest baseline18a45886:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I /usr/libexec/doxa-client-peer-proof-18a45886/prove_root_client_guest.py
```

Package artifacts are ROOT0:0 mode0644, parents ROOT0755. Tar source-owner and
group-write modes initially failed root-control setup checks; corrected only the
unique proof package. The updated fixture cleans its own scope after setup
failure as well as success. Exit0: UID1000 connected ROOT brokerPID47166 through
actualSO_PEERPIDFD/fresh nonce. ROOT `/proc/PID/exe` remains EACCES13; readable
live stat starttime856669 equals instance before/after the CLI challenge. Reply
unprepared/CIDnull, no Engine call or provider/quota installation. Runtime and
policy scopes were removed, proof package retained. Evidence root-client-20261013.json.
