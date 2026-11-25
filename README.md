# KTS HOST VPS Maker — systemd + private IPv4 edition

## What this build adds

- Ubuntu 24.04 image boots with **systemd as PID 1** (not `sleep infinity`).
- Every VPS has its **own Docker Engine daemon** (`dockerd`) and its own persistent `/var/lib/docker` volume. `docker.io`, `containerd`, and `runc` are installed in the guest image, so `apt install docker.io` works normally and `systemctl enable --now docker` manages the guest daemon.
- Each VPS is attached to a dedicated Docker bridge network and receives a unique **private IPv4 address**.
- Every VPS can independently bind to port 80, 443, 22, or any other port because the private IP differs.
- RAM and CPU scheduling quota are set by Docker.
- Persistent Docker volume per VPS.
- SSHX on-demand browser terminal.
- Guest resource compatibility layer: allocation-aware `free`, `nproc`, `lscpu`, `getconf`, Fastfetch/Neofetch-style output and `/proc` bind-mount attempt.
- 100+ common command names are routed through a compatibility shim; commands not specifically virtualized pass through to Ubuntu unchanged.
- Discord commands: `<create`, `<myvps`, `<info`, `<connect`, `<start`, `<stop`, `<restart`, `<delete`, `<list`, `<help`.

## Critical networking distinction

The assigned address (default range `172.30.0.0/16`) is a **private/local IPv4**, not a public IPv4.

All 100 containers can listen on their own `172.30.x.x:80` simultaneously. The host and other containers on that Docker network can reach those addresses. This does **not** make them directly reachable from the public internet.

Because the parent VM has no public IPv4, public inbound access requires an external public ingress point:
- HTTP/HTTPS: a reverse proxy on a server with a public IP, or a suitable tunnel.
- Arbitrary TCP/UDP ports: a public relay/router with explicit forwarding, or a provider-routed IPv6/public subnet.
- A single public IPv4 cannot expose the same raw TCP port to 100 different VPSs without a routing/proxy layer or separate public IPs. For HTTP, hostnames can be routed by a reverse proxy.

This project does not invent public IPs and does not configure a public relay.

## Pterodactyl compatibility and security

- Pterodactyl Panel services (Nginx/PHP/MariaDB) can run in the systemd container if systemd works on the outer host.
- Wings needs a working Docker daemon and substantial container privileges. **Do not mount the host `/var/run/docker.sock` into customer VPSs**: that effectively grants control of the host and other customers' containers.
- Each VPS is launched with Docker `--privileged`, a writable cgroup mount, its own network namespace, and its own persistent `/var/lib/docker`. This follows the nested-Docker pattern in the linked KTS Host `crispy-lamp` project. It does not mount the host Docker socket. **Important:** privileged guests are not a hard security boundary; root inside one can affect the parent Docker host/kernel and potentially other VPSs. Use only on a host you control and accept that risk.
- Wings can be attempted inside this environment, but compatibility depends on the outer LXC allowing the required cgroups, namespaces, networking and kernel features. Test Wings before promising it to customers.
- Systemd support depends on the parent LXC allowing cgroup access and the required capabilities. The bot reports provisioning failures if Docker cannot start the image.

## Disk and CPU truth

- `MAX_DISK_GB` is a requested size ceiling only. A Docker named volume on this nested-LXC filesystem is not a hard quota or independent root disk.
- CPU `--cpus` is a scheduling quota, not dedicated physical core pinning.
- RAM is cgroup-limited, subject to parent host limits.
- Do not market these as dedicated physical resources or independent block disks.

## Install

```bash
unzip kts-host-vps-maker-v2.zip
cd kts-host-vps-maker-v2
cp .env.example .env
nano .env
chmod +x install.sh
./install.sh
```

Set your Discord token and admin user ID in `.env`. Enable Message Content Intent in the Discord Developer Portal.

## Commands

```text
<create @member RAM_GB CPU_CORES DISK_GB
<myvps
<info VPS_ID
<connect VPS_ID
<start VPS_ID
<stop VPS_ID
<restart VPS_ID
<delete VPS_ID
<list
<help
```

Example:
`<create @customer 4 2 40`

## Verify nested Docker inside a newly created VPS

```bash
systemctl status docker
systemctl enable --now docker
docker version
docker run --rm hello-world
```

Docker Engine data is stored in a separate persistent volume for each VPS.

## Verify inside a newly created VPS

```bash
ps -p 1 -o comm=
systemctl is-system-running
systemctl status
ip -4 addr
ip route
ss -lntup
```

Expected PID 1 is `systemd`. `systemctl is-system-running` may report `degraded` in a container if optional host-only services are masked; check the specific services you need.

## Verify networking on the outer Docker host

```bash
docker network inspect kts-vps-net
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' kts-vps-<ID>
```

The Docker bridge subnet must not overlap with your host/LAN/VPN networks. Change `VPS_SUBNET` and `VPS_GATEWAY` in `.env` if it does.

## Limitations

No public IPv4 assignment, public port relay, hard disk quota, dedicated physical CPU pinning, backups, billing, or bandwidth quota is included. Privileged nested Docker is powerful but is not a tenant-grade security boundary. Test systemd on your exact outer LXC before offering this to customers.


## Guest system-command compatibility layer

The image installs a compatibility shim under `/usr/local/bin` for 100+ common command names. The shim preserves normal behavior for commands it does not virtualize. The allocation-aware outputs are:

- `free -h`: reports the configured RAM limit and cgroup memory usage.
- `nproc`, `getconf _NPROCESSORS_ONLN`: report the configured CPU quota's visible vCPU count.
- `lscpu`: reports a generic **KTS Virtual CPU** and explicitly labels the CPU allocation as a shared scheduling quota, not dedicated cores.
- `fastfetch`, `neofetch`, `screenfetch`, `ktsfetch`: show the configured RAM and CPU quota.
- `cat /proc/meminfo` and `cat /proc/cpuinfo`: compatibility output when invoked through the `cat` shim. At boot, the image also attempts bind mounts over these proc entries; the outer container/kernel may prohibit this.

Important: this is a guest presentation/compatibility layer, not hardware virtualization. Tools that directly inspect sysfs, use their own proc readers, or bypass `/usr/local/bin` may still see host/kernel details. `df -h` deliberately reports the real backing filesystem; the requested disk size is not a hard quota or independent disk. CPU quota is not dedicated CPU pinning. Do not advertise these as dedicated hardware.

The command shim currently has more than 100 command-name entry points, but **not all 100+ commands are spoofed**: most pass through to their real Ubuntu implementation. Only the resource-reporting commands listed above are virtualized. This is intentional to avoid breaking package management, networking, systemd, Docker, and ordinary shell behavior.
