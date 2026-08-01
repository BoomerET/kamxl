# Examples

Two kinds, told apart by filename prefix:

- **`offline_*.py`** -- run with no hardware at all. `offline_typed_commands.py`
  uses the same scripted fake serial connection as the offline unit test
  suite (`tests/fakes.py`); `offline_packet_parsing.py` needs nothing but
  `packet.py` and some canned text. Good for getting a feel for the API
  before you have a KAM-XL wired up, or for CI.
- **`hardware_*.py`** -- need a real Kantronics KAM-XL on a serial port.
  Edit the `PORT`/callsign constants at the top of each one before
  running. These follow the same pattern as the `*Test.py` scripts at
  the repo root, just trimmed down and commented for reading rather
  than testing.

Run any of them directly:

```
python3 examples/offline_packet_parsing.py
python3 examples/offline_typed_commands.py
python3 examples/hardware_basic_terminal.py     # needs real hardware
python3 examples/hardware_connect_and_chat.py   # needs real hardware
python3 examples/hardware_monitor_packets.py    # needs real hardware
```

See [`docs/quickstart.md`](../docs/quickstart.md) for the same material
as a walkthrough instead of standalone scripts.
