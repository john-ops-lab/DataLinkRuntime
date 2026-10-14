"""Child-only Linux network denial. The HTTP supervisor retains its listener."""

import ctypes
import errno
import resource


def restrict_child():
    resource.setrlimit(resource.RLIMIT_FSIZE, (96 * 1024 * 1024, 96 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Loaded only inside the child, before document libraries or Office execute.
    # Deny IPv4/IPv6 socket creation; Unix-domain sockets remain available for LO.
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int

    class Compare(ctypes.Structure):
        _fields_ = [
            ("arg", ctypes.c_uint),
            ("op", ctypes.c_uint),
            ("datum_a", ctypes.c_uint64),
            ("datum_b", ctypes.c_uint64),
        ]

    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(Compare),
    ]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)
    if not context:
        raise RuntimeError("child isolation unavailable")
    try:
        for domain in (2, 10):
            comparison = Compare(0, 4, domain, 0)
            if (
                library.seccomp_rule_add_array(
                    context,
                    0x00050000 | errno.EPERM,
                    library.seccomp_syscall_resolve_name(b"socket"),
                    1,
                    ctypes.byref(comparison),
                )
                < 0
            ):
                raise RuntimeError("child network isolation unavailable")
        if library.seccomp_load(context) < 0:
            raise RuntimeError("child network isolation unavailable")
    finally:
        library.seccomp_release(context)
