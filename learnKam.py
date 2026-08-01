from kamxl import KAMXL, KAMError


PORT = "COM8"


def main():
    kam = KAMXL(PORT)

    try:
        print(f"Connecting to KAM-XL on {PORT}...")
        kam.connect()

        print()
        print("KAM-XL information")
        print("------------------")
        print("VERSION :", kam.get_typed("VERSION"))
        print("MYCALL  :", kam.get_typed("MYCALL"))
        print("HBAUD   :", kam.get_typed("HBAUD"))
        print("MONITOR :", kam.get_typed("MONITOR"))
        print("PORT    :", kam.get_typed("PORT"))

        print()
        print("Typed write test")
        print("----------------")

        original_monitor = kam.get_typed("MONITOR")

        print("Original MONITOR:", original_monitor)

        try:
            result = kam.set_typed(
                "MONITOR",
                (False, True)
            )

            print("Changed MONITOR :", result)

        finally:
            # Make sure MONITOR is restored even if something above
            # (or the print itself) raises before we get to it.
            result = kam.set_typed(
                "MONITOR",
                original_monitor
            )

            print("Restored MONITOR:", result)

        print()
        print("Read-only protection test")
        print("-------------------------")

        try:
            kam.set_typed(
                "VERSION",
                "TEST"
            )
        except KAMError as exc:
            print("Correctly caught:", exc)

        print()
        print("All basic tests completed.")

    except KAMError as exc:
        print()
        print("KAM-XL error:", exc)

    except Exception as exc:
        print()
        print("Unexpected error:", exc)

    finally:
        kam.disconnect()


if __name__ == "__main__":
    main()
