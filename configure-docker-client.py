#!/usr/bin/env python3
"""Install verified registry trust and machine credentials. Python 3.9 compatible.

The private configuration stays on the data volume, not in a public template.
Copyright 2026 Ezrael. SPDX-License-Identifier: Apache-2.0
"""
import argparse
import base64
import json
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time


def restrict_networks(config):
    firewall = config.get('firewall')
    if not firewall:
        return
    ports = firewall.get('ports', [])
    if not isinstance(ports, list) or not ports or len(ports) > 8 or any(not isinstance(p, int) or not 1 <= p <= 65535 for p in ports):
        raise RuntimeError('puertos del filtro no válidos')
    policy_path = Path(firewall['policy_file'])
    document = json.loads(policy_path.read_text())
    networks = [ipaddress.ip_network(value, strict=False) for value in document['allowed_networks']]
    if not networks or any(network.prefixlen == 0 for network in networks):
        raise RuntimeError('redes del filtro no válidas')
    for version, command in [(4, 'iptables'), (6, 'ip6tables')]:
        rules = ['*filter', ':LR_ACCESS - [0:0]', '-F LR_ACCESS']
        for network in networks:
            if network.version == version:
                rules.append('-A LR_ACCESS -s %s -j RETURN' % network)
        rules.extend(['-A LR_ACCESS -j DROP', 'COMMIT', ''])
        subprocess.run([command + '-restore', '--wait', '5', '--noflush'], input='\n'.join(rules), text=True, check=True, stdout=subprocess.DEVNULL)
        for chain in ['INPUT', 'DOCKER-USER']:
            if chain == 'DOCKER-USER':
                subprocess.run([command, '-w', '5', '-N', chain], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if chain == 'INPUT':
                desired = [['-p', 'tcp', '-m', 'multiport', '--dports', ','.join(str(p) for p in ports), '-j', 'LR_ACCESS']]
            else:
                # Docker has already translated the destination port. Match
                # the published host port, not an unrelated container's port.
                desired = [['-p', 'tcp', '-m', 'conntrack', '--ctorigdstport', str(port), '-j', 'LR_ACCESS'] for port in ports]
            for rule in desired:
                exists = subprocess.run([command, '-w', '5', '-C', chain] + rule, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if exists.returncode:
                    subprocess.run([command, '-w', '5', '-I', chain, '1'] + rule, check=True)
            if chain == 'DOCKER-USER':
                previous_rule = ['-p', 'tcp', '-m', 'multiport', '--dports', ','.join(str(p) for p in ports), '-j', 'LR_ACCESS']
                old = subprocess.run([command, '-w', '5', '-C', chain] + previous_rule, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if old.returncode == 0:
                    subprocess.run([command, '-w', '5', '-D', chain] + previous_rule, check=True)


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError("no se admiten enlaces en la configuración privada")
    fd, name = tempfile.mkstemp(prefix=".registry-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def configure(path, daemon=Path('/etc/docker/daemon.json'), docker_config=Path('/root/.docker/config.json'), certs=Path('/etc/docker/certs.d')):
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077 or meta.st_size > 65536:
        raise RuntimeError("el fichero de cliente debe ser privado y regular")
    config = json.loads(path.read_text())
    endpoints = config.get('endpoints', {})
    if not isinstance(endpoints, dict) or not 1 <= len(endpoints) <= 64:
        raise RuntimeError("lista de registros inválida")
    ca_path = Path(config['ca_file'])
    ca = ca_path.read_bytes()
    if len(ca) > 16384 or not ca.startswith(b'-----BEGIN CERTIFICATE-----'):
        raise RuntimeError("certificado público inválido")
    for endpoint, credentials in endpoints.items():
        if not re.fullmatch(r'(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\]):[1-9][0-9]{0,4}', endpoint) or int(endpoint.rsplit(':', 1)[1]) > 65535:
            raise RuntimeError("dirección del registro no válida")
        if not isinstance(credentials, dict) or set(credentials) != {'username', 'password'} or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', credentials['username']) or not 32 <= len(credentials['password']) <= 256:
            raise RuntimeError("credencial de máquina inválida")
    # Unraid keeps /root/.docker as a root-owned link to its persistent native
    # Docker configuration. Resolve that existing directory, without relaxing
    # symlink checks for certificates, credentials or arbitrary target files.
    if docker_config.parent.is_symlink():
        resolved_parent = docker_config.parent.resolve(strict=True)
        parent_meta = resolved_parent.stat()
        if parent_meta.st_uid != os.geteuid() or parent_meta.st_mode & 0o022:
            raise RuntimeError('el directorio real de Docker no es privado de su administrador')
        docker_config = resolved_parent / docker_config.name
    existing = json.loads(docker_config.read_text()) if docker_config.exists() else {}
    if existing.get('credsStore') or any(endpoint in existing.get('credHelpers', {}) for endpoint in endpoints):
        raise RuntimeError("este cliente usa un almacén externo: configura sus credenciales con docker login")
    auths = existing.setdefault('auths', {})
    for endpoint, credential in endpoints.items():
        directory = certs / endpoint
        directory.mkdir(parents=True, exist_ok=True)
        atomic(directory / 'ca.crt', ca, 0o644)
        auths[endpoint] = {'auth': base64.b64encode((credential['username'] + ':' + credential['password']).encode()).decode()}
    atomic(docker_config, (json.dumps(existing, indent=2) + '\n').encode())
    daemon_document = json.loads(daemon.read_text()) if daemon.exists() else {}
    before = daemon_document.get('insecure-registries', [])
    after = [value for value in before if value not in endpoints]
    changed = before != after
    if changed:
        if after:
            daemon_document['insecure-registries'] = after
        else:
            daemon_document.pop('insecure-registries', None)
        atomic(daemon, (json.dumps(daemon_document, indent=2) + '\n').encode(), 0o644)
        pidfile = Path('/var/run/docker.pid')
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
            executable = Path('/proc/%d/exe' % pid)
            if executable.exists() and executable.resolve().name == 'dockerd':
                subprocess.run(['kill', '-HUP', str(pid)], check=True)
    restrict_networks(config)
    print('Certificados y credenciales configurados para %d direcciones; HTTPS verificado.' % len(endpoints))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--wait', type=int, default=0)
    parser.add_argument('--watch', action='store_true', help='mantiene el filtro al cambiar las redes en la app')
    args = parser.parse_args()
    deadline = time.monotonic() + min(max(args.wait, 0), 300)
    while not args.config.exists() and time.monotonic() < deadline:
        time.sleep(2)
    try:
        if args.watch:
            import fcntl
            lock = open('/run/local-registry-client.lock', 'w')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit(0)
            previous = None
            while True:
                try:
                    document = json.loads(args.config.read_text())
                    paths = [args.config, Path(document['ca_file'])]
                    if document.get('firewall'):
                        paths.append(Path(document['firewall']['policy_file']))
                    fingerprint = hashlib.sha256(b''.join(p.read_bytes() for p in paths)).digest()
                    if fingerprint != previous:
                        configure(args.config)
                        previous = fingerprint
                except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
                    print('Acceso seguro: no se ha podido actualizar la configuración; se conserva el filtro anterior.', flush=True)
                time.sleep(10)
        else:
            configure(args.config)
    except (OSError, ValueError, RuntimeError, KeyError) as e:
        raise SystemExit('No se pudo preparar el acceso seguro de Docker: %s' % e)
