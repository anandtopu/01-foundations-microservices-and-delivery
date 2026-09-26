# syntax=docker/dockerfile:1
# Meridian's DMZ SFTP drop (spec M2): OpenSSH, a chrooted read-only "gateway" user, key auth only.
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server \
 && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*

# nologin shell: the account can do SFTP (internal-sftp) and nothing else.
# "-p '*'" = no usable password but NOT locked: with UsePAM off (our minimal sshd_config),
# sshd refuses even key logins for a locked ("!") account.
RUN useradd --create-home --shell /usr/sbin/nologin gateway && usermod -p '*' gateway

# Chroot rules (spec section 12): every path component of ChrootDirectory must be owned by root and
# not group/world writable. Only the subdirectories may belong to someone else, and here nothing does:
# the gateway user needs read access only, and the IBM i job (us, in the lab) writes via a bind mount.
RUN mkdir -p /srv/sftp/outbound/shipments /run/sshd /var/lib/sftp-hostkeys \
 && chown root:root /srv/sftp /srv/sftp/outbound \
 && chmod 755 /srv/sftp /srv/sftp/outbound /srv/sftp/outbound/shipments

# The gateway's PUBLIC key (spec section 6: cp secrets/gateway_ed25519.pub mocks/sftp/).
COPY --chown=gateway:gateway --chmod=600 gateway_ed25519.pub /home/gateway/.ssh/authorized_keys
RUN chmod 700 /home/gateway/.ssh

COPY sshd_config /etc/ssh/sshd_config
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
EXPOSE 22
VOLUME ["/var/lib/sftp-hostkeys"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
