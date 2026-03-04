Scan →  CameraModule

Service UUID: 19B10010-E8F2-537E-4F6C-D104768A1214

Characteristic UUID: 19b10012-e8f2-537e-4f6c-d104768a1214

Write type: Write o Write Without Response

Payload: UTF-8 plain text

Start recording -> rec

Stop recording -> stp


Expected logs:

[BLE] GATT registered

[BLE] Advertisement registered

on command:

[CMD] 'rec' then [REC] start → ...

[CMD] 'stp' then [REC] stop

7
