# PC machine bootstrap

The November 25, 2026 Option A cutover moved pc's machine bootstrap to
dotfiles-v2. The canonical declaration is
[`config.pc.toml`](../unix/home/.config/mise/config.pc.toml) in the
`linux,pc` environment. The former cpp-standard `mise.pc.toml` is retired
as a source of host configuration. cpp-standard was sunset on November 27,
2026, and nothing on pc is maintained from it. Its compiler-runner files on
pc (`/etc/gha-runner/cpp-compiler` and the `cpp-standard-amd64` runner
group) are legacy: do not extend them. This repository is the source of
truth for the runner launcher and `agent-secrets`.

Run `mise run pc:doctor` to check bootstrap drift, live kernel settings,
the Docker `--privileged` flag, CDI, pending updates, and runner firewall. The
apt-cache exception is a UFW forward rule under `ufw-user-forward`.
The pinned mise firewall backend supports host input and output rules.
Keep the existing forward exception in UFW and use the doctor to verify it.

Before changing the runner firewall, drain and stop the five runner slots,
apply the reviewed file changes, and recheck the fence before starting the
slots. The declarations leave service restarts explicit. NVIDIA supplies
its refresh units with the toolkit package. Its settled CDI specification
lives under `/run/cdi`.

Drain the slots before changing `gha-slot`, as Bash reads it incrementally.

## Cooling and hardware monitoring

The `linux,pc` bootstrap declares the NCT6775 module-load file, the NZXT H1
pump and radiator fan services, and the hardware watch and weekly heartbeat
timers. The timers start their static oneshot services. The cooling scripts
and unit files mirror the live files and have no automatic restart on file
changes. On a rebuild, verify the cooling units and tachometer readings
before putting the machine under load. The `cooling` line in `mise run pc:doctor`
checks both units and reads the pump and radiator fan speeds from hwmon.

Restore `/usr/local/sbin/hw-heartbeat`, `/usr/local/sbin/hw-notify`,
`/usr/local/bin/ntfy-qr`, and the root-only `/etc/hw-watch/ntfy-topic`
from the private machine configuration before enabling the monitoring timers
on a rebuild. These files carry host-specific notification details. Without
them, hardware watch still logs sensor readings to the journal, but phone
and desktop alerts and the weekly heartbeat are unavailable.
