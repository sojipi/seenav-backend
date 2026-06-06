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


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env")

HOST = os.environ.get("SEENAV_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("SEENAV_PORT", "8787"))
PROVIDER = os.environ.get("SEENAV_PROVIDER", "mock").strip().lower()
MODEL_BASE_URL = os.environ.get("VISION_MODEL_BASE_URL", "").rstrip("/")
MODEL_API_KEY = os.environ.get("VISION_MODEL_API_KEY", "")
MODEL_NAME = os.environ.get("VISION_MODEL_NAME", "")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", MODEL_BASE_URL).rstrip("/")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", MODEL_API_KEY)
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", MODEL_NAME)
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
ANTHROPIC_AUTH_HEADER = os.environ.get("ANTHROPIC_AUTH_HEADER", "both").strip().lower()
ANTHROPIC_TRUST_ENV_PROXY = os.environ.get("ANTHROPIC_TRUST_ENV_PROXY", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)

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
        "orientation": "面向地图中绿色 C区车道方向",
        "landmarks": ["地图当前位置", "绿色 C区", "柱号 C12", "出口箭头"],
        "nextAction": "沿绿色 C区车道直行，保持在地图标注的 C区颜色范围内。",
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
        "orientation": "面对地图中 C18 支路入口",
        "landmarks": ["绿色 C区", "柱号 C16", "C18箭头", "白色车道线"],
        "nextAction": "到 C16 后右转，进入地图上通向 C18 的同色车位排。",
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
        "landmarks": ["绿色 C区", "C18标线", "消防栓", "柱号 C18"],
        "nextAction": "继续沿当前车位排前进，按地图颜色确认仍在 C区，C18 在右侧第二个车位。",
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
            "imuHistory": [],
            "createdAt": now_ms(),
        }
    return SESSIONS[session_id]


def remember_parking_map(session: dict[str, Any], payload: dict[str, Any]) -> None:
    map_base64 = str(payload.get("mapBase64") or "")
    if not map_base64:
        return
    previous_map = session.get("parkingMap") if isinstance(session.get("parkingMap"), dict) else {}
    map_size = safe_int(payload.get("mapSize"), 0)
    captured_at = safe_int(payload.get("mapCapturedAt"), now_ms())
    incoming_imu = normalize_imu(payload.get("mapIMU") or payload.get("mapImu"))
    previous_imu = previous_map.get("imu") if isinstance(previous_map.get("imu"), dict) else None
    same_map = previous_map.get("size") == map_size and previous_map.get("capturedAt") == captured_at
    map_imu = incoming_imu
    if same_map and not imu_has_reading(map_imu) and imu_has_reading(previous_imu):
        map_imu = previous_imu
    session["parkingMap"] = {
        "imageBase64": map_base64,
        "mimeType": payload.get("mapMimeType") or "image/jpeg",
        "size": map_size,
        "capturedAt": captured_at,
        "imu": map_imu,
    }


def remember_imu(session: dict[str, Any], payload: dict[str, Any]) -> None:
    imu = normalize_imu(payload.get("imu"))
    if not imu or not imu.get("hasReading"):
        return
    map_imu = (session.get("parkingMap") or {}).get("imu")
    relative_yaw = relative_yaw_degrees(imu, map_imu)
    if relative_yaw is not None:
        imu["mapRelativeYawDegrees"] = relative_yaw
    session["lastIMU"] = imu
    imu_history = session.get("imuHistory", [])
    imu_history.append(session["lastIMU"])
    session["imuHistory"] = imu_history[-12:]


def normalize_session_id(payload: dict[str, Any]) -> str:
    value = str(payload.get("sessionId") or payload.get("session_id") or "demo").strip()
    return value[:64] or "demo"


def safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def safe_float(value: Any, fallback: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return number


def imu_has_reading(imu: Any) -> bool:
    return isinstance(imu, dict) and bool(imu.get("hasReading"))


def normalize_degrees(value: float) -> float:
    normalized = value % 360
    if normalized < 0:
        normalized += 360
    return normalized


def normalize_signed_degrees(value: float) -> float:
    normalized = normalize_degrees(value)
    if normalized > 180:
        normalized -= 360
    return normalized


def normalize_vector(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"x": None, "y": None, "z": None, "timestamp": 0, "hasReading": False}
    x = safe_float(value.get("x"))
    y = safe_float(value.get("y"))
    z = safe_float(value.get("z"))
    has_reading = bool(value.get("hasReading")) and x is not None and y is not None and z is not None
    return {
        "x": x,
        "y": y,
        "z": z,
        "timestamp": safe_int(value.get("timestamp"), 0),
        "hasReading": has_reading,
    }


def normalize_quaternion(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) < 4:
        return None
    quaternion: list[float] = []
    for item in value[:4]:
        number = safe_float(item)
        if number is None:
            return None
        quaternion.append(number)
    return quaternion


def normalize_imu(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    accelerometer = normalize_vector(value.get("accelerometer"))
    gyroscope = normalize_vector(value.get("gyroscope"))
    orientation_raw = value.get("orientation") if isinstance(value.get("orientation"), dict) else {}
    euler_raw = orientation_raw.get("euler") if isinstance(orientation_raw.get("euler"), dict) else {}
    compass_raw = value.get("compass") if isinstance(value.get("compass"), dict) else {}
    yaw = safe_float(
        euler_raw.get("yawDegrees"),
        safe_float(
            value.get("headingDegrees"),
            safe_float(compass_raw.get("headingDegrees"), safe_float(compass_raw.get("heading"))),
        ),
    )
    pitch = safe_float(euler_raw.get("pitchDegrees"))
    roll = safe_float(euler_raw.get("rollDegrees"))
    quaternion = normalize_quaternion(orientation_raw.get("quaternion"))
    orientation_has_reading = bool(
        quaternion is not None or
        yaw is not None or
        orientation_raw.get("hasReading")
    ) and (quaternion is not None or yaw is not None)
    heading_degrees = normalize_degrees(yaw) if yaw is not None else None
    normalized = {
        "accelerometer": accelerometer,
        "gyroscope": gyroscope,
        "orientation": {
            "quaternion": quaternion,
            "euler": {
                "yawDegrees": heading_degrees,
                "pitchDegrees": pitch,
                "rollDegrees": roll,
            },
            "timestamp": safe_int(orientation_raw.get("timestamp"), 0),
            "hasReading": orientation_has_reading,
        },
        "headingDegrees": heading_degrees,
        "timestamp": safe_int(value.get("timestamp"), now_ms()),
    }
    normalized["hasReading"] = bool(
        accelerometer["hasReading"] or
        gyroscope["hasReading"] or
        orientation_has_reading
    )
    map_relative = safe_float(value.get("mapRelativeYawDegrees"))
    if map_relative is not None:
        normalized["mapRelativeYawDegrees"] = normalize_signed_degrees(map_relative)
    return normalized


def relative_yaw_degrees(current_imu: dict[str, Any] | None, map_imu: dict[str, Any] | None) -> float | None:
    if not current_imu or not map_imu:
        return None
    current_heading = current_imu.get("headingDegrees")
    map_heading = map_imu.get("headingDegrees")
    if not isinstance(current_heading, (int, float)) or not isinstance(map_heading, (int, float)):
        return None
    return normalize_signed_degrees(float(current_heading) - float(map_heading))


def vector_magnitude(vector: Any) -> float | None:
    if not isinstance(vector, dict) or not vector.get("hasReading"):
        return None
    x = vector.get("x")
    y = vector.get("y")
    z = vector.get("z")
    if not all(isinstance(item, (int, float)) for item in (x, y, z)):
        return None
    return float((x * x + y * y + z * z) ** 0.5)


def infer_motion_state(imu: dict[str, Any] | None) -> dict[str, Any]:
    if not imu_has_reading(imu):
        return {"state": "unknown", "description": "No usable motion reading."}

    gyro_magnitude = vector_magnitude(imu.get("gyroscope"))
    accel_magnitude = vector_magnitude(imu.get("accelerometer"))
    gyro_z = (imu.get("gyroscope") or {}).get("z")

    if gyro_magnitude is not None and gyro_magnitude >= 0.35:
        return {
            "state": "turning",
            "description": "The glasses appear to be rotating.",
            "gyroscopeMagnitude": round(gyro_magnitude, 3),
            "gyroZ": round(float(gyro_z), 3) if isinstance(gyro_z, (int, float)) else None,
        }
    if accel_magnitude is not None and abs(accel_magnitude - 9.81) >= 1.2:
        return {
            "state": "moving",
            "description": "The wearer appears to be walking or changing speed.",
            "accelerometerMagnitude": round(accel_magnitude, 3),
        }
    return {
        "state": "steady",
        "description": "The glasses appear mostly steady.",
        "accelerometerMagnitude": round(accel_magnitude, 3) if accel_magnitude is not None else None,
        "gyroscopeMagnitude": round(gyro_magnitude, 3) if gyro_magnitude is not None else None,
    }


def build_imu_assessment(session: dict[str, Any]) -> dict[str, Any]:
    parking_map = session.get("parkingMap") if isinstance(session.get("parkingMap"), dict) else {}
    map_imu = parking_map.get("imu") if isinstance(parking_map.get("imu"), dict) else None
    current_imu = session.get("lastIMU") if isinstance(session.get("lastIMU"), dict) else None
    assessment: dict[str, Any] = {
        "provided": imu_has_reading(current_imu),
        "mapBaselineProvided": imu_has_reading(map_imu),
        "canCompareToMap": False,
        "motion": infer_motion_state(current_imu),
    }
    if not current_imu:
        return assessment

    relative_yaw = safe_float(current_imu.get("mapRelativeYawDegrees"))
    if relative_yaw is None:
        relative_yaw = relative_yaw_degrees(current_imu, map_imu)
    if relative_yaw is None:
        return assessment

    relative_yaw = normalize_signed_degrees(relative_yaw)
    absolute_yaw = abs(relative_yaw)
    if absolute_yaw <= 25:
        alignment = "aligned"
        description = "相对地图拍摄方向基本一致"
    elif absolute_yaw <= 65:
        alignment = "slight_turn"
        description = f"相对地图拍摄方向{turn_text(relative_yaw)}约 {round(absolute_yaw)}°"
    elif absolute_yaw <= 135:
        alignment = "turned"
        description = f"相对地图拍摄方向已{turn_text(relative_yaw)}约 {round(absolute_yaw)}°"
    else:
        alignment = "opposite"
        description = f"相对地图拍摄方向接近反向，已{turn_text(relative_yaw)}约 {round(absolute_yaw)}°"

    assessment.update(
        {
            "canCompareToMap": True,
            "mapRelativeYawDegrees": round(relative_yaw, 1),
            "absoluteDeltaDegrees": round(absolute_yaw, 1),
            "turnDirection": "right" if relative_yaw > 0 else "left" if relative_yaw < 0 else "straight",
            "alignment": alignment,
            "description": description,
        }
    )
    return assessment


def turn_text(relative_yaw: float) -> str:
    if relative_yaw > 0:
        return "向右转"
    if relative_yaw < 0:
        return "向左转"
    return "偏转"


def append_orientation_note(orientation: Any, note: str) -> str:
    base = str(orientation or "").strip()
    if not base:
        return note
    if note in base:
        return base
    return f"{base}（IMU：{note}）"


def prepend_instruction(next_action: Any, prefix: str) -> str:
    action = str(next_action or "").strip()
    if action.startswith(prefix):
        return action
    if not action:
        return prefix
    return f"{prefix}；{action}"


def apply_imu_assessment(result: dict[str, Any], assessment: dict[str, Any], model_used: bool) -> dict[str, Any]:
    result["imuAssessment"] = assessment
    if not assessment.get("canCompareToMap"):
        return result

    result["orientation"] = append_orientation_note(result.get("orientation"), assessment["description"])

    route_class = str(result.get("routeClass") or "")
    is_terminal = "route-done" in route_class or "route-off" in route_class
    alignment = assessment.get("alignment")
    if is_terminal:
        return result

    if alignment == "opposite":
        result["routeState"] = "方向待确认"
        result["routeClass"] = "route-state route-warn"
        result["confidence"] = min(clamp_percent(result.get("confidence"), 60), 68)
        result["nextAction"] = prepend_instruction(
            result.get("nextAction"),
            "IMU 显示你已接近背向地图拍摄方向，先停下确认前方地标和地图箭头",
        )
        result["scanButtonText"] = "重新校准"
        return result

    if alignment == "turned" and not model_used:
        result["routeState"] = "方向待确认"
        result["routeClass"] = "route-state route-warn"
        result["confidence"] = min(clamp_percent(result.get("confidence"), 70), 78)
        result["nextAction"] = prepend_instruction(
            result.get("nextAction"),
            "IMU 显示已经明显转向，请用前方柱号、箭头和分区颜色确认这是计划路线",
        )
    return result


def clamp_unit(value: Any, fallback: float) -> float:
    number = safe_float(value, fallback)
    if number is None:
        number = fallback
    return max(0.0, min(1.0, float(number)))


def normalize_nav_map(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_nodes = value.get("nodes")
    if not isinstance(raw_nodes, list):
        return None

    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_node in enumerate(raw_nodes[:12]):
        if not isinstance(raw_node, dict):
            continue
        node_id = str(raw_node.get("id") or f"node_{index}").strip()[:32]
        if not node_id or node_id in seen_ids:
            node_id = f"node_{index}"
        seen_ids.add(node_id)
        label = str(raw_node.get("label") or node_id).strip()[:24]
        nodes.append(
            {
                "id": node_id,
                "label": label,
                "x": clamp_unit(raw_node.get("x"), 0.1 + index * 0.12),
                "y": clamp_unit(raw_node.get("y"), 0.5),
                "type": str(raw_node.get("type") or "landmark")[:24],
            }
        )

    if len(nodes) < 2:
        return None

    node_ids = {node["id"] for node in nodes}
    edges: list[dict[str, str]] = []
    raw_edges = value.get("edges")
    if isinstance(raw_edges, list):
        for raw_edge in raw_edges[:18]:
            start_id = None
            end_id = None
            if isinstance(raw_edge, dict):
                start_id = raw_edge.get("from")
                end_id = raw_edge.get("to")
            elif isinstance(raw_edge, list) and len(raw_edge) >= 2:
                start_id = raw_edge[0]
                end_id = raw_edge[1]
            start = str(start_id or "").strip()
            end = str(end_id or "").strip()
            if start in node_ids and end in node_ids and start != end:
                edges.append({"from": start, "to": end})
    if not edges:
        edges = [
            {"from": nodes[index]["id"], "to": nodes[index + 1]["id"]}
            for index in range(len(nodes) - 1)
        ]

    raw_route = value.get("route")
    route = [str(item).strip() for item in raw_route] if isinstance(raw_route, list) else []
    route = [item for item in route if item in node_ids]
    if len(route) < 2:
        route = [node["id"] for node in nodes]

    current_node_id = str(value.get("currentNodeId") or value.get("current") or route[0]).strip()
    target_node_id = str(value.get("targetNodeId") or value.get("target") or route[-1]).strip()
    if current_node_id not in node_ids:
        current_node_id = route[0]
    if target_node_id not in node_ids:
        target_node_id = route[-1]

    return {
        "version": 1,
        "source": str(value.get("source") or "recognized_map")[:32],
        "title": str(value.get("title") or "路线图").strip()[:24],
        "nodes": nodes,
        "edges": edges,
        "route": route,
        "currentNodeId": current_node_id,
        "targetNodeId": target_node_id,
    }


def demo_nav_map(destination: str) -> dict[str, Any]:
    target = destination or "目标点"
    return {
        "version": 1,
        "source": "fallback_demo",
        "title": "地标路线",
        "nodes": [
            {"id": "start", "label": "当前位置", "x": 0.12, "y": 0.62, "type": "current"},
            {"id": "landmark_1", "label": "主通道", "x": 0.34, "y": 0.48, "type": "landmark"},
            {"id": "turn", "label": "转向点", "x": 0.58, "y": 0.48, "type": "turn"},
            {"id": "target", "label": target[:18], "x": 0.84, "y": 0.34, "type": "target"},
        ],
        "edges": [
            {"from": "start", "to": "landmark_1"},
            {"from": "landmark_1", "to": "turn"},
            {"from": "turn", "to": "target"},
        ],
        "route": ["start", "landmark_1", "turn", "target"],
        "currentNodeId": "start",
        "targetNodeId": "target",
    }


def build_nav_map(result: dict[str, Any], session: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    nav_map = normalize_nav_map(result.get("navMap"))
    if nav_map:
        session["navMap"] = nav_map
        return nav_map

    current = session.get("navMap")
    if isinstance(current, dict):
        return current

    destination = str(payload.get("destination") or "").strip()
    nav_map = demo_nav_map(destination)
    session["navMap"] = nav_map
    return nav_map


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


def build_navigation_prompt(payload: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    parking_map = session.get("parkingMap") or {}
    destination = str(payload.get("destination") or "").strip() or "未知目标车位"
    map_provided = bool(parking_map.get("imageBase64"))
    map_imu = parking_map.get("imu") or {}
    last_imu = session.get("lastIMU") or {}
    imu_assessment = build_imu_assessment(session)
    prompt = {
        "task": "Analyze smart-glasses parking navigation using a supplied parking map, the current camera frame, and IMU sensor data.",
        "destination": destination,
        "scenario": payload.get("scenario", "parking"),
        "destinationRules": [
            "The user's requested destination is the exact destination string. Do not replace it with examples or prior demo targets.",
            "Never route to B1 C区 C18 unless the user explicitly requested C18 or the supplied parking map clearly shows that as the route.",
            "Do not instruct the user to change floor unless the supplied parking map visibly requires a floor change for the requested destination.",
            "If the requested destination is not visible on the current parking map, ask for the correct map or guide toward the matching area shown on the map instead of inventing a different floor or zone.",
        ],
        "parkingMapContext": {
            "provided": map_provided,
            "mapSize": parking_map.get("size", 0),
            "mapMimeType": parking_map.get("mimeType", ""),
            "capturedIMU": map_imu,
            "orientationReference": "The capturedIMU is the glasses orientation when the parking map image was captured. Treat it as the baseline for comparing later camera-frame orientations.",
            "meaning": "The parking map image is authoritative. It contains the user's current starting position, target parking zones, and area colors.",
        },
        "imuContext": {
            "provided": bool(last_imu),
            "current": last_imu,
            "recentHistory": session.get("imuHistory", [])[-4:],
            "mapRelativeYawDegrees": last_imu.get("mapRelativeYawDegrees"),
            "assessment": imu_assessment,
            "meaning": "IMU sensor data from the smart glasses. accelerometer is device acceleration, gyroscope is rotation rate, orientation.quaternion/euler yaw is glasses orientation, and mapRelativeYawDegrees is the signed yaw delta from the map-capture baseline to the current frame. Use the relative yaw together with visible map arrows, labels, lanes, and landmarks to decide whether the user is facing the intended map direction.",
        },
        "routeContext": payload.get("routeContext") or {},
        "history": session.get("history", [])[-4:],
        "guidanceRules": [
            "Use the parking map as the route reference and starting-position source.",
            "Use visible parking-area colors from the camera frame as the main localization cue.",
            "Cross-check area color, parking-zone labels, arrows, lane direction, and numbered spaces before saying the user arrived.",
            "Do not mark arrived unless the target parking space or an immediate target-side landmark is visible.",
            "Use mapRelativeYawDegrees to determine how much the user has turned since the parking map was captured.",
            "Use accelerometer and gyroscope data to detect walking, turning, and standing still. Adjust guidance if the user appears to be turning or has stopped.",
            "When map-relative yaw changes between frames, update the orientation description and next action to reflect the new facing direction.",
        ],
        "responseContract": {
            "routeState": "已定位 | 方向正确 | 方向待确认 | 接近目标 | 已到达 | 偏离路线",
            "routeClass": "route-state route-ok | route-state route-warn | route-state route-done | route-state route-off",
            "frameMeta": "short label",
            "currentPlace": "where the user is",
            "orientation": "where the user is facing (incorporate map-relative yaw/IMU orientation when available)",
            "landmarks": ["visible landmark names"],
            "nextAction": "one concise Chinese walking instruction based on visible landmarks and IMU-detected movement state",
            "confidence": "0-100 integer",
            "progress": "0-100 integer",
            "activeStep": "0-4 integer",
            "scanButtonText": "short Chinese label",
            "navMap": {
                "title": "short map title",
                "source": "recognized_map",
                "nodes": [
                    {
                        "id": "stable node id",
                        "label": "visible landmark or destination label",
                        "x": "0-1 normalized horizontal coordinate in the map image",
                        "y": "0-1 normalized vertical coordinate in the map image",
                        "type": "current | landmark | turn | target",
                    }
                ],
                "edges": [{"from": "node id", "to": "node id"}],
                "route": ["ordered node ids from current position to destination"],
                "currentNodeId": "current/start node id",
                "targetNodeId": "destination node id",
            },
        },
    }
    if map_provided:
        prompt["semanticMapPolicy"] = "Do not use the built-in demo B1/C18 semantic map. Use only the supplied parking map image, current frame, history, and requested destination. If the supplied map is visible, extract a compact navMap graph for canvas rendering with normalized coordinates."
    else:
        prompt["semanticMap"] = PARKING_MAP
        prompt["semanticMapPolicy"] = "This built-in map is only a fallback demo map and must not override the user's requested destination."
    return prompt


def get_frame_image(payload: dict[str, Any]) -> tuple[str, str] | None:
    image_base64 = payload.get("imageBase64") or ""
    mime_type = payload.get("mimeType") or "image/jpeg"
    if not image_base64:
        return None
    return str(image_base64), str(mime_type)


def is_same_image(left: Any, right: Any) -> bool:
    left_text = str(left or "")
    right_text = str(right or "")
    return bool(left_text and right_text and left_text == right_text)


def call_vision_model(payload: dict[str, Any], session: dict[str, Any]) -> dict[str, Any] | None:
    if PROVIDER == "openai_compatible":
        return call_openai_compatible_model(payload, session)
    if PROVIDER == "anthropic_compatible":
        return call_anthropic_compatible_model(payload, session)
    return None


def call_openai_compatible_model(payload: dict[str, Any], session: dict[str, Any]) -> dict[str, Any] | None:
    if not MODEL_BASE_URL or not MODEL_API_KEY or not MODEL_NAME:
        return None
    frame_image = get_frame_image(payload)
    if not frame_image:
        return None
    image_base64, mime_type = frame_image
    parking_map = session.get("parkingMap") or {}
    skip_duplicate_frame = is_same_image(image_base64, parking_map.get("imageBase64"))
    prompt = build_navigation_prompt(payload, session)
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)}
    ]
    if parking_map.get("imageBase64"):
        user_content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{parking_map.get('mimeType', 'image/jpeg')};base64,{parking_map['imageBase64']}"
                },
            }
        )
    if not skip_duplicate_frame:
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
            }
        )
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
                "content": user_content,
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


def anthropic_messages_url() -> str:
    if ANTHROPIC_BASE_URL.endswith("/v1"):
        return f"{ANTHROPIC_BASE_URL}/messages"
    return f"{ANTHROPIC_BASE_URL}/v1/messages"


def anthropic_headers() -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
        "User-Agent": "SeeNavBackend/1.0",
    }
    if ANTHROPIC_AUTH_HEADER in ("authorization", "bearer", "both"):
        headers["Authorization"] = f"Bearer {ANTHROPIC_API_KEY}"
    if ANTHROPIC_AUTH_HEADER in ("x-api-key", "api-key", "both"):
        headers["x-api-key"] = ANTHROPIC_API_KEY
    return headers


def open_anthropic_request(request: urllib.request.Request, timeout: int = 30) -> Any:
    if ANTHROPIC_TRUST_ENV_PROXY:
        return urllib.request.urlopen(request, timeout=timeout)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def call_anthropic_compatible_model(payload: dict[str, Any], session: dict[str, Any]) -> dict[str, Any] | None:
    if not ANTHROPIC_BASE_URL or not ANTHROPIC_API_KEY or not ANTHROPIC_MODEL:
        return None
    frame_image = get_frame_image(payload)
    if not frame_image:
        return None
    image_base64, mime_type = frame_image
    parking_map = session.get("parkingMap") or {}
    skip_duplicate_frame = is_same_image(image_base64, parking_map.get("imageBase64"))
    prompt = build_navigation_prompt(payload, session)
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)}
    ]
    if parking_map.get("imageBase64"):
        user_content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": parking_map.get("mimeType", "image/jpeg"),
                    "data": parking_map["imageBase64"],
                },
            }
        )
    if not skip_duplicate_frame:
        user_content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": image_base64,
                },
            }
        )
    request_body: dict[str, Any] = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1200,
        "system": "You return only strict JSON for a landmark-based smart-glasses parking navigation UI.",
        "messages": [
            {
                "role": "user",
                "content": user_content,
            }
        ],
    }
    data = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        anthropic_messages_url(),
        data=data,
        headers=anthropic_headers(),
        method="POST",
    )
    try:
        with open_anthropic_request(request, timeout=30) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    return parse_anthropic_json(response_json)


def parse_anthropic_json(response_json: dict[str, Any]) -> dict[str, Any] | None:
    content = response_json.get("content")
    if isinstance(content, str):
        return parse_model_json(content)
    if not isinstance(content, list):
        return None

    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
    if not texts:
        return None
    return parse_model_json("\n".join(texts))


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


def model_configured() -> bool:
    if PROVIDER == "openai_compatible":
        return bool(MODEL_BASE_URL and MODEL_API_KEY and MODEL_NAME)
    if PROVIDER == "anthropic_compatible":
        return bool(ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY and ANTHROPIC_MODEL)
    return False


def locate(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = normalize_session_id(payload)
    session = get_session(session_id)
    remember_parking_map(session, payload)
    remember_imu(session, payload)
    fallback = infer_demo_frame(session, payload)
    model_result = call_vision_model(payload, session)
    model_used = model_result is not None
    imu_assessment = build_imu_assessment(session)
    result = validate_result(model_result or {}, fallback)
    result = apply_imu_assessment(result, imu_assessment, model_used)
    result["navMap"] = build_nav_map(result, session, payload)
    result["sessionId"] = session_id
    result["provider"] = PROVIDER
    result["destination"] = str(payload.get("destination") or "")
    result["mapProvided"] = bool(session.get("parkingMap", {}).get("imageBase64"))
    result["imuProvided"] = bool(session.get("lastIMU", {}).get("hasReading"))
    parking_map = session.get("parkingMap") if isinstance(session.get("parkingMap"), dict) else {}
    map_imu = parking_map.get("imu") if isinstance(parking_map.get("imu"), dict) else {}
    result["mapIMUProvided"] = imu_has_reading(map_imu)
    if session.get("lastIMU", {}).get("mapRelativeYawDegrees") is not None:
        result["mapRelativeYawDegrees"] = session["lastIMU"]["mapRelativeYawDegrees"]
    result["timestamp"] = now_ms()

    session["frameIndex"] += 1
    session["lastResult"] = result
    history_entry = {
        "timestamp": result["timestamp"],
        "currentPlace": result["currentPlace"],
        "orientation": result["orientation"],
        "routeState": result["routeState"],
        "landmarks": result["landmarks"],
        "progress": result["progress"],
    }
    last_imu = session.get("lastIMU")
    if last_imu:
        history_entry["headingDegrees"] = last_imu.get("headingDegrees")
        history_entry["mapRelativeYawDegrees"] = last_imu.get("mapRelativeYawDegrees")
        history_entry["accelerometer"] = last_imu.get("accelerometer", {})
        history_entry["gyroscope"] = last_imu.get("gyroscope", {})
    session["history"].append(history_entry)
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
                    "modelConfigured": model_configured(),
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
