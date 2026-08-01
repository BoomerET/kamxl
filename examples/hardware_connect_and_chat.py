"""
AX.25 connected-mode session against a real KAM-XL: connect (directly,
or via a digipeater), send a line of text, read whatever comes back,
then disconnect cleanly. Requires a KAM-XL wired to a serial port and
another station to connect to.

Edit PORT, TARGET, and VIA below for your setup. Leave VIA as None
for a direct connect.
"""

from kamxl import KAMXL, KAMError


PORT = "COM8"
TARGET = "KD5EOC-10"
VIA = None          # e.g. "RSSTN", or ["DIGI1", "DIGI2"]


def main():
    kam = KAMXL(PORT)

    try:
        print(f"Connecting to KAM-XL on {PORT}...")
        kam.connect()

        print(f"Attempting AX.25 connect to {TARGET} (via={VIA})...")
        banner = kam.connect_station(TARGET, via=VIA)
        print("Connected:")
        print(banner)

        kam.send_connected("hello from kamxl!")

        print("Waiting for a reply (5s)...")
        reply = kam.read_connected(timeout=5)
        print("Received:", reply or "(nothing)")

    except KAMError as exc:
        print()
        print("KAM-XL error:", exc)

    finally:
        print("Disconnecting...")

        try:
            kam.disconnect_station()
        except KAMError as exc:
            print("Could not cleanly disconnect station:", exc)

        kam.disconnect()


if __name__ == "__main__":
    main()
