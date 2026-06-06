# SeeNav Backend

This is a standalone backend for the AIUI smart-glasses navigation demo. It is a separate Git project and is ignored by the parent AIUI app repo.

## Local Run

```powershell
py .\server.py
```

Default server:

```text
http://127.0.0.1:8787
```

Use it from the AIUI page by opening:

```text
pages/index/index?apiBase=http%3A%2F%2F127.0.0.1%3A8787
```

For a real glasses device, replace `127.0.0.1` with the LAN address reachable by the device.

## Railway Quick Deploy

This repo is ready for Railway quick deploy. Railway uses:

- `railway.json` for Nixpacks, the start command, and `/health` checks
- `runtime.txt` to pin Python 3.12
- `Procfile` as a fallback process declaration
- `requirements.txt` to mark this as a Python service with no external packages

Deploy steps:

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) and login
3. Click **New Project** -> **Deploy from GitHub repo**
4. Select this repository
5. Railway will auto-detect Python and run `python server.py`

If you deploy from a monorepo, set Railway's root directory to `seenav-backend`.

Optional environment variables:

```text
SEENAV_PROVIDER=mock
VISION_MODEL_BASE_URL=https://api.example.com/v1
VISION_MODEL_API_KEY=replace-me
VISION_MODEL_NAME=vision-model-name
ANTHROPIC_BASE_URL=https://newapi.zenmb.com
ANTHROPIC_API_KEY=replace-me
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929-thinking
ANTHROPIC_AUTH_HEADER=both
ANTHROPIC_TRUST_ENV_PROXY=false
```

After deploy, you'll get a public URL like `https://seenav-backend.up.railway.app`

Use it in AIUI:

```text
pages/index/index?apiBase=https%3A%2F%2Fseenav-backend.up.railway.app
```

## API

### Health

```http
GET /health
```

### Locate

```http
POST /api/visual-nav/locate
```

Request:

```json
{
  "sessionId": "demo",
  "destination": "B1 C区 C18",
  "imageBase64": "...",
  "mimeType": "image/jpeg",
  "size": 123456,
  "mapBase64": "...",
  "mapMimeType": "image/jpeg",
  "mapSize": 456789,
  "mapCapturedAt": 1717560000000,
  "mapIMU": {
    "accelerometer": { "x": 0.01, "y": 0.02, "z": 9.8, "timestamp": 1717560000000, "hasReading": true },
    "gyroscope": { "x": 0.0, "y": 0.0, "z": 0.0, "timestamp": 1717560000000, "hasReading": true },
    "orientation": {
      "quaternion": [0, 0, 0.7071, 0.7071],
      "euler": { "yawDegrees": 90, "pitchDegrees": 0, "rollDegrees": 0 },
      "timestamp": 1717560000000,
      "hasReading": true
    },
    "headingDegrees": 90,
    "timestamp": 1717560000000,
    "hasReading": true
  },
  "imu": {
    "accelerometer": { "x": 0.03, "y": 0.01, "z": 9.7, "timestamp": 1717560010000, "hasReading": true },
    "gyroscope": { "x": 0.0, "y": 0.0, "z": 0.12, "timestamp": 1717560010000, "hasReading": true },
    "orientation": {
      "quaternion": [0, 0, 0.866, 0.5],
      "euler": { "yawDegrees": 120, "pitchDegrees": 0, "rollDegrees": 0 },
      "timestamp": 1717560010000,
      "hasReading": true
    },
    "headingDegrees": 120,
    "mapRelativeYawDegrees": 30,
    "timestamp": 1717560010000,
    "hasReading": true
  },
  "routeContext": {
    "phase": "navigating",
    "startSource": "parking_map",
    "visualFocus": "parking_area_color",
    "orientationReference": "map_capture_imu"
  },
  "scenario": "parking"
}
```

Response:

```json
{
  "routeState": "已定位",
  "routeClass": "route-state route-ok",
  "frameMeta": "实拍帧 01",
  "currentPlace": "B1 C区电梯口外侧",
  "orientation": "面向 C12-C16 柱号方向",
  "landmarks": ["C区标牌", "柱号 C12", "电梯厅"],
  "nextAction": "沿当前方向直行，看到 C16 柱后右转。",
  "confidence": 82,
  "progress": 25,
  "activeStep": 2,
  "scanButtonText": "继续校准",
  "mapProvided": true
}
```

### Reset Session

```http
POST /api/visual-nav/reset
```

Request:

```json
{ "sessionId": "demo" }
```

## Model Mode

By default, the backend runs in deterministic `mock` mode, which is safest for competition demos.

To call an OpenAI-compatible vision model endpoint, set:

```powershell
$env:SEENAV_PROVIDER="openai_compatible"
$env:VISION_MODEL_BASE_URL="https://api.example.com/v1"
$env:VISION_MODEL_API_KEY="..."
$env:VISION_MODEL_NAME="..."
py .\server.py
```

The endpoint must accept `/chat/completions` style requests with image content.

To call an Anthropic-compatible vision model endpoint, set:

```powershell
$env:SEENAV_PROVIDER="anthropic_compatible"
$env:ANTHROPIC_BASE_URL="https://newapi.zenmb.com"
$env:ANTHROPIC_API_KEY="..."
$env:ANTHROPIC_MODEL="claude-sonnet-4-5-20250929-thinking"
$env:ANTHROPIC_AUTH_HEADER="both"
$env:ANTHROPIC_TRUST_ENV_PROXY="false"
py .\server.py
```

The Anthropic-compatible endpoint is called at `/v1/messages`. The request sends both the parking map and the current glasses frame as base64 image blocks. `ANTHROPIC_AUTH_HEADER=both` sends both `x-api-key` and `Authorization: Bearer ...`, which is useful for proxy services; use `x-api-key` or `authorization` if your provider requires one specific header. `ANTHROPIC_TRUST_ENV_PROXY=false` ignores local `HTTP_PROXY` / `HTTPS_PROXY` variables, which is useful when a local proxy breaks Python TLS requests.

## Design

The backend keeps per-session state in memory:

- last route step
- last recognized place
- route history
- whether the user appears to be off route

It returns landmark-style walking guidance rather than car-style directions.
