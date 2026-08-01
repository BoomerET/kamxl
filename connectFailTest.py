from kamxl import (
    KAMXL,
    KAMError,
    KAMTimeoutError,
    KAMConnectionError,
)


# N0CALL is the traditional dummy/placeholder callsign in ham radio --
# not a real, licensed station -- so this should never actually
# connect. Good for observing what the KAM-XL does when nothing
# answers: BUSY, RETRY COUNT EXCEEDED, or our own timeout firing
# first.
TARGET = "N0CALL-15"

# Generous timeout so the KAM-XL's own AX.25 retry logic has a real
# chance to give up and report "*** RETRY COUNT EXCEEDED" itself,
# instead of us bailing out first with our own KAMTimeoutError.
TIMEOUT = 180


kam = KAMXL("COM8")

try:
    kam.connect()

    print(f"Attempting to connect to {TARGET} (should fail)...")
    print(f"Timeout: {TIMEOUT}s")
    print()

    banner = kam.connect_station(TARGET, timeout=TIMEOUT)

    print("Unexpectedly connected:")
    print(repr(banner))

    print()
    print("Disconnecting, since this wasn't supposed to succeed...")

    try:
        confirmation = kam.disconnect_station()
        print(repr(confirmation))
    except Exception as exc:
        print("Could not cleanly disconnect station:", exc)

except KeyboardInterrupt:
    print()
    print("Stopped by user (Ctrl-C).")

except KAMTimeoutError as exc:
    print()
    print("KAMTimeoutError (our own timeout fired first):")
    print(exc)

except KAMConnectionError as exc:
    print()
    print("KAMConnectionError (KAM-XL itself reported failure):")
    print(exc)

except KAMError as exc:
    print()
    print("KAM-XL error:", exc)

except Exception as exc:
    print()
    print("Unexpected error:", exc)

finally:
    kam.disconnect()
