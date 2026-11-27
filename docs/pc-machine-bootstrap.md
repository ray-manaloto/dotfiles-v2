# PC machine bootstrap

The November 25, 2026 Option A cutover moved pc's machine bootstrap to
dotfiles-v2. The canonical declaration is
[`config.pc.toml`](../unix/home/.config/mise/config.pc.toml) in the
`linux,pc` environment. The former cpp-standard `mise.pc.toml` is retired
as a source of host configuration. cpp-standard still owns its CI workflows
and compiler setup.

Run `mise run pc:doctor` to check bootstrap drift and the live kernel,
container privilege, CDI, pending updates, and runner firewall. The
apt-cache exception is a UFW forward rule under `ufw-user-forward`.
The pinned mise firewall backend supports host input and output rules.
Keep the existing forward exception in UFW and use the doctor to verify it.

Before changing the runner firewall, drain and stop the five runner slots,
apply the reviewed file changes, and recheck the fence before starting the
slots. The declarations leave service restarts explicit. NVIDIA supplies
its refresh units with the toolkit package. Its settled CDI specification
lives under `/run/cdi`.
