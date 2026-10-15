"""
Command line: ``cfa-shoc verify cert.json --trust did:key:...``

Prints VALID (exit 0) or the reason verification failed (exit 1).
"""

import argparse
import json
import sys
from typing import List, Optional

from . import __version__


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="cfa-shoc", description="CFA-SHOC tools")
    parser.add_argument("--version", action="version", version=f"cfa-shoc {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify", help="verify a provenance certificate offline")
    verify.add_argument("certificate", help="path to the certificate JSON file")
    verify.add_argument(
        "--trust", action="append", default=[], metavar="DID",
        help="trusted issuer did:key (repeat for several)",
    )

    args = parser.parse_args(argv)
    if args.command == "verify":
        from .provenance import verify_certificate

        try:
            with open(args.certificate, "r", encoding="utf-8") as handle:
                certificate = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"Cannot read certificate: {exc}")
            return 1
        ok, reason = verify_certificate(certificate, set(args.trust))
        print(reason)
        return 0 if ok else 1
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
