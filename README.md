# sl88-linux-editor

A native Linux editor for the **Studiologic SL88 Studio** MIDI keyboard, built by reverse-engineering the keyboard's SysEx protocol.

The official SL Editor only exists for Windows and macOS. This project gives Linux users a graphical editor (PyQt5) and a command-line tool for reading and changing programs, zones, velocity curves, sticks and pedals.

<img width="1228" height="986" alt="SL Editor" src="https://github.com/user-attachments/assets/82b15883-9e82-47e3-82bc-e62b2d8606f1" />


> **Status:** The protocol layer, the command-line tool and the main GUI features (programs, zones, MIDI channel/port, velocity curves, sticks) are working on real hardware. Some features are still **unverified** against the keyboard. See [Verification status](#verification-status).

> **The user interface is in English.** (Earlier versions used Tkinter and a Swedish interface. That is no longer the case.)

---

## Features

**Programs**

- Reads and displays all 250 programs from the keyboard (config dump)
- Filter/search the program list by number or name
- Rename programs (14 characters)
- Optional STORE (write to the keyboard's memory) and read-back verification

**Zones (ZONE 1-4)**

- Enable/disable each zone
- Volume per zone
- MIDI port per zone: USB, MIDI OUT 1 or MIDI OUT 2
- MIDI channel per zone
- Lowest/highest key (key range)
- Overview table (ZONE 1-4 tab) showing all raw zone values, with note names and curve names
- Keyboard display showing which keys belong to which zone

**Velocity curves (SL CURVES)**

- Choose soft, medium, hard or USER 1-6 per zone
- View all nine curves from the keyboard
- Edit USER 1-6 by dragging points, and save them directly to the keyboard

**Sticks and pedals**

- STICK 1-3, X and Y, per zone: Off, Pitch Bend, Aftertouch or any CC 1-127
- PEDAL 1-4 per zone: Off, Damper, Sostenuto, Soft Pedal or any CC 1-127

**Organize and SL Touch**

- ORGANIZE: view programs and groups, local project files (JSON), SYNC IN / SYNC OUT
- KEY-BALANCE: sensitivity editor (+/-30 %) per key, with 12-point and freehand modes and 10 local memory slots

**Other**

- Dark mode (View -> Dark Mode, or Ctrl+D). The choice is remembered.
- Dry Run mode: nothing is sent to the keyboard, useful for trying things out
- Log window showing every SysEx message and status message
- All MIDI communication runs in a background thread, so the window never freezes
- Command-line tool (`sl88_editor.py`) for scripting and experiments

---

## Verification status

This is a reverse-engineered project. Not everything has been confirmed against real hardware.

| Area                                          | Status                                                       |
| --------------------------------------------- | ------------------------------------------------------------ |
| Config dump, program names, rename, STORE     | Verified                                                     |
| Volume, velocity curve selection, USER curves | Verified / working                                           |
| MIDI port and ENABLE per zone                 | Working                                                      |
| STICK parameters (CC N is stored as N+2)      | Working in tests                                             |
| MIDI channel, key range                       | Read correctly, writing is locked behind "Allow unverified fields" |
| PEDALS (offsets 208-223, CC N stored as N+4)  | **Unverified**                                               |
| ORGANIZE groups (SYNC OUT)                    | **Unverified**                                               |
| KEY-BALANCE (read from / save to keyboard)    | **Unverified, not implemented yet.** The editor and local memory slots work, but nothing is sent to the keyboard. |

Treat anything marked unverified as experimental.

---

## Requirements

- Linux (developed and tested on Deepin 25.2, which is Debian-based)
- Python 3
- A Studiologic SL88 Studio connected via USB
- Python packages: `mido`, `python-rtmidi`, `PyQt5`

---

## Installation

### Alternative: Install via prebuilt .deb package (Recommended)

If you are on a Debian/Ubuntu-based distribution (like Deepin), you can install the prebuilt `.deb` package directly:

```bash
sudo apt install ./sl88-editor_1.0.2_all.deb
```



For other Linux distributions, follow the steps below in a terminal.

### Step 1: Install system packages

On Debian, Ubuntu and derivatives:

```bash
sudo apt update
sudo apt install git python3 python3-venv python3-dev build-essential libasound2-dev libjack-dev
```

These are needed to build `python-rtmidi`. On other distributions, install the equivalent packages (Python 3, venv, a C++ compiler, ALSA and JACK development headers).

### Step 2: Download the project

```bash
git clone https://github.com/Zebaztian2525/sl88-linux-editor.git ~/sl88-editor
cd ~/sl88-editor
```

Alternatively, download the files manually and put them in one folder. All four Python files must be in the same folder:

- `sl88_protocol.py` (protocol and logic)
- `sl88_editor.py` (MIDI communication and command-line tool)
- `sl88_extras.py` (pedals, key-balance, groups)
- `sl88_gui.py` (PyQt5 interface, starts the program)

### Step 3: Create a virtual environment and install the Python packages

```bash
python3 -m venv ~/sl88env
source ~/sl88env/bin/activate
pip install mido python-rtmidi PyQt5
```

You need to run `source ~/sl88env/bin/activate` again in every new terminal before starting the editor.

---

## Running the editor

Connect the SL88 via USB, then:

```bash
cd ~/sl88-editor
source ~/sl88env/bin/activate
python3 sl88_gui.py
```

1. Choose the MIDI port, normally **SL STUDIO MIDI 2** (click *Refresh Ports* if the list is empty).
2. Click **Connect**. The editor reads all program names from the keyboard.
3. Select a program in the list on the left.
4. Make changes in the tabs and click **Apply Zone** (or the relevant Apply button).
5. Leave **STORE** checked to save changes in the keyboard's memory, or uncheck it to change only the temporary (RAM) program. Check **Verify / read back** to confirm what the keyboard actually received.

### Using it together with a DAW

You can use the editor and your DAW at the same time, but the order matters:

1.  **Start the editor first** and connect to **SL STUDIO MIDI 2**.
2.  **Then start your DAW.** It will use the first MIDI port for playing notes.
3.  If the DAW cannot see the keyboard (because the editor grabbed the port first), click **Disconnect** in the editor and then **Connect** again. The DAW should now find the keyboard.

*(Note: The editor uses the second MIDI port for SysEx, while the DAW uses the first port for notes. The exact behaviour may depend on your DAW and ALSA setup.)*

### About the MIDI port setting per zone

Each zone sends its notes to one of three outputs: USB, MIDI OUT 1 or MIDI OUT 2 (the two DIN connectors on the back). Many factory programs send zones 3 and 4 to the DIN outputs, so those zones will be silent in your computer or DAW. Set the zone's MIDI port to **USB** and click **Apply Zone** to hear it over USB.

### Safe experimenting

- Tick **Dry Run** to see what would be sent without sending anything.
- Zone channel and key range are locked until you tick **Allow unverified fields**.
- Try changes on a spare program first.

---

## Command-line usage

Everything is also available without the GUI. Changes are a dry run (nothing is sent) unless you add `--send`.

```bash
# List MIDI ports
python3 sl88_editor.py --list

# Check the files without any hardware
python3 sl88_editor.py --check

# Read all programs and curves
python3 sl88_editor.py --port "SL STUDIO MIDI 2" --dump

# Read and print program 2
python3 sl88_editor.py --port "SL STUDIO MIDI 2" --read 2 --send

# Rename program 250, store it, and read it back
python3 sl88_editor.py --port "SL STUDIO MIDI 2" --rename 250 "TEST NAME" --send --store --verify

# Set the velocity curve for zone 1
python3 sl88_editor.py --port "SL STUDIO MIDI 2" --program 250 --curve soft --zone 1 --send

# Set MIDI port for zone 3 to USB (0 = USB, 1 = MIDI OUT 1, 2 = MIDI OUT 2)
python3 sl88_editor.py --port "SL STUDIO MIDI 2" --program 250 --field midi_port 3 0 --send --store
```

---

## Troubleshooting

- **No MIDI ports listed:** check the USB cable and run `aseqdump -l` to see whether Linux sees the keyboard.
- **`pip install python-rtmidi` fails:** the system packages from Step 1 are missing.
- **The Qt window does not start:** on some systems you also need `sudo apt install libxcb-cursor0`.
- **A zone is silent:** check that the zone is enabled, that its MIDI port is USB, and that your DAW or synth listens on the zone's MIDI channel.

---

## Disclaimer

This is a reverse-engineered project and is **not affiliated with or endorsed by Studiologic or Fatar**. The SysEx protocol was worked out from public documentation and from observing a real SL88 Studio, so it may behave differently on other firmware versions.

**Use at your own risk.** Always back up your programs before changing anything, and try new features on a spare program first. The authors are not responsible for lost settings or damaged hardware.

---

## Credits

Protocol documentation: [maranite/SL-Monitor](https://github.com/maranite/SL-Monitor).

## Contributing

Bug reports, hardware test results and pull requests are welcome, especially for confirming the unverified features (pedals, groups, key-balance).

## License

MIT License. See the `LICENSE` file for the full text.
