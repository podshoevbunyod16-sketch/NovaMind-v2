"""Apply modest process limits, then replace this helper with the requested command.

This helper is a resource limiter only; it is not an OS/container security sandbox.
"""
import os
import resource
import sys

_LIMITS = (
    (resource.RLIMIT_CPU, 30),
    (resource.RLIMIT_AS, 512 * 1024 * 1024),
    (resource.RLIMIT_FSIZE, 10 * 1024 * 1024),
    (resource.RLIMIT_NOFILE, 64),
    (resource.RLIMIT_NPROC, 32),
)


def main():
    if len(sys.argv) < 2:
        return 2
    for limit, value in _LIMITS:
        try:
            resource.setrlimit(limit, (value, value))
        except (OSError, ValueError):
            # Some platforms expose a limit but do not permit changing it.
            continue
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
