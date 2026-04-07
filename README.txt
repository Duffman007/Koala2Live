Koala2Live
==========

USER-EDITABLE FILES (modify these without rebuilding)
  version.txt    — App version shown in About dialog (eg: BETA 0.9)
                   Accepts any text: letters, numbers, spaces
  changelog.txt  — Release notes shown in "View Change Log" window
                   Plain text, any format you like
  icon.png       — App icon source image
                   IDEAL SIZE: 1024 × 1024 px (square)
  background.png — Background image in the app window
                   IDEAL SIZE: 1100 × 520 px (or any image cropped to ~2.1:1 ratio)
  KoalaALS.py    — The conversion script (update this for new script versions)

BUILD (requires Xcode Command Line Tools — free)
  Install once: xcode-select --install
  Then run:     ./build.sh

FIRST RUN
  macOS will block the app (not from App Store).
  Right-click → Open → Open to approve it once.

USAGE
  Drop a .koala backup file onto the window,
  or click to browse. The project is exported
  next to your .koala file.
