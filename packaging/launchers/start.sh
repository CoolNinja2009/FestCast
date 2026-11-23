#!/usr/bin/env bash
# FestCast launcher (Linux and macOS). Keep this window open during the event: if FestCast ever crashes it
# restarts itself here. Closing it stops FestCast (OBS keeps recording; the TV output pauses until FestCast is back).
cd "$(dirname "$0")" || exit 1
if [ ! -x python/bin/python3 ]; then echo "python/ is missing: unzip the whole FestCast folder again."; read -r _; exit 1; fi
[ -f .env ] || cp .env.example .env
mkdir -p "${FESTCAST_DATA_DIR:-data}/logs"
echo
echo "  FestCast is starting. The dashboard opens automatically (the address is printed below)."
echo "  Keep this window open during the event. Press Ctrl+C to stop FestCast."
echo
OPENFLAG=--open
trap 'exit 0' INT TERM
while true; do
  python/bin/python3 -E -s -m backend.main $OPENFLAG
  CODE=$?
  OPENFLAG=
  [ "$CODE" = 0 ] && exit 0
  [ "$CODE" -ge 128 ] && exit "$CODE"            # stopped with Ctrl+C / killed on purpose
  echo "$(date) FestCast exited with code $CODE - restarted automatically" >> "${FESTCAST_DATA_DIR:-data}/logs/restarts.log"
  echo
  echo "  FestCast stopped unexpectedly (code $CODE). Restarting in 3 seconds... (Ctrl+C to stop)"
  sleep 3
done
