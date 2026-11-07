#!/bin/sh
# Install the launcher, the desktop entry and the icons for the current user.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

python3 "$here/make_icon.py" >/dev/null

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
cat > "$HOME/.local/bin/trd" <<LAUNCH
#!/bin/sh
exec /usr/bin/python3 "$here/trd_client.py" "\$@"
LAUNCH
chmod 755 "$HOME/.local/bin/trd"

cat > "$HOME/.local/share/applications/trd.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=TRD - Text Remote Desktop
GenericName=Text Remote Desktop
Comment=Monocell desktop over SSH
Exec=$HOME/.local/bin/trd
Icon=trd
Terminal=false
Categories=Network;RemoteAccess;
Keywords=remote;desktop;monocell;ssh;terminal;text;
StartupWMClass=trd_client.py
DESK
command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 >/dev/null 2>&1 || true
echo "installed: ~/.local/bin/trd and the desktop entry (icon 'trd')"
