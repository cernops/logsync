# logsync

`logsync` drains HTCondor job event (UserLog) files by durably copying complete records to their destinations and punching the copied ranges from the source files.

It runs on modern Linux with Python 3.11 or newer and requires source-filesystem support for sparse hole punching, atomic `RENAME_NOREPLACE`, and Linux file leases.

Example to run it for one user with explicit source and destination prefixes:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --user alice
```

See the [complete usage and operations guide](docs/README.md) and [metrics reference](docs/METRICS.md) for details.

See [LICENSE](LICENSE) for the license terms and [NOTICE](NOTICE) for the copyright notice.
