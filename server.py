from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
MAP_PATH = ROOT / "data" / "parking_map.json"
HOST = os.environ.get("SEENAV_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("SEENAV_PORT", "8787"))
PROVIDER = os.environ.get("SEENAV_PROVIDER", "mock").strip().lower()
MODEL_BASE_URL = os.environ.get("VISION_MODEL_BASE_URL", "").rstrip("/")
MODEL_API_KEY = os.environ.get("VISION_MODEL_API_KEY", "")
MODEL_NAME = os.environ.get("VISION_MODEL_NAME", "")

SESSIONS: dict[str, dict[str, Any]] = {}


def load_map() -> dict[str, Any]:
    with MAP_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


PARKING_MAP = load_map()


DEMO_FRAMES = [
    {
        "routeState": "已定位",
        "routeClass": "route-state route-ok",
        "frameMeta": "实拍帧 01 · B1 停车场",
        "currentPlace": "B1 C区电梯口外侧",
        "orientation": "面向 C12-C16 柱号方向",
        "landmarks": ["C区标牌", "电梯厅", "柱号 C12", "出口箭头"],
        "nextAction": "沿当前方向直行，看到 C16 柱后准备右转。",
        "confidence": 82,
        "progress": 25,
        "activeStep": 2,
        "scanButtonText": "继续校准",
    },
    {
        "routeState": "方向正确",
        "routeClass": "route-state route-ok",
        "frameMeta": "实拍帧 02 · C16 柱前",
        "currentPlace": "C16 柱前主通道",
        "orientation": "面对 C18 支路入口",
        "landmarks": ["柱号 C16", "C18箭头", "白色车道线", "限速牌"],
        "nextAction": "在 C16 柱后右转，进入右侧车位排。",
        "confidence": 88,
        "progress": 55,
        "activeStep": 3,
        "scanButtonText": "右转后校准",
    },
    {
        "routeState": "接近目标",
        "routeClass": "route-state route-warn",
        "frameMeta": "实拍帧 03 · C18 车位排",
        "currentPlace": "C18 车位排前方",
        "orientation": "目标在右前方第二个车位",
        "landmarks": ["C18标线", "消防栓", "灰色SUV", "柱号 C18"],
        "nextAction": "继续前进 8 到 12 米，C18 在右侧第二个车位。",
        "confidence": 91,
        "progress": 82,
        "activeStep": 4,
        "scanButtonText": "确认到达",
    },
    {
        "routeState": "已到达",
        "routeClass": "route-state route-done",
        "frameMeta": "实拍帧 04 · 目标车位",
        "currentPlace": "B1 C区 C18",
        "orientation": "目的地位于右侧",
        "landmarks": ["车位 C18", "目标车辆", "柱号 C18", "C区标牌"],
        "nextAction": "已到达目的地，停止导航。",
        "confidence": 96,
        "progress": 100,
        "activeStep": 4,
        "scanButtonText": "重新校准",
    },
]

DEVIATION_FRAME = {
    "routeState": "偏离路线",
    "routeClass": "route-state route-off",
    "frameMeta": "偏航帧 · D区入口",
    "currentPlace": "B1 D区通道口",
    "orientation": "背离 C18 方向",
    "landmarks": ["D区标牌", "出口箭头", "柱号 D03", "收费处"],
    "nextAction": "你已走到 D区，请向左回到 C区标牌，再寻找 C16 柱。",
    "confidence": 74,
    "progress": 42,
    "activeStep": 2,
    "scanButtonText": "重新定位",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def get_session(session_id: str) -> dict[str, Any]:
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "sessionId": session_id,
            "frameIndex": 0,
            "history": [],
            "createdAt": now_ms(),
        }
    return SESSIONS[session_id]


def normalize_session_id(payload: dict[str, Any]) -> str:
    value = str(payload.get("sessionId") or payload.get("session_id") or "demo").strip()
    return value[:64] or "demo"


def safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def clamp_percent(value: Any, fallback: int) -> int:
    number = safe_int(value, fallback)
    return max(0, min(100, number))


def validate_result(result: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    merged = {**fallback, **{k: v for k, v in result.items() if v not in (None, "")}}
    landmarks = merged.get("landmarks")
    if not isinstance(landmarks, list) or not landmarks:
        merged["landmarks"] = fallback["landmarks"]
    merged["confidence"] = clamp_percent(merged.get("confidence"), fallback["confidence"])
    merged["progress"] = clamp_percent(merged.get("progress"), fallback["progress"])
    merged["activeStep"] = max(0, min(4, safe_int(merged.get("activeStep"), fallback["activeStep"])))
    return merged


def infer_demo_frame(session: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        [
            str(payload.get("destination", "")),
            str(payload.get("debugText", "")),
            str(payload.get("recognizedText", "")),
        ]
    ).upper()
    if "D区" in text or "D03" in text or "偏航" in text:
        return dict(DEVIATION_FRAME)

    index = session["frameIndex"] % len(DEMO_FRAMES)
    return dict(DEMO_FRAMES[index])


def call_vision_model(payload: dict[str, Any], session: dict[str, Any]) -> dict[str, Any] | None:
    if PROVIDER != "openai_compatible":
        return None
    if not MODEL_BASE_URL or not MODEL_API_KEY or not MODEL_NAME:
        return None

    image_base64 = payload.get("imageBase64") or ""
    mime_type = payload.get("mimeType") or "image/jpeg"
    if not image_base64:
        return None

    prompt = {
        "task": "Analyze a smart-glasses navigation frame for pedestrian landmark navigation.",
        "destination": payload.get("destination", "B1 C区 C18"),
        "scenario": payload.get("scenario", "parking"),
        "semanticMap": PARKING_MAP,
        "history": session.get("history", [])[-4:],
        "responseContract": {
            "routeState": "已定位 | 方向正确 | 接近目标 | 已到达 | 偏离路线",
            "routeClass": "route-state route-ok | route-state route-warn | route-state route-done | route-state route-off",
            "frameMeta": "short label",
            "currentPlace": "where the user is",
            "orientation": "where the user is facing",
            "landmarks": ["visible landmark names"],
            "nextAction": "one concise Chinese walking instruction based on visible landmarks",
            "confidence": "0-100 integer",
            "progress": "0-100 integer",
            "activeStep": "0-4 integer",
            "scanButtonText": "short Chinese label",
        },
    }
    request_body = {
        "model": MODEL_NAME,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You return only strict JSON for a landmark-based smart-glasses navigation UI.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                    },
                ],
            },
        ],
    }

    url = f"{MODEL_BASE_URL}/chat/completions"
    data = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {MODEL_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    try:
        content = response_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return parse_model_json(content)


def parse_model_json(content: str) -> dict[str, Any] | None:
    if not content:
        return None
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def locate(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = normalize_session_id(payload)
    session = get_session(session_id)
    fallback = infer_demo_frame(session, payload)
    model_result = call_vision_model(payload, session)
    result = validate_result(model_result or {}, fallback)
    result["sessionId"] = session_id
    result["provider"] = PROVIDER
    result["timestamp"] = now_ms()

    session["frameIndex"] += 1
    session["lastResult"] = result
    session["history"].append(
        {
            "timestamp": result["timestamp"],
            "currentPlace": result["currentPlace"],
            "orientation": result["orientation"],
            "routeState": result["routeState"],
            "landmarks": result["landmarks"],
            "progress": result["progress"],
        }
    )
    session["history"] = session["history"][-12:]
    return result


class SeeNavHandler(BaseHTTPRequestHandler):
    server_version = "SeeNavBackend/1.0"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.add_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self.write_json(
                {
                    "ok": True,
                    "provider": PROVIDER,
                    "modelConfigured": bool(MODEL_BASE_URL and MODEL_API_KEY and MODEL_NAME),
                    "sessions": len(SESSIONS),
                }
            )
            return
        self.write_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        payload = self.read_json()
        if path == "/api/visual-nav/locate":
            self.write_json(locate(payload))
            return
        if path == "/api/visual-nav/reset":
            session_id = normalize_session_id(payload)
            SESSIONS.pop(session_id, None)
            self.write_json({"ok": True, "sessionId": session_id})
            return
        self.write_json({"error": "not found"}, status=404)

    def read_json(self) -> dict[str, Any]:
        length = safe_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def write_json(self, value: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.add_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def add_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} {format % args}")


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), SeeNavHandler)
    print(f"SeeNav backend running at http://{HOST}:{PORT}")
    print(f"Provider: {PROVIDER}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping SeeNav backend")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
