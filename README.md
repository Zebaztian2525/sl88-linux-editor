# sl88-linux-editor
A native Linux editor for the Studiologic SL88 Studio MIDI keyboard, reverse-engineered via SysEx.

The protocol and CLI are fully functional. The GUI (sl88_gui.py) is partially complete and needs a developer who enjoys Tkinter to finish the layout and connect the remaining controls. Contributions are very welcome!

Features: Read and display all 250 programs. Rename programs. Change velocity curves (soft, medium, hard, memory4-9). Map joystick axes (Stick 1-3, X/Y) to Pitch Bend, Aftertouch, or CC messages. Save changes to the keyboard's memory. Dark mode support.

Note: The graphical user interface (GUI) is in Swedish (e.g., "Anslut" means "Connect" and "Visa" means "View").

<img width="1918" height="1078" alt="image" src="https://github.com/user-attachments/assets/70fbe4da-9b7e-4826-8624-e46e391b3382" />

How to install and run:
You do not need to be a programmer to use this. Just follow these steps in a terminal on your Linux computer.

Step 1: Install the required packages. Open a terminal and copy-paste this command:
```
sudo apt update
sudo apt install python3 python3-venv python3-tk build-essential libasound2-dev
```
Step 2: Download the files. Download the three Python files from this GitHub page (sl88_protocol.py, sl88_editor.py, and sl88_gui.py). Put them in a folder on your computer, for example ~/sl88-editor. Run this in the terminal:
```
mkdir ~/sl88-editor
cd ~/sl88-editor
# Now move or copy the three files into this folder.
```
Step 3: Create a virtual environment and install MIDI libraries. Run these commands one by one:
```
python3 -m venv ~/sl88env
source ~/sl88env/bin/activate
pip install mido python-rtmidi
```
Step 4: Run the editor. Make sure your SL88 is connected via USB. Then run:
```
python3 sl88_gui.py
```
A window will open. Choose your port (e.g., SL STUDIO MIDI 2), click Anslut (Connect), and you are ready to edit your keyboard! Note: The dark mode can be toggled from the "Visa" menu.

You don't have to close your DAW (like Bitwig) to use this editor! Because the SL88 has two separate MIDI ports, you can keep making music while tweaking your keyboard settings.
Just make sure to **start your DAW first**, and then select **"SL STUDIO MIDI 2"** (the second port) in the editor's port list. This way, your DAW uses the first port for playing notes, while the editor uses the second port for SysEx communication. They work perfectly side-by-side!

Status: The protocol and CLI are fully functional. The GUI (sl88_gui.py) is complete but always open for improvements. Contributions are very welcome!

Disclaimer: This is a reverse-engineered project. Use at your own risk and always back up your programs first.

License: MIT License
