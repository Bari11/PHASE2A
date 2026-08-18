# 🐦 Canary

Real-time stress and posture awareness using computer vision.

## Quick Start

```bash
pip install PyQt5 opencv-python numpy "mediapipe>=0.10.0"
python main.py
```

Voice alerts (optional):
- Linux:   sudo apt install espeak
- macOS:   nothing needed
- Windows: nothing needed

## Controls

| Action | How |
|---|---|
| Open Dashboard | Left-click tray icon |
| Start / Stop session | Right-click tray → Start/Stop |
| Trigger Unplug now (demo) | Right-click → Trigger Unplug Now |
| Debug camera view | python debug_detector_view.py |

## Alert Colors

| Color | Meaning | Cooldown |
|---|---|---|
| 🔴 Red | Posture alert | 30s |
| 🟡 Yellow | Stress alert | 45s |
| 🟢 Green | 10-min good streak | 5 min |
| 🔵 Blue | Hydration reminder | 30 min |

## Unplug Mode

After 3 alerts of the same type, Canary shows a 2-minute warning
banner. When the countdown ends:

1. Wire unplugs — screen dims with spark animation
2. Exercise break screen appears (nature gradient)
3. Cartoon figure demonstrates exercises prescribed for YOUR signals
4. Camera confirms you moved — progress dots fill
5. Wire plugs back in — you're recharged

**For demo:** Right-click tray → "Trigger Unplug Now"

## File Structure

```
canary/
├── main.py
├── debug_detector_view.py
├── requirements.txt
├── core/
│   ├── detector.py          ← CV detection engine
│   └── notifications.py     ← Color-coded alerts
├── ui/
│   ├── tray_app.py          ← Dashboard + Settings + Tray
│   └── unplug/
│       ├── break_screen.py  ← Full Unplug sequence
│       ├── figure.py        ← Animated cartoon figure
│       ├── exercise_data.py ← Exercise library + prescription
│       └── warning_banner.py← 2-min countdown banner
└── data/
    └── settings.json
```

## Settings

- Voice Alerts: On/Off + Female/Male
- Unplug Mode: On/Off
- Exercise Figure: Girl/Boy
