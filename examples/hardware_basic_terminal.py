"""
Basic Terminal Mode usage against a real KAM-XL: connect, read a few
common parameters, do a typed round-trip write, and confirm read-only
protection. Requires a KAM-XL wired to a serial port.

Edit PORT below to match your setup (e.g. "/dev/ttyUSB0" on Linux).
"""

from kamxl import KAMXL, KAMError


PORT = "COM8"


def main():
    kam = KAMXL(PORT)

    try:
        print(f"Connecting to KAM-XL on {PORT}...")
        kam.connect()

        print()
        print("Common parameters")
        print("------------------")
        print("VERSION :", kam.get_typed("VERSION"))
        print("MYCALL  :", kam.get_typed("MYCALL"))
        print("HBAUD   :", kam.get_typed("HBAUD"))
        print("MONITOR :", kam.get_typed("MONITOR"))
        print("PORT    :", kam.get_typed("PORT"))

        print()
        print("Typed write/restore round-trip (MONITOR)")
        print("-----------------------------------------")

        original_monitor = kam.get_typed("MONITOR")
        print("Original:", original_monitor)

        try:
            changed = kam.set_typed("MONITOR", (False, True))
            print("Changed :", changed)
        finally:
            # Always restore, even if something above raised.
            restored = kam.set_typed("MONITOR", original_monitor)
            print("Restored:", restored)

        print()
        print("Read-only protection (VERSION)")
        print("-------------------------------")
        try:
            kam.set_typed("VERSION", "TEST")
        except KAMError as exc:
            print("Correctly rejected:", exc)

    except KAMError as exc:
        print()
        print("KAM-XL error:", exc)

    finally:
        kam.disconnect()


if __name__ == "__main__":
    main()
