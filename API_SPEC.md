# Arduino Sensor System - API Specification

## 개요

Arduino 기반 센서 시스템의 REST API 및 WebSocket 명세입니다.  
실시간 센서 데이터 수신, 제어 상태 조회, 데이터 세그먼트 관리 기능을 제공합니다.

**Base URL**: `http://localhost:8000`

---

## 📡 REST API Endpoints

### 1. Health Check

#### `GET /`

서버 상태 및 API 정보를 반환합니다.

**Response** (Mock Server):
```json
{
  "title": "Arduino Sensor System Mock Server",
  "status": "running",
  "mode": "simulation",
  "endpoints": {
    "/sensor/latest": "최근 센서 데이터",
    "/environment": "현재 환경 상태 (실제값)",
    "/control": "현재 제어 상태",
    "/reconciler/state": "Reconciler 상태",
    "/environment/set": "환경 변수 설정 (POST)"
  }
}
```

**Response** (Real Server):
```json
{
  "message": "Sensor Data API",
  "status": "running"
}
```

---

### 2. Sensor Data

#### `GET /sensor/latest`

최근 수신한 센서 데이터를 반환합니다.

**Response**: `200 OK`
```json
{
  "moisture": 45,
  "temp_inner": 22,
  "humd_inner": 60,
  "temp_outer": 25,
  "humd_outer": 70,
  "illumination": 500.5
}
```

**Response**: `404 Not Found` (데이터가 아직 없을 때)
```json
{
  "error": "No sensor data available yet"
}
```

**Data Model: SensorReport**
| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `moisture` | `int` | % | 토양 습도 (0-100) |
| `temp_inner` | `int` | °C | 내부 온도 |
| `humd_inner` | `int` | % | 내부 습도 (0-100) |
| `temp_outer` | `int` | °C | 외부 온도 |
| `humd_outer` | `int` | % | 외부 습도 (0-100) |
| `illumination` | `float` \| `null` | lux | 조도 (빛의 밝기) |

---

### 3. Reconciler State

#### `GET /reconciler/state`

현재 제어 시스템(Reconciler)의 상태, 설정, 튜닝 파라미터를 반환합니다.

**Response**: `200 OK`
```json
{
  "state": {
    "temp_ema": [22.0, 22.1, 22.2],
    "moisture_ema": [45.0, 45.5, 46.0],
    "integral_moisture": 0.5,
    "last_moisture_error": -5.0,
    "integral_temp": 0.2,
    "last_temp_error": 2.0,
    "filt_moist": 45.5,
    "filt_temp": 22.1,
    "der_moist": 0.1,
    "der_temp": 0.05
  },
  "config": {
    "moisture_range": [20, 60],
    "target_inner_temp": 20
  },
  "tune": {
    "pump_Kp": 10.0,
    "pump_Ki": 0.1,
    "pump_Kd": 0.1,
    "peltier_Kp": 30.0,
    "peltier_Ki": 0.02,
    "peltier_Kd": 0.005,
    "temp_ema_alpha": 0.1,
    "temp_ema_count": 13,
    "moisture_ema_alpha": 0.1,
    "moisture_ema_count": 13,
    "temp_deadband": 1.0,
    "moisture_deadband": 1.0,
    "cutoff": 64,
    "der_tau": 1.0,
    "aw_limit": 2.0
  }
}
```

**Data Models**:

**ReconcilerConfig**
| Field | Type | Description |
|-------|------|-------------|
| `moisture_range` | `[int, int]` | 목표 습도 범위 [최소, 최대] (%) |
| `target_inner_temp` | `int` | 목표 내부 온도 (°C) |

**ReconcilerTune**
| Field | Type | Description |
|-------|------|-------------|
| `pump_Kp` | `float` | 펌프 제어 비례 게인 |
| `pump_Ki` | `float` | 펌프 제어 적분 게인 |
| `pump_Kd` | `float` | 펌프 제어 미분 게인 |
| `peltier_Kp` | `float` | 펠티어 제어 비례 게인 |
| `peltier_Ki` | `float` | 펠티어 제어 적분 게인 |
| `peltier_Kd` | `float` | 펠티어 제어 미분 게인 |
| `temp_ema_alpha` | `float` | 온도 EMA 필터 알파 (0-1) |
| `temp_ema_count` | `int` | 온도 초기 SMA 샘플 수 |
| `moisture_ema_alpha` | `float` | 습도 EMA 필터 알파 (0-1) |
| `moisture_ema_count` | `int` | 습도 초기 SMA 샘플 수 |
| `temp_deadband` | `float` | 온도 데드밴드 (°C) |
| `moisture_deadband` | `float` | 습도 데드밴드 (%) |
| `cutoff` | `int` | 액추에이터 컷오프 레벨 |
| `der_tau` | `float` | 미분 저역 통과 필터 시상수 (초) |
| `aw_limit` | `float` | 적분 항 안티-와인드업 제한 |

**ReconcilerState**
| Field | Type | Description |
|-------|------|-------------|
| `temp_ema` | `float[]` | 온도 EMA 버퍼 |
| `moisture_ema` | `float[]` | 습도 EMA 버퍼 |
| `integral_moisture` | `float` | 습도 PID 적분 항 |
| `last_moisture_error` | `float` | 이전 습도 오차 |
| `integral_temp` | `float` | 온도 PID 적분 항 |
| `last_temp_error` | `float` | 이전 온도 오차 |
| `filt_moist` | `float \| null` | 필터링된 습도 |
| `filt_temp` | `float \| null` | 필터링된 온도 |
| `der_moist` | `float` | 습도 미분 항 |
| `der_temp` | `float` | 온도 미분 항 |

---

### 4. Data Segments

#### `GET /segments?n={count}`

저장된 데이터 세그먼트 목록을 반환합니다.

**Query Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `n` | `int` | 50 | 반환할 세그먼트 개수 |

**Response**: `200 OK`
```json
{
  "segments": [
    "segment_2025-10-28_14-30-00.json",
    "segment_2025-10-28_14-00-00.json",
    "segment_2025-10-28_13-30-00.json"
  ]
}
```

---

## 🔧 Mock Server Only Endpoints

### 5. Environment State (Mock Only)

#### `GET /environment`

시뮬레이션 환경의 실제 물리 상태를 반환합니다. (Mock Server 전용)

**Response**: `200 OK`
```json
{
  "environment": {
    "moisture": 45.3,
    "temp_inner": 22.1,
    "humd_inner": 60.2,
    "temp_outer": 25.0,
    "humd_outer": 70.0,
    "illumination": 500.5
  },
  "control": {
    "pump_level": 512,
    "peltier_level": 256,
    "peltier_forward": 1,
    "fan_level": 1023
  }
}
```

**Data Model: Environment**
| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `moisture` | `float` | % | 실제 토양 습도 |
| `temp_inner` | `float` | °C | 실제 내부 온도 |
| `humd_inner` | `float` | % | 실제 내부 습도 |
| `temp_outer` | `float` | °C | 실제 외부 온도 |
| `humd_outer` | `float` | % | 실제 외부 습도 |
| `illumination` | `float` | lux | 실제 조도 |

**Data Model: Control State**
| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `pump_level` | `int` | 0-1023 | 펌프 출력 레벨 |
| `peltier_level` | `int` | 0-1023 | 펠티어 출력 레벨 |
| `peltier_forward` | `int` | 0 or 1 | 펠티어 방향 (0: 가열, 1: 냉각) |
| `fan_level` | `int` | 0-1023 | 팬 출력 레벨 |

---

### 6. Control State (Mock Only)

#### `GET /control`

현재 제어 상태와 마지막 제어 명령을 반환합니다. (Mock Server 전용)

**Response**: `200 OK`
```json
{
  "control": {
    "pump_level": 512,
    "peltier_level": 256,
    "peltier_forward": 1,
    "fan_level": 1023
  },
  "last_command": {
    "pump_level": 512,
    "peltier_level": 256,
    "peltier_forward": 1
  }
}
```

---

### 7. Set Environment (Mock Only)

#### `POST /environment/set?key={key}&value={value}`

시뮬레이션 환경 변수를 설정합니다. (테스트용, Mock Server 전용)

**Query Parameters**:
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `key` | `string` | Yes | 환경 변수 키 |
| `value` | `float` | Yes | 설정할 값 |

**Valid Keys**:
- `moisture` - 토양 습도 (%)
- `temp_inner` - 내부 온도 (°C)
- `humd_inner` - 내부 습도 (%)
- `temp_outer` - 외부 온도 (°C)
- `humd_outer` - 외부 습도 (%)
- `illumination` - 조도 (lux)

**Example Request**:
```
POST /environment/set?key=moisture&value=30.0
```

**Response**: `200 OK`
```json
{
  "success": true,
  "key": "moisture",
  "value": 30.0,
  "environment": {
    "moisture": 30.0,
    "temp_inner": 22.1,
    "humd_inner": 60.2,
    "temp_outer": 25.0,
    "humd_outer": 70.0,
    "illumination": 500.5
  }
}
```

**Response**: `400 Bad Request` (잘못된 키)
```json
{
  "error": "Unknown key: invalid_key"
}
```

---

### 8. Set Control (Mock Only)

#### `POST /control/set?pump_level={level}&peltier_level={level}&peltier_forward={forward}`

제어 상태를 직접 설정합니다. (테스트용, Mock Server 전용)

**Query Parameters**:
| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `pump_level` | `int` | 0 | 0-1023 | 펌프 레벨 |
| `peltier_level` | `int` | 0 | 0-1023 | 펠티어 레벨 |
| `peltier_forward` | `int` | 0 | 0 or 1 | 펠티어 방향 |

**Example Request**:
```
POST /control/set?pump_level=512&peltier_level=256&peltier_forward=1
```

**Response**: `200 OK`
```json
{
  "success": true,
  "control": {
    "pump_level": 512,
    "peltier_level": 256,
    "peltier_forward": 1,
    "fan_level": 1023
  }
}
```

**Note**: `fan_level`은 자동으로 설정됩니다:
- `peltier_level >= 64`: `fan_level = 1023`
- `peltier_level < 64`: `fan_level = 0`

---

## 🔌 WebSocket API

### `WS /live`

실시간 센서 데이터와 제어 명령을 스트리밍합니다.

**Connection**: `ws://localhost:8000/live`

**Message Format** (Server → Client):

센서 리포트:
```json
{
  "report": {
    "moisture": 45,
    "temp_inner": 22,
    "humd_inner": 60,
    "temp_outer": 25,
    "humd_outer": 70,
    "illumination": 500.5
  }
}
```

제어 명령:
```json
{
  "command": {
    "pump_level": 512,
    "peltier_level": 256,
    "peltier_forward": 1
  }
}
```

**Update Frequency**: ~1초 간격 (데이터 변경 시)

**Example Usage** (JavaScript):
```javascript
const ws = new WebSocket('ws://localhost:8000/live');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  
  if (data.report) {
    console.log('Sensor Report:', data.report);
  }
  
  if (data.command) {
    console.log('Control Command:', data.command);
  }
};

ws.onerror = (error) => {
  console.error('WebSocket Error:', error);
};

ws.onclose = () => {
  console.log('WebSocket Closed');
};
```

---

## 📊 Data Models Summary

### SensorReport
센서에서 수신한 환경 데이터

```typescript
interface SensorReport {
  moisture: number;        // 0-100 (%)
  temp_inner: number;      // °C
  humd_inner: number;      // 0-100 (%)
  temp_outer: number;      // °C
  humd_outer: number;      // 0-100 (%)
  illumination?: number;   // lux
}
```

### ReconcilerCommand
시스템이 Arduino에 보내는 제어 명령

```typescript
interface ReconcilerCommand {
  pump_level: number;      // 0-1023
  peltier_level: number;   // 0-1023
  peltier_forward: number; // 0 or 1
}
```

### ReconcilerConfig
제어 시스템의 목표 설정

```typescript
interface ReconcilerConfig {
  moisture_range: [number, number];  // [min, max] (%)
  target_inner_temp: number;         // °C
}
```

### ReconcilerTune
PID 제어기 튜닝 파라미터

```typescript
interface ReconcilerTune {
  pump_Kp: number;
  pump_Ki: number;
  pump_Kd: number;
  peltier_Kp: number;
  peltier_Ki: number;
  peltier_Kd: number;
  temp_ema_alpha: number;
  temp_ema_count: number;
  moisture_ema_alpha: number;
  moisture_ema_count: number;
  temp_deadband: number;
  moisture_deadband: number;
  cutoff: number;
  der_tau: number;
  aw_limit: number;
}
```

---

## 🚀 Quick Start

### Real Server (Arduino 연결 필요)
```bash
python main.py
```

### Mock Server (테스트용)
```bash
python mock_server.py
```

### API 테스트
```bash
# 최근 센서 데이터
curl http://localhost:8000/sensor/latest

# Reconciler 상태
curl http://localhost:8000/reconciler/state

# 데이터 세그먼트 목록
curl http://localhost:8000/segments?n=10

# Mock Server: 환경 설정
curl -X POST "http://localhost:8000/environment/set?key=moisture&value=30"

# Mock Server: 제어 설정
curl -X POST "http://localhost:8000/control/set?pump_level=500&peltier_level=300&peltier_forward=1"
```

---

## 📝 Notes

- **Real Server** (`main.py`): Arduino 시리얼 연결 필요, 실제 센서 데이터 사용
- **Mock Server** (`mock_server.py`): Arduino 없이 시뮬레이션, 물리 엔진으로 환경 변화 모델링
- 모든 타임스탬프는 Unix epoch time (초 단위)
- WebSocket은 단방향 (Server → Client)
- 센서 데이터는 약 1초 간격으로 업데이트됨

---

## 🔒 Error Responses

모든 엔드포인트는 다음 형식의 에러 응답을 반환할 수 있습니다:

```json
{
  "error": "Error message description"
}
```

**Common Error Codes**:
- `404 Not Found`: 데이터가 아직 없거나 리소스를 찾을 수 없음
- `400 Bad Request`: 잘못된 요청 파라미터
- `500 Internal Server Error`: 서버 내부 오류

---

**Version**: 1.0.0  
**Last Updated**: 2025-10-28
