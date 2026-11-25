FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive container=docker
STOPSIGNAL SIGRTMIN+3

RUN apt-get update && apt-get install -y --no-install-recommends \
    systemd systemd-sysv dbus dbus-user-session \
    docker.io containerd runc \
    bash ca-certificates curl wget git sudo nano vim-tiny less procps \
    util-linux coreutils iproute2 iputils-ping dnsutils net-tools \
    python3 python3-pip build-essential htop btop tmux screen unzip zip tar \
    fastfetch sysstat lsof pciutils usbutils file tree jq rsync ncdu \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# SSHX is used for the browser terminal.
RUN curl -fsSL https://sshx.io/get | sh && \
    SSHX_BIN="$(find /root /usr/local/bin /usr/bin -type f -name sshx -print -quit 2>/dev/null)" && \
    test -n "$SSHX_BIN" && ln -sf "$SSHX_BIN" /usr/local/bin/sshx

RUN mkdir -p /etc/kts /opt/kts-storage /var/lib/kts /etc/systemd/system /etc/systemd/system/multi-user.target.wants && \
    ln -sf /lib/systemd/system/containerd.service /etc/systemd/system/multi-user.target.wants/containerd.service && \
    ln -sf /lib/systemd/system/docker.service /etc/systemd/system/multi-user.target.wants/docker.service && \
    ln -sf /dev/null /etc/systemd/system/dev-hugepages.mount && \
    ln -sf /dev/null /etc/systemd/system/sys-kernel-debug.mount && \
    ln -sf /dev/null /etc/systemd/system/sys-kernel-tracing.mount

COPY scripts/ /usr/local/lib/kts/
COPY config/ /etc/kts/
RUN chmod +x /usr/local/lib/kts/* && \
    /usr/local/lib/kts/install-command-compat && \
    cp /usr/local/lib/kts/kts-resource-view /usr/local/sbin/kts-resource-view && \
    chmod +x /usr/local/sbin/kts-resource-view && \
    printf '[Unit]\\nDescription=KTS VPS resource view\\nDefaultDependencies=no\\nAfter=local-fs.target\\nBefore=basic.target\\n[Service]\\nType=oneshot\\nExecStart=/usr/local/sbin/kts-resource-view\\nRemainAfterExit=yes\\n[Install]\\nWantedBy=basic.target\\n' > /etc/systemd/system/kts-resource-view.service && \
    ln -sf /etc/systemd/system/kts-resource-view.service /etc/systemd/system/basic.target.wants/kts-resource-view.service && \
    \
    useradd -m -s /bin/bash ubuntu && \
    echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/ubuntu && chmod 0440 /etc/sudoers.d/ubuntu

RUN printf '\n# KTS HOST VPS environment\nexport TERM=xterm-256color\n' >> /etc/skel/.bashrc && \
    printf '\n# KTS HOST VPS environment\nexport TERM=xterm-256color\n' >> /home/ubuntu/.bashrc

WORKDIR /home/ubuntu
CMD ["/sbin/init"]
