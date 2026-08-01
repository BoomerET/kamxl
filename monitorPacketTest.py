from kamxl import KAMXL, KAMError
from packet import PacketParser


kam = KAMXL("COM8")
parser = PacketParser()

# Set before the try block for the same reason as listenTest.py: lets
# the restore-on-exit logic safely check "did we actually read an
# original value" without risking a NameError if connect() (or the
# first get_typed call) fails.
original = None


def print_packet(packet):
    header = f"{packet.source} -> {packet.destination}"

    if packet.digipeaters:
        header += f" via {','.join(packet.digipeaters)}"

    header += f" (port {packet.port})"

    print(header)

    for line in packet.payload.splitlines():
        print(f"    {line}")

    print()


def on_chunk(text):
    for packet in parser.feed(text):
        print_packet(packet)


try:
    kam.connect()

    original = kam.get_typed("MONITOR")

    print("Original MONITOR:", original)

    kam.set_multiport_bool(
        "MONITOR",
        2,
        True
    )

    print("Listening for 10 minutes...")
    print()

    kam.listen(
        seconds=600,
        callback=on_chunk
    )

except KeyboardInterrupt:
    print()
    print("Stopped listening (Ctrl-C).")

except KAMError as exc:
    print()
    print("KAM-XL error:", exc)

except Exception as exc:
    print()
    print("Unexpected error:", exc)

finally:
    # Anything still buffered when the session ended (no trailing
    # header/newline) counts as a real packet too.
    for packet in parser.flush():
        print_packet(packet)

    if original is not None:
        print()
        print("Restoring MONITOR...")

        try:
            kam.set_typed(
                "MONITOR",
                original
            )
        except Exception as exc:
            print(
                "Could not restore MONITOR:",
                exc
            )

    kam.disconnect()
