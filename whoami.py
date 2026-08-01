from kamxl import KAMXL

kam = KAMXL("COM8")

try:
    kam.connect()

    mycall = kam.get_typed("MYCALL")

    print(f"Port 1 MYCALL: {mycall[0]!r}")
    print(f"Port 2 MYCALL: {mycall[1]!r}")

finally:
    kam.disconnect()
