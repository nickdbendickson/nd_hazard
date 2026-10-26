#!/bin/bash
# hecras_wine_run.sh <model folder>: run a Runner HEC-RAS export in HEC-RAS 6.6 under Wine.
#   1. RAS terrain from Terrain/*.tif (RasProcess.exe CreateTerrain), added to the .rasmap
#   2. Ras.exe -c <prj> <p01>: geometry, preprocessing, unsteady solve, post-processing
# Needs WINEPREFIX with HEC-RAS 6.6 and .NET 4.8 (winetricks dotnet48) and an X display.
# Ras.exe needs quoted Windows paths, so it is started from a .bat (wine's own argv quoting
# drops the quotes and Ras.exe opens its command-line help window instead).
set -e
D=$(cd "$1" && pwd); cd "$D"
export WINEPREFIX=${WINEPREFIX:-$HOME/pfx64} DISPLAY=${DISPLAY:-:77} WINEDEBUG=-all
R="$WINEPREFIX/drive_c/Program Files (x86)/HEC/HEC-RAS/6.6"
PRJ=$(ls *.prj | head -1); NAME=${PRJ%.prj}
TIF=$(ls Terrain/*.tif | head -1); T=$(basename "$TIF" .tif)
U=$(grep -q 'SI Units' "$PRJ" && echo Meters || echo Feet)
wine "$R/RasProcess.exe" CreateTerrain units=$U stitch=true "prj=$(winepath -w GIS/Projection.prj)" \
  "out=$(winepath -w "Terrain/$T.hdf")" "$(winepath -w "$TIF")" | tail -1
python3 - "$NAME.rasmap" "$T" <<'PY'
import sys
p, t = sys.argv[1], sys.argv[2]
s = open(p, newline='').read()
layer = ('<Terrains Checked="True" Expanded="True">\r\n    <Layer Name="%s" Type="TerrainLayer" '
         'Checked="True" Filename=".\\Terrain\\%s.hdf" />\r\n  </Terrains>' % (t, t))
if '<Terrains />' in s:
    open(p, 'w', newline='').write(s.replace('<Terrains />', layer))
PY
BAT="$D/_run_ras.bat"
printf 'cd /d "C:\\Program Files (x86)\\HEC\\HEC-RAS\\6.6"\r\nRas.exe -c "%s" "%s"\r\n' \
  "$(winepath -w "$D/$PRJ")" "$(winepath -w "$D/$NAME.p01")" > "$BAT"
# The first launch in a new prefix shows HEC's Terms and Conditions; this accepts them ("I agree",
# then Next, at their positions on a 1280x1024 screen). Remembered after a clean exit.
( for i in $(seq 1 60); do
    if xdotool search --name 'Terms and Conditions for Use' >/dev/null 2>&1; then
      sleep 2; xdotool mousemove 489 673 click 1; sleep 1; xdotool mousemove 818 714 click 1; break; fi
    sleep 2; done ) &
s=$(date +%s)
wine cmd /c "$(winepath -w "$BAT")" > ras_c.log 2>&1 || true
wineserver -w || true
echo "Ras.exe -c took $(( $(date +%s)-s )) s"
