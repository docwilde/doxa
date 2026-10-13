# Actual UID1000 daemon and frontend fixture proof

The sixth attempt, `root-daemon-r6-20261013` / project 5019, PASSED. It completed one actual frontend turn, rendered the exact response, verified idle, accepted the owner stop request, exited the frontend and daemon cleanly, and passed every resource cleanup check. Five earlier failed receipts remain preserved below.

All attempts use the disposable `doxa-proof` guest. The caller is UID1000, the independent Engine is UID1001, and the policy and broker are controlled by ROOT. The image and provider files are credential-free fixtures. These receipts do not verify vendor sessions, paid calls, TLS provider traffic, Core, LORE human review, or provider authentication.

## Pinned artifacts

The daemon and frontend were built from source `934661cd`.

- Daemon SHA256: `bdab19e567fe3e7b337414933f64d90be5cd361c55434b482d1d1cd7e378b1aa`.
- Frontend SHA256: `5b4d8ef78eb7c42d84fe9c80f4220cdefdad6d2cad5987399dedc5497aa965c2`.
- Final ROOT broker source: `5e536002`, SHA256 `3305ba7bca32430837331e8613852b48ed486bacd2c35393ab0e27a48c616b7b`. Attempts 1–4 used SHA256 `6f83930003e5976e8e64f4950ff460c8795ffcc5d12c810f1218b09ff9a62fb5`; attempt 5 used the separately recorded broker below.
- Native gateway SHA256: `18d1d633a67aee1dd1bfd67eaf5de82ebcc42dfb0ef112198c6691439de98fe9`.
- Image worker SHA256: `cfbf9586d176289a53cfbe5c9c8fbd9d7f253a4c20525941dceb7bc68bf2dd1f`.
- Immutable image: `sha256:2a26a7b39d5c878e4780447d8559f8b08903d313e76b9397d3d2ea51653b2f23`.
- Successful composed Runtime template SHA256: `2c65be7ae393e3febbeeb4c2cd96ce0ab18c27f1d88737c62cb2c058f46558f6`.

The policy operation limit is 120 seconds. Each daemon proof has one shared body deadline of 240 seconds and a separate cleanup budget of 45 seconds. Each scope receives at most one typed frontend prompt. Old scopes and failed receipts remain preserved. No receipt was promoted after a failure.

## Attempt 1: fixture setup failure

Session `root-daemon-20261013`, project 5011, failed before broker, daemon, frontend or container startup. The quota provisioner and three caller Git commands succeeded. ROOT installation refused the absent fixed state directory. No CID was created. All five directly owned processes exited, and scope process, socket and registry counts were zero. Engine identity and configuration were unchanged.

Executed harness: `8316d776`, SHA256 `a872f96a53514319d450e9277828d7bf7e30933e7ce915cabe6469673bfbd1c3`. Source `38620be5` adds fresh ROOT0:0/0755 state directory creation after controlled-parent validation. The broker installer creates and verifies its private Git metadata lock.

## Attempt 2: first-use modal consumed input

Session `root-daemon-r2-20261013`, project 5015, reached an actual ROOT container, UID1000 daemon registry and two attached clients. The observer received the fixture hello and flat status101, with no running or queued work. Fixture resume=true was rejected. The first-use operations modal consumed input, and no daemon turn events occurred. The body deadline expired.

Executed harness: `87d79c6d`, SHA256 `d1ee3481426cbdb1d34c0126698d79b983b4d6681ef0827aa5ee6151393717c7`. ROOT policy SHA256: `0aae4d9063b722cd0166ea819a7e0195421d19a8986125001c7c233eef285d80`. Exact CID: `6c1402a145ec77f1e51fa7437eb101058b6c41713377c32abddf2a7eb93284da`.

Cleanup passed: CID404, captured cgroup absent, ROOT stopped, all retained process pidfds exited, zero scope processes, sockets and registry entries, and unchanged Engine. No extra prompt, forced CID cleanup or deadline extension occurred.

## Attempt 3: terminal evidence predicate failed

Session `root-daemon-r3-20261013`, project 5016, used the frontend's normal empty `.setup-done` marker, owned by UID1000:1000 with mode0600 and one link in its private DOXA home. The marker schema is identical in frozen source `934661cd` and current source; file blob `7f858c0eea69af7ec2d4bc14ba1be4f8a536f173`. Memory remained disabled through `DOXA_LORE=0`, as confirmed by the actual daemon hello.

The attached opening view rendered, and one prompt was typed. No Enter was sent. The full-prompt rendering predicate timed out because stripping escape codes concatenates painted cursor glyphs and interleaved footer redraws instead of reconstructing current screen cells. No daemon turn events occurred.

Executed harness: `abaf10fd`, SHA256 `41072fcaab922569dfcbd2e7e92d376fbbdff8ac1f5b02d6483e6e276777bcda`. ROOT policy SHA256: `a0a5905514635720a3dc8711c48a3e1e666dad47336f27705d020c79ff2f1b31`. Exact CID: `355f634e62341fccfe51e264c4ed81540c5679eb51f8501a9e3665829afc9060`.

All cleanup checks passed with no errors: CID404, actual cgroup absent, ROOT stopped, all retained pidfds exited, zero scope processes, sockets and registry entries, and unchanged Engine.

## Incremental terminal replay verification

Source `7410bac5` adds a fixture-only incremental 48 × 160 cell replay. It applies cursor movement, overwrites and erases; handles split UTF8, wide cells and bounded combining marks; and accepts only the known style and mode controls plus the frozen frontend's exact OSC22 pointer form. Unsupported controls fail. Limits are 16MiB total input, 131072 controls, 512 characters per pending control, and 16 codepoints per cell.

Redraw, space, erase, UTF8 width, combining marks, byte-by-byte input, unsupported-control and bound checks passed. The actual attempt3 capture contains 18550 bytes, SHA256 `4fd6ec6f97c8ae790b776894ed61fc5244a3c05d89fd13de8ead33e77dcb544d`. Whole-buffer and byte-by-byte replay produced identical cells and reconstructed the full prompt, with 602 controls. This verifies the evidence parser only.

Review found an EOF gap. Source `647f4eae` finalizes UTF8 and rejects pending controls at actual PTY EOF. Errors remain recorded while teardown drains. The final receipt requires complete terminal EOF. Actual PTY tests passed for valid UTF8 and rejected partial CSI and UTF8 on close. Independent review also covered partial OSC and cleanup behavior.

## Attempt 4: frontend turn passed, daemon shutdown failed

Session `root-daemon-r4-20261013`, project 5017, ran the reviewed harness647f4eae, SHA256 `c8df3f1d4c294c25d1154f6961811ce54b9083ebb16dd3c60233d018932db577`. ROOT policy SHA256: `bdc9915c12fa5dc0910043564cbb49d4316fb2b8ff09e2d15b408c9883451bd6`. Exact CID: `0ed4047b457a2b254c076cd123efb64a91c569f0fda8f351970b614a32b5b0ac`.

The one prompt passed the readiness, typing, rendered-input and Enter predicates. The retained observer received exactly one turn_started, one text_delta containing `Deterministic native fixture response.`, and one turn_done. The frontend rendered the response. Status102 showed running=false and queued=0. CtrlQ exited the captured frontend with code0, and actual PTY EOF finalized completely. The existing owner stop_if_idle request103 returned ok=true.

The overall receipt is FAILED. The daemon exited with code1 and stderr `doxa-daemon: ROOT client operation refused, exceeded its bound or timed out`. A successful stop reply and complete resource cleanup do not satisfy the required clean daemon exit.

All cleanup checks passed with an empty error list: ROOT stopped, exact CID404, captured cgroup absent, every directly owned pidfd exited, zero scope processes, sockets and registry entries, complete terminal EOF, and unchanged Engine PID 27821/socket device 56, inode 688/configuration. The faithful opening, prompt and response screen artifacts and full raw trace are retained. No further input, forced cleanup or rerun occurred. This failure led to the shutdown investigation recorded below; attempt 6 verifies the corrected path.

## Attempt 6: complete pass

Fresh session `root-daemon-r6-20261013`, project 5019, used harness `02717f81`, SHA256 `16b7a4921d020e36638a5ae88c23b4fcfa396d2fe35932cde2b2776e23a0f6df`. The final broker source `5e536002`, SHA256 `3305ba7bca32430837331e8613852b48ed486bacd2c35393ab0e27a48c616b7b`, was staged at the unique ROOT-owned `/usr/libexec/doxa/daemon-r6-20261013-broker.py`. The unique template `/etc/doxa/hardened/daemon-r6-template-20261013.json` and installed ROOT policy have SHA256 `2a8b12caff244cd2b506e7ed7b3e6767c61aeee58eb9f12e79aa1764c89ac9e6`. Original templates and all failed scopes remain unchanged.

One prompt passed current-screen readiness, typing, rendered-input and Enter predicates. The retained UID1000 observer received exactly one turn_started, one text_delta containing `Deterministic native fixture response.`, and one turn_done. The actual frontend rendered the response. Status 102 showed running=false and queued=0. CtrlQ exited the captured frontend with code 0 and complete PTY EOF. Owner stop_if_idle request 103 returned ok=true. The captured daemon, observer and ROOT broker each exited with code 0. Fixture resume=true was separately rejected before session startup, as required.

Overall status is PASSED. Exact CID `a659fc0b7339f240f9a045dc975325ad1d7f195f2211b58b520b4f8e26541607` returned 404. Its actual captured cgroup disappeared. ROOT manifest status is stopped. Every directly owned pidfd exited; scope process, socket and registry counts are zero. Terminal EOF finalized completely. Cleanup errors are empty. Engine PID 27821, socket device 56/inode 688, UID/GID 1001, mode0600 and configuration hash remained unchanged. No forced CID cleanup, extra prompt, deadline extension or shared Engine change occurred.

The last native after-stage check succeeded with exit 0 and no diagnostic error. `control-stop-failure.json` and `broker-instance.json` are absent. Other ROOT scope diagnostics and file identities are retained. Private session source/quota data and immutable evidence files remain preserved; there are no running scope services.

The public report SHA256 is `155baaa8744f6ae9a316fab0a191b95778ce5913028f50cedc09a2595561ed9e`. The raw terminal trace is 19129 bytes, SHA256 `2c07b798e20c2c6bd5e6639988ec44cdcc93c7b8aa999df28f955c47c8ac867d`. Reached opening, prompt and response screens are separate artifacts. This pass establishes the credential-free fixture daemon/IsolationHost/ROOT guard/control-plane/frontend join and cleanup. It does not establish vendor inference, TLS provider traffic, authentication, canonical human review, Core or LORE human paths. Fixture checkpoint resume remains refused; composed Runtime cold-resume is a separate proof.

## Reproduction

Only run in the authorized disposable guest with freshly reserved scope paths and the exact protected template and artifacts above. The Root-private script and UID1000-readable observer source must contain identical reviewed bytes. The final sixth attempt used:

```text
python3 -I /root/isolation-proof/daemon-proof-5019.py --ack-disposable \
  --template-policy /etc/doxa/hardened/daemon-r6-template-20261013.json \
  --seconds 240 --output /root/isolation-proof/daemon-run6
```

The harness uses direct typed Engine API calls and retained pidfds for its own children. It never signals processes discovered only by argv or a /proc snapshot. Final status can be passed only after the body and every required cleanup check succeed. Preserve failed scope data; a retry requires a fresh coordinated scope.

## Attempt 4 scoped shutdown diagnostics

Read-only copies of the actual stopped scope are under `run4-scoped-diagnostics`, including ROOT file identities. The manifest remains status=stopped. The last native broker-check succeeded with exit 0, phase `workspace_exec`, family `none`, PID 90824. The last native stage-check recorded `client cancelled`, exit -9, phase `after`, family `none`, PID 90919. Native serve closed with exit -15 and no diagnostic error, PID 90294. The gateway instance remains a historical identity file; broker-instance.json is absent. The enforced quota policy remains ROOT0:0/0600, project 5017, SHA256 `144a0ffa6a02caebe885d4b1d03f93d6270856ac7883fd477fedf41eab9db93e`. No additional control, admission or stop request was sent while collecting these files. These historical diagnostics led to the busy-transport diagnosis and corrected shutdown verified in attempt 6.

## Attempt 5: stop refused a busy transport

Fresh session `root-daemon-r5-20261013`, project 5018, used harness `a3b5c37a`, SHA256 `bd83ee0e32837f587dd46a1f274c8670f0a639cfd87401aadd5d32c66b630fe7`. The new ROOT broker is source `dbf43a81`, SHA256 `4f6b59be1b4baf329b4eb641ef40654eea0d3096555c074b6418096e9955c2f5`, staged at `/usr/libexec/doxa/daemon-r5-20261013-broker.py`. The unique ROOT template `/etc/doxa/hardened/daemon-r5-template-20261013.json` and installed policy have SHA256 `8885ee698c378ca33bf3cd7544c994b53af823f55ba451349955734935f1784f`. Only the broker path/hash and session ID/root differ from the preserved R6 template. Native gateway, worker, caller binaries, image, one prompt, and body/cleanup limits are unchanged.

The actual frontend turn passed every readiness, rendered-input, submission, event, response-render and idle check. Exactly one turn_started, the exact fixture text_delta, and one turn_done were observed. Frontend and observer exited with code 0. Terminal EOF completed, and stop_if_idle request 103 returned ok=true. Overall status remains FAILED because the daemon exited with code 1 and the ROOT client refusal message.

The new ROOT diagnostic identifies operation `stop`, phase `dispatch-idle`, reason `busy-transport`, exception `Refused`, with no errno or nested cause. The last native workspace_exec check succeeded; the after-stage check was cancelled by its client. These are actual scope diagnostics, retained under `run5-scoped-diagnostics`. No extra request was sent during collection.

Exact CID `7e5d4033d01df1d50c3c6ca3495cdca432e8b1a6b416a41e77fe42fb8bc94c30` returned 404. Every cleanup check passed with an empty error list: captured cgroup absent, ROOT stopped, all directly owned process pidfds exited, zero scope processes/sockets/registry entries, complete terminal EOF and unchanged Engine. Old templates, brokers, quota trees and failed receipts remain preserved. No further input or rerun occurred. This failure required the bounded quiescence fix subsequently verified by attempt 6; this attempt remains failed.
