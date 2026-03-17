#!/bin/bash
# Creates Email Assistant.app in ~/Applications so it can be pinned to the Dock.
# Run once: bash create_dock_app.sh

APP_NAME="Email Assistant"
APP_PATH="$HOME/Applications/$APP_NAME.app"
PROJECT="$HOME/Projects/email-assistant"

APPLESCRIPT="on run
    set projectPath to \"$PROJECT\"
    set portInUse to do shell script \"lsof -Pi :5001 -sTCP:LISTEN -t 2>/dev/null | wc -l | tr -d ' '\"
    if portInUse is \"0\" then
        do shell script \"cd \" & projectPath & \" && source .venv/bin/activate && nohup python src/app.py > /tmp/email-assistant.log 2>&1 &\"
        delay 2
    end if
    open location \"http://localhost:5001\"
end run"

mkdir -p "$HOME/Applications"
osacompile -o "$APP_PATH" -e "$APPLESCRIPT"

echo ""
echo "✓ Created: $APP_PATH"
echo ""
echo "To pin to your Dock:"
echo "  1. Open Finder → Go menu → Home → Applications"
echo "  2. Drag 'Email Assistant' to your Dock"
echo ""
echo "Each click will start the server (if not running) and open http://localhost:5001"
