"""
Structured packet monitoring against a real KAM-XL, using monitor()'s
generator form. Requires a KAM-XL wired to a serial port; prints
whatever unsolicited traffic it happens to hear.

Edit PORT and SECONDS below for your setup. MONITOR is turned on for
port 2 only and restored to its original setting on exit -- see
docs/troubleshooting.md if you're not seeing much traffic.
"""

from kamxl import KAMXL, KAMError


PORT = "COM8"
SECONDS = 300


def main():
    kam = KAMXL(PORT)
    original_monitor = None

    try:
        print(f"Connecting to KAM-XL on {PORT}...")
        kam.connect()

        original_monitor = kam.get_typed("MONITOR")
        kam.set_multiport_bool("MONITOR", 2, True)

        print(f"Monitoring port 2 for {SECONDS}s (Ctrl-C to stop early)...")
        print()

        for packet in kam.monitor(seconds=SECONDS):
            print(f"{packet.source} -> {packet.destination} "
                  f"(port {packet.port}): {packet.payload!r}")

    except KeyboardInterrupt:
        print()
        print("Stopped (Ctrl-C).")

    except KAMError as exc:
        print()
        print("KAM-XL error:", exc)

    finally:
        if original_monitor is not None:
            print()
            print("Restoring MONITOR...")

            try:
                kam.set_typed("MONITOR", original_monitor)
            except Exception as exc:
                print("Could not restore MONITOR:", exc)

        kam.disconnect()


if __name__ == "__main__":
    main()
