# Actual UID1000 daemon/frontend fixture proof

The first attempt (`root-daemon-20261013`, project5011) failed before ROOT broker, daemon, frontend or container startup. The quota provisioner and three caller Git commands succeeded. The ROOT installer refused the absent fixed session state directory. This was a fixture setup error; no Runtime admission or frontend turn passed.

The executed harness was `8316d776`, SHA256 `a872f96a53514319d450e9277828d7bf7e30933e7ce915cabe6469673bfbd1c3`. Its final immutable ROOT pins matched the successful composed RuntimeR6 template SHA256 `2c65be7ae393e3febbeeb4c2cd96ce0ab18c27f1d88737c62cb2c058f46558f6` and image `sha256:2a26a7b39d5c878e4780447d8559f8b08903d313e76b9397d3d2ea51653b2f23`.

All five directly spawned processes exited through their retained pidfds. Scope process, socket and registry counts were zero. No CID was created. EnginePID27821/socket device56,inode688 and configuration SHA remained unchanged. The provisioned project5011 tree/policy were preserved as failed-before-container evidence.

`38620be5` fixes the harness to create a fresh ROOT0:0/0755 state directory after controlled-parent validation. The ROOT installer independently creates and verifies the private Git metadata lock. A fresh coordinated scope and reviewed source are required for a retry. The actual fixture turn proves no vendor/provider/auth, paid call, Core or LORE human path.
