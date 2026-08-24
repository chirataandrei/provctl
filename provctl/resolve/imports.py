"""Pull top-level imports out of Python source via ast."""

from __future__ import annotations

import ast
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

# Populated by CPython since 3.10; covers the stdlib for the *running*
# interpreter. See note in `is_stdlib` about why that is a caveat, not a bug.
_STDLIB = set(sys.stdlib_module_names)

# Not in stdlib_module_names but never a PyPI dependency worth flagging.
_ALWAYS_IGNORE = {"__future__", "__main__"}

# Stdlib names that exist in *some* Python version but not the one running
# provctl. `sys.stdlib_module_names` reflects the running interpreter only, so
# without this a 3.13 runner reports `annotationlib` (3.14) as third-party, and
# any Python-2 compatibility shim reports `urllib2` and friends.
#
# Measured on the 30-repo corpus: compatibility shims and version-straddling
# imports were a large share of import warnings, and every one was noise.
_CROSS_VERSION_STDLIB = {
    # Python 2 names, still present in compat shims.
    "urllib2", "urlparse", "StringIO", "cStringIO", "cPickle", "ConfigParser",
    "Queue", "SocketServer", "HTMLParser", "httplib", "commands", "copy_reg",
    "thread", "dummy_thread", "dummy_threading", "exceptions", "md5", "sets",
    "whichdb", "anydbm", "izip", "repr", "Tkinter", "tkFileDialog", "cookielib",
    "Cookie", "xmlrpclib", "BaseHTTPServer", "SimpleHTTPServer", "CGIHTTPServer",
    "robotparser", "UserDict", "UserList", "UserString", "new", "types2",
    # Removed from the stdlib in 3.12/3.13 but still imported by older code.
    "distutils", "imp", "asynchat", "asyncore", "smtpd", "cgi", "cgitb",
    "telnetlib", "nntplib", "spwd", "crypt", "nis", "ossaudiodev", "audioop",
    "aifc", "sunau", "chunk", "sndhdr", "imghdr", "mailcap", "msilib", "pipes",
    "uu", "xdrlib", "lib2to3", "binhex", "formatter",
    # Added after 3.11; a older runner would not know them.
    "tomllib", "annotationlib", "compression", "zoneinfo", "graphlib",
}


@dataclass(frozen=True)
class ImportSite:
    """One imported top-level module, and where it appeared."""

    module: str
    lineno: int
    end_lineno: int
    file: str

    @property
    def lines(self) -> range:
        return range(self.lineno, self.end_lineno + 1)


def is_stdlib(module: str) -> bool:
    """True for modules shipped with the interpreter."""
    return module in _STDLIB or module in _ALWAYS_IGNORE or module in _CROSS_VERSION_STDLIB


def is_private_module(module: str) -> bool:
    """True for `_typeshed`, `_pytest` and friends."""
    return module.startswith("_")


def extract_imports(source: str, filename: str = "<unknown>") -> list[ImportSite]:
    """Parse `source` and return every top-level module it imports."""
    # `ast.parse` emits SyntaxWarning for things like invalid escape sequences
    # in string literals. Those are the source file's business, not ours, and
    # letting them through means scanning a repo with test fixtures floods the
    # user's terminal with warnings about code we merely read.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError, RecursionError):
        return []

    sites: list[ImportSite] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                sites.append(
                    ImportSite(
                        module=top,
                        lineno=node.lineno,
                        end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
                        file=filename,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            # `level > 0` is a relative import (`from . import x`), which is
            # first-party by construction and can never be a PyPI package.
            if node.level and node.level > 0:
                continue
            if not node.module:
                continue
            top = node.module.split(".", 1)[0]
            sites.append(
                ImportSite(
                    module=top,
                    lineno=node.lineno,
                    end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
                    file=filename,
                )
            )
    return sites


def extract_third_party(source: str, filename: str = "<unknown>") -> list[ImportSite]:
    """Imports excluding the standard library and package-private modules."""
    return [
        site
        for site in extract_imports(source, filename)
        if not is_stdlib(site.module) and not is_private_module(site.module)
    ]


def extract_from_file(path: Path) -> list[ImportSite]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return extract_third_party(source, str(path))


def is_python_file(path: str) -> bool:
    return path.endswith((".py", ".pyi"))
