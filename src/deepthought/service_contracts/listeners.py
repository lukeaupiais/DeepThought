"""Ports that programs of the repository listen on, from their configuration.

A request to a loopback host (``localhost``, ``127.0.0.1``) with a literal
port reaches whichever program listens on that port. When the repository
declares that program, only the routes it serves are candidates.

Read so far: Kratos service configuration, ``configs/*.yaml`` whose
``server.http.addr`` is ``host:port`` (the layout ``kratos new`` creates and
the ``conf.Server`` message reads). The program is the directory holding
``configs``.
"""

import re

from .deployed import _yaml_files

LOOPBACK = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "[::1]"})


def kratos_ports(root):
    """{port: {program directory ("" for the root)}} of Kratos configs."""
    try:
        import yaml
    except ImportError:
        return {}
    found = {}
    for path, relative in _yaml_files(root):
        parts = relative.split("/")
        if len(parts) < 2 or parts[-2] != "configs":
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        server = data.get("server") if isinstance(data, dict) else None
        http = server.get("http") if isinstance(server, dict) else None
        address = http.get("addr") if isinstance(http, dict) else None
        match = (
            re.fullmatch(r"[^:\s]*:(\d+)", address)
            if isinstance(address, str)
            else None
        )
        if match:
            found.setdefault(int(match.group(1)), set()).add("/".join(parts[:-2]))
    return found


def under(file, directory):
    return directory == "" or file == directory or file.startswith(directory + "/")
