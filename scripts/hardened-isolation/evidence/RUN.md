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
