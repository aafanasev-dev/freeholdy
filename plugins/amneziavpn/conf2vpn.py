#!/usr/bin/env python3
# conf2vpn.py — convert an AmneziaWG client .conf into the Amnezia native
# `vpn://` connection string (and, optionally, its raw JSON).
#
# The Amnezia mobile/desktop apps do NOT import raw WireGuard .conf text via QR.
# They expect their own format: a base64url(qCompress(JSON)) blob prefixed with
# "vpn://". qCompress is Qt's framing — a 4-byte big-endian uncompressed-length
# header followed by a zlib (deflate) stream. This script reproduces that.
#
#   conf2vpn.py CLIENT.conf [--name NAME]   # prints the vpn:// string
#   conf2vpn.py CLIENT.conf --json          # prints the inner JSON instead
#
# Reads the .conf from a path or '-' (stdin). The public key needed by the JSON
# is derived from the client PrivateKey (X25519) — no server round-trip needed.

import argparse
import base64
import json
import sys
import zlib


def parse_conf(text):
    """Parse a WireGuard/AmneziaWG .conf into {'Interface': {...}, 'Peer': {...}}."""
    section = None
    out = {"Interface": {}, "Peer": {}}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            out.setdefault(section, {})
            continue
        if section is None or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[section][key.strip()] = val.strip()
    return out


def pubkey_from_priv(priv_b64):
    """Derive the WireGuard public key (base64) from a base64 private key.

    Uses `cryptography` (X25519) when available. Callers without it can pass the
    key in via --pubkey (e.g. derived inside the container with `awg pubkey`).
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    raw = base64.b64decode(priv_b64)
    priv = X25519PrivateKey.from_private_bytes(raw)
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(pub).decode()


# AmneziaWG obfuscation params carried in both the outer container and last_config.
AWG_PARAMS = ["Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"]


def build_json(conf, name, client_pub=None):
    iface = conf.get("Interface", {})
    peer = conf.get("Peer", {})

    priv = iface.get("PrivateKey", "")
    if not client_pub:
        client_pub = pubkey_from_priv(priv) if priv else ""

    client_ip = iface.get("Address", "").split(",")[0].strip().split("/")[0].strip()

    endpoint = peer.get("Endpoint", "")
    host, _, port = endpoint.rpartition(":")
    host = host or endpoint
    port = port or "51820"

    dns_field = iface.get("DNS", "")
    dns_parts = [d.strip() for d in dns_field.split(",") if d.strip()]
    dns1 = dns_parts[0] if dns_parts else "1.1.1.1"
    dns2 = dns_parts[1] if len(dns_parts) > 1 else "1.0.0.1"

    allowed = [a.strip() for a in peer.get("AllowedIPs", "0.0.0.0/0, ::/0").split(",") if a.strip()]

    awg = {p: iface.get(p, "") for p in AWG_PARAMS}

    last_config = {
        **awg,
        "allowed_ips": allowed,
        "clientId": client_pub,
        "client_ip": client_ip,
        "client_priv_key": priv,
        "client_pub_key": client_pub,
        "config": conf_text_with_keepalive(conf),
        "hostName": host,
        "mtu": "1376",
        "persistent_keep_alive": "25",
        "port": int(port) if port.isdigit() else port,
        "psk_key": peer.get("PresharedKey", ""),
        "server_pub_key": peer.get("PublicKey", ""),
        "transport_proto": "udp",
    }

    container_awg = {
        **awg,
        "last_config": json.dumps(last_config, indent=4),
        "port": str(port),
        "transport_proto": "udp",
    }

    return {
        "containers": [{"awg": container_awg, "container": "amnezia-awg"}],
        "defaultContainer": "amnezia-awg",
        "description": name,
        "dns1": dns1,
        "dns2": dns2,
        "hostName": host,
    }


def conf_text_with_keepalive(conf):
    """Re-render the .conf, ensuring the peer has PersistentKeepalive (Amnezia uses 25)."""
    iface = conf.get("Interface", {})
    peer = dict(conf.get("Peer", {}))
    peer.setdefault("PersistentKeepalive", "25")

    iface_order = ["Address", "DNS", "PrivateKey"] + AWG_PARAMS
    peer_order = ["PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive"]

    def emit(section, order):
        lines = []
        for k in order:
            if k in section:
                lines.append(f"{k} = {section[k]}")
        for k, v in section.items():
            if k not in order:
                lines.append(f"{k} = {v}")
        return lines

    out = ["[Interface]"] + emit(iface, iface_order) + ["", "[Peer]"] + emit(peer, peer_order)
    return "\n".join(out) + "\n"


def encode_vpn(obj):
    payload = json.dumps(obj).encode()
    header = len(payload).to_bytes(4, "big")
    compressed = zlib.compress(payload, 8)
    blob = header + compressed
    b64 = base64.urlsafe_b64encode(blob).decode().rstrip("=")
    return "vpn://" + b64


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("conf", help="path to client .conf, or '-' for stdin")
    ap.add_argument("--name", default=None, help="server label shown in the app")
    ap.add_argument("--pubkey", default=None,
                    help="client public key (skip X25519 derivation; e.g. `awg pubkey`)")
    ap.add_argument("--json", action="store_true", help="print inner JSON instead of vpn://")
    args = ap.parse_args()

    if args.conf == "-":
        text = sys.stdin.read()
        name = args.name or "VPN Server"
    else:
        with open(args.conf) as f:
            text = f.read()
        import os
        name = args.name or os.path.splitext(os.path.basename(args.conf))[0]

    conf = parse_conf(text)
    try:
        obj = build_json(conf, name, client_pub=args.pubkey)
    except ImportError:
        sys.exit("conf2vpn: need the `cryptography` module or pass --pubkey "
                 "(derive it with: echo PRIVKEY | docker exec -i CONTAINER awg pubkey)")

    if args.json:
        print(json.dumps(obj, indent=2))
    else:
        print(encode_vpn(obj))


if __name__ == "__main__":
    main()
