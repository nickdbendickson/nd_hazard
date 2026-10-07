#!/bin/bash
# Create a conda environment with ANUGA for the ND Dam Breach Runner.
#
#   ./create_anuga_env.sh [env name]
#
# Needs conda or mamba (Miniforge recommended: https://github.com/conda-forge/miniforge).
# The environment only has to be created once; it works offline afterwards.
set -euo pipefail
ENV="${1:-anuga}"

if command -v mamba >/dev/null; then CONDA=mamba
elif command -v conda >/dev/null; then CONDA=conda
else echo "conda/mamba not found. Install Miniforge first."; exit 1; fi

$CONDA create -y -n "$ENV" -c conda-forge python=3.12 anuga numpy scipy matplotlib
PY="$($CONDA run -n "$ENV" python -c 'import sys; print(sys.executable)')"
$CONDA run -n "$ENV" python -c "import anuga; print('ANUGA', anuga.__version__)"
echo
echo "In QGIS: Processing > Options > Providers > ND Dam Breach Runner >"
echo "  Python interpreter of the ANUGA environment = $PY"
