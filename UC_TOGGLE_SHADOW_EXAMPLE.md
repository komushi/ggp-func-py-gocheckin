# UC Toggle IoT Device Shadow Examples

## Shadow Structure

The UC toggles are stored in the **IoT device shadow** under `desired` state and synced to the `gocheckin_asset` DynamoDB table.

## Example 1: P2 Camera (Entrance with Lock) - All UCs Enabled

```json
{
  "state": {
    "desired": {
      "hostId": "host-001",
      "assetId": "camera-entrance-001",
      "localIp": "192.168.1.100",
      "category": "CAMERA",
      "isDetecting": true,
      "isRecording": true,
      "locks": {
        "lock-001": {
          "lockName": "Main Entrance",
          "lockType": "keypad"
        }
      },
      "uc_toggles": {
        "enable_uc1_uc2": true,
        "enable_uc3": true,
        "enable_uc4_uc8": true,
        "enable_uc5": true
      }
    }
  }
}
```

**Behavior:**
- UC1: Member unlock enabled
- UC2: Tailgating detection enabled
- UC3: Unknown face logging enabled
- UC4: Group size validation enabled
- UC8: Person detection enabled (gate + continuous + extend)

---

## Example 2: P1 Camera (Hallway Surveillance, No Lock) - UC8 Only

```json
{
  "state": {
    "desired": {
      "hostId": "host-001",
      "assetId": "camera-hallway-002",
      "localIp": "192.168.1.101",
      "category": "CAMERA",
      "isDetecting": true,
      "isRecording": true,
      "locks": {},
      "uc_toggles": {
        "enable_uc3": true,
        "enable_uc8": true
      }
    }
  }
}
```

**Behavior:**
- UC3: Unknown face logging enabled
- UC8: Person detection enabled (gate + continuous + extend)
- UC1+UC2: N/A (no lock)
- UC4+UC8: N/A (no lock, use standalone UC8)

---

## Example 3: P2 Camera - UC1+UC2 Disabled (Surveillance Only)

```json
{
  "state": {
    "desired": {
      "hostId": "host-001",
      "assetId": "camera-entrance-001",
      "localIp": "192.168.1.100",
      "category": "CAMERA",
      "isDetecting": true,
      "isRecording": true,
      "locks": {
        "lock-001": {
          "lockName": "Main Entrance",
          "lockType": "keypad"
        }
      },
      "uc_toggles": {
        "enable_uc1_uc2": false,
        "enable_uc3": true,
        "enable_uc4_uc8": true,
        "enable_uc5": true
      }
    }
  }
}
```

**Behavior:**
- UC1+UC2: **DISABLED** - No face recognition, no unlock, no tailgating detection
- UC4+UC8: Still runs YOLOv8n gate (filters false ONVIF triggers)
- SCRFD+ArcFace: Does NOT run (saves ~35% NPU)
- ONVIF triggers still start session (if UC8 gate passes)

---

## Example 4: P2 Camera - UC4+UC8 Disabled (Face Recognition Only)

```json
{
  "state": {
    "desired": {
      "hostId": "host-001",
      "assetId": "camera-entrance-001",
      "localIp": "192.168.1.100",
      "category": "CAMERA",
      "isDetecting": true,
      "isRecording": true,
      "locks": {
        "lock-001": {
          "lockName": "Main Entrance",
          "lockType": "keypad"
        }
      },
      "uc_toggles": {
        "enable_uc1_uc2": true,
        "enable_uc3": true,
        "enable_uc4_uc8": false,
        "enable_uc5": true
      }
    }
  }
}
```

**Behavior:**
- UC4+UC8: **DISABLED** - No YOLOv8n gate, no person counting
- UC1+UC2: Face recognition runs directly on ONVIF trigger
- Session extend: Motion-only (no dual-signal)
- NPU: ~35% (SCRFD+ArcFace only, no YOLOv8n)

---

## Example 5: P1 Camera - All Toggles Disabled (Recording Only)

```json
{
  "state": {
    "desired": {
      "hostId": "host-001",
      "assetId": "camera-hallway-002",
      "localIp": "192.168.1.101",
      "category": "CAMERA",
      "isDetecting": false,
      "isRecording": true,
      "locks": {},
      "uc_toggles": {
        "enable_uc3": false,
        "enable_uc8": false
      }
    }
  }
}
```

**Behavior:**
- Detection disabled - only recording runs
- No NPU usage
- ONVIF triggers ignored for detection

---

## Shadow Update Flow

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌──────────────┐
│   AWS IoT   │────▶│ Device Shadow    │────▶│ gocheckin_asset │────▶│  py_handler  │
│   Console   │     │ (desired state)  │     │   DynamoDB      │     │  (runtime)   │
└─────────────┘     └──────────────────┘     └─────────────────┘     └──────────────┘
     ▲                      │                        │                    │
     │                      │                        │                    │
     │                      ▼                        ▼                    │
     │              Shadow Delta Event      fetch_camera_items()          │
     │              (IoT Rule → Lambda)              │                    │
     │                                               │                    │
     └───────────────────────────────────────────────┴────────────────────┘
                              has_config_changed() triggers restart
```

---

## Lambda Function for Shadow → DynamoDB Sync

```python
# shadow_sync_lambda.py
import boto3
import json

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('gocheckin_asset')

def lambda_handler(event, context):
    """Sync IoT shadow delta to DynamoDB."""
    for change in event.get('changes', []):
        asset_id = change.get('assetId')
        host_id = change.get('hostId')

        # Update UC toggles in asset table
        table.update_item(
            Key={'hostId': host_id, 'assetId': asset_id},
            UpdateExpression='SET uc_toggles = :toggles',
            ExpressionAttributeValues={
                ':toggles': change.get('uc_toggles', {})
            }
        )

    return {'statusCode': 200, 'body': 'Sync complete'}
```

---

## Testing Commands

### Update Shadow via AWS CLI

```bash
# P2 camera - enable all UCs
aws iot-data update-thing-shadow \
  --thing-name "ggp-func-py-gocheckin-001" \
  --payload '{"state":{"desired":{"uc_toggles":{"enable_uc1_uc2":true,"enable_uc3":true,"enable_uc4_uc8":true,"enable_uc5":true}}}}'

# P2 camera - disable UC1+UC2 (no unlock, surveillance only)
aws iot-data update-thing-shadow \
  --thing-name "ggp-func-py-gocheckin-001" \
  --payload '{"state":{"desired":{"uc_toggles":{"enable_uc1_uc2":false}}}}'

# P1 camera - enable UC8 only
aws iot-data update-thing-shadow \
  --thing-name "ggp-func-py-gocheckin-001" \
  --payload '{"state":{"desired":{"uc_toggles":{"enable_uc8":true,"enable_uc3":false}}}}'
```

### Get Current Shadow

```bash
aws iot-data get-thing-shadow \
  --thing-name "ggp-func-py-gocheckin-001" \
  | jq '.state.desired.uc_toggles'
```

---

## Toggle Field Reference

| Field | Type | Default | Patterns | Description |
|-------|------|---------|----------|-------------|
| `enable_uc1_uc2` | boolean | true | P2 only | UC1 (Member ID + Unlock) + UC2 (Tailgating) |
| `enable_uc3` | boolean | true | P1, P2 | UC3 (Unknown Face Logging) |
| `enable_uc4_uc8` | boolean | true | P2 only | UC4 (Group Size) + UC8 (Body Detection) |
| `enable_uc5` | boolean | true | P1, P2 | UC5 (Non-Active/Blocklist Alert) |
| `enable_uc8` | boolean | true | P1 only | UC8 standalone (Body Detection without UC4) |

**Note:** On P2 cameras, `enable_uc8` is ignored - UC8 is controlled by `enable_uc4_uc8` instead.
