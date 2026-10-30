"""Hide the mouse pointer on the machine's touchscreen. Run on the Pi by
tools/pi_host_setup.sh.

The kiosk compositor (cage) draws its own pointer from the "default"
Xcursor theme and ignores XCURSOR_THEME, so this writes an "invisible"
theme (every common cursor name is one transparent pixel) and points
~/.icons/default at it."""
import os
import struct

ICONS = os.path.expanduser("~/.icons")
ROOT = os.path.join(ICONS, "invisible", "cursors")
os.makedirs(ROOT, exist_ok=True)

IMAGE = 0xFFFD0002
size = 24
w = h = 1
header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
toc = struct.pack("<III", IMAGE, size, 16 + 12)
chunk = struct.pack("<IIIIIIIII", 36, IMAGE, size, 1, w, h, 0, 0, 0)
pixels = struct.pack("<I", 0x00000000)          # fully transparent ARGB
data = header + toc + chunk + pixels

names = ["default", "left_ptr", "pointer", "hand1", "hand2", "text", "xterm",
         "arrow", "top_left_arrow", "grab", "grabbing", "move", "crosshair",
         "wait", "watch", "progress", "not-allowed", "help", "context-menu",
         "col-resize", "row-resize", "all-scroll", "cell", "copy", "alias",
         "no-drop", "vertical-text", "zoom-in", "zoom-out", "ew-resize",
         "ns-resize", "nesw-resize", "nwse-resize", "n-resize", "s-resize",
         "e-resize", "w-resize", "ne-resize", "nw-resize", "se-resize", "sw-resize"]
with open(os.path.join(ROOT, "default"), "wb") as f:
    f.write(data)
for n in names[1:]:
    p = os.path.join(ROOT, n)
    if os.path.lexists(p):
        os.remove(p)
    os.symlink("default", p)
with open(os.path.join(ICONS, "invisible", "index.theme"), "w") as f:
    f.write("[Icon Theme]\nName=invisible\nComment=No visible pointer (kiosk)\n")

# cage loads "default", whatever XCURSOR_THEME says
dflt = os.path.join(ICONS, "default")
os.makedirs(dflt, exist_ok=True)
link = os.path.join(dflt, "cursors")
if os.path.lexists(link):
    os.remove(link)
os.symlink("../invisible/cursors", link)
with open(os.path.join(dflt, "index.theme"), "w") as f:
    f.write("[Icon Theme]\nName=default\nComment=kiosk: no visible pointer\n"
            "Inherits=invisible\n")
print("   cursor: hidden on the touchscreen")
