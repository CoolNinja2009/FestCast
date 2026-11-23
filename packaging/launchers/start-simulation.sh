#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
export FESTCAST_SIMULATION=true
export FESTCAST_DATA_DIR="$PWD/data-sim"
echo "SIMULATION MODE: OBS is simulated. Nothing shown is a real camera, programme or recording."
exec ./start.sh
