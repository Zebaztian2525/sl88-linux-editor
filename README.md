# sl88-linux-editor
A native Linux editor for the Studiologic SL88 Studio MIDI keyboard, reverse-engineered via SysEx.

The protocol and CLI are fully functional. The GUI (sl88_gui.py) is partially complete and needs a developer who enjoys Tkinter to finish the layout and connect the remaining controls. Contributions are very welcome!

<img width="1918" height="1078" alt="image" src="https://github.com/user-attachments/assets/70fbe4da-9b7e-4826-8624-e46e391b3382" />

## 🚀 How to install and run

You do not need to be a programmer to use this. Just follow these steps in a terminal on your Linux computer.

### 1. Install the required packages
Open a terminal and copy-paste this command:

sudo apt update
sudo apt install python3 python3-venv python3-tk build-essential libasound2-dev

2. Download the files
Download the three Python files from this GitHub page (sl88_protocol.py, sl88_editor.py, and sl88_gui.py).
Put them in a folder on your computer, for example ~/sl88-editor.

mkdir ~/sl88-editor
cd ~/sl88-editor
#Now move or copy the three files into this folder.

3. Create a virtual environment and install MIDI libraries
Run these commands one by one:

python3 -m venv ~/sl88env
source ~/sl88env/bin/activate
pip install mido python-rtmidi

4. Run the editor
Make sure your SL88 is connected via USB, and that Bitwig (or any other DAW) is closed so the MIDI port is free. Then run:

python3 sl88_gui.py

A window will open. Choose your port (e.g., SL STUDIO MIDI 2), click Anslut (Connect), and you are ready to edit your keyboard!

Note: The dark mode can be toggled from the "Visa" menu.
