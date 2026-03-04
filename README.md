# Camera Glasses Recorder (Raspberry Pi 5)

BLE-controlled video recorder for a USB UVC camera on Raspberry Pi 5. This repo allows you to have a almost perfect video recording and syncronization with BLE commands.
You can use these glasses to synchronize with other devices in order to record synchronized video/data. 
If you place the usb camera on a pair of glasses, you have a working camera glasses prototype. 

Commands:
- `rec` start recording
- `stp` stop recording

Records MJPG input at 800x600 @ 20 fps and saves MP4 (H.264) locally. 

## Hardware
- Raspberry Pi 5 
- USB UVC camera (shows as /dev/video0) 
- Power via USB-c 
- microSD for storage


## 1) Install OS (Raspberry Pi OS Lite) and SSH Access
1. Download Raspberry Pi Imager. (https://www.raspberrypi.com/software/)
2. Insert your micro-SD card and open Raspberry Pi Imager.
3. Select Raspberry Pi Zero 2 W
4. In the OS Section: Raspberry Pi OS (other) -> Select Raspberry Pi OS Lite (32-bit) (64 if using raspberry pi 5)
5. Select your micro-SD storage.
6. Set hostname (example: `rpi5`) and credentials (username: 'user', this is an important point). Then set Wifi settings (SSH ACTIVE!, pi connect OFF).
7. Write and when it's finished insert your card on the raspberry pi device.
8. Boot and wait a minute, then from your terminal:
   ```bash
   ssh user@rpi5.local
9. If you have this error ('WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!') run this line and retry point 8: 
   ```bash
   ssh-keygen -f '/home/user/.ssh/known_hosts' -R 'rpi5.local'
10. Enable stable SSH features for debugging:
    ```bash
        sudo systemctl enable ssh
        sudo systemctl start ssh
        systemctl is-enabled ssh
        systemctl status ssh --no-pager
    ``` 
    
## 2) Install dependencies

1. Inside the rpi5 terminal clone the repository:
   ```bash
   sudo apt-get install git
   git clone https://github.com/giampierobartolomei/raspberrypi5camera.git
   
2. Put yourself in the dir:
   ```bash
   cd raspberrypi5camera

4. Run installer:
   ```bash
   bash scripts/install.sh

## 3) Enable BlueZ experimental mode (required for stable peripheral role)

1. Execute:
   ```bash
   bash scripts/enable_bluetooth_experimental.sh
   sudo reboot 
2. verify after the boot:
   ```bash
   ssh user@rpi5.local
   sudo rfkill unblock bluetooth
   sudo hciconfig hci0 up
   sudo bluetoothctl power on
   cd raspberrypi5camera
   bash scripts/verify.sh
You should see bluetoothd --experimental -E and hci0 UP RUNNING. You should see also camera formats.

## 4) Run Manually for debug
   1. Execute
      ```bash
      sudo python3 -u ble/ble_sync.py
   2. If you get these print it's running correctly:
      ```bash
        [BLE] Using adapter: /org/bluez/hci0
        [BLE] Registering GATT application…
        [BLE] Registering advertisement…
        [BLE] GATT registered
        [BLE] Advertisement registered
        
## 5) Use nRF Connect - if you want to start recording videos from your phone
See docs: docs/nrf_connect.md

## 6) Autostart via systemd - after this program starts automatically on boot - run on the repo filepath
   ```bash
sudo cp systemd/ble-gatt-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ble-gatt-recorder.service #use this only with stable code, use 'start' instead for debugging - 'disable' for stopping the process
sudo journalctl -u ble-gatt-recorder.service -f
   ```
   
## 7) Get the MP4 files
1. Files are saved in: /home/user/raspberrypi5camera/ble/recordings
2. From another pc:
  ```bash
  scp user@rpi5.local:/home/user/raspberrypi5camera/ble/recordings/filename.mp4 .
   ```
3. you can also save video from the sd card.
## EXTRA: Test the camera via web

1. install the package:
   ```bash
   git clone https://github.com/jacksonliam/mjpg-streamer.git
   cd mjpg-streamer/mjpg-streamer-experimental
   sudo apt install -y cmake build-essential libjpeg-dev
   make
   sudo make install

2. Run this line:
   ```bash
   mjpg_streamer -i "input_uvc.so -d /dev/video0 -r 640x480 -f 15" -o "output_http.so -p 8080 -w /usr/local/share/mjpg-streamer/www"


3. Go to this website from another device: (find the IP address by typing hostname -I)
   ```bash
   http://<raspberry-pi-ip>:8080



## OPTIONAL: DEBUG LEDs - USE A RESISTOR!

1. Connect a Yellow Led, as a Service Ready LED, to know if the Camera Glasses are ready to Record. (GPIO20)
2. Connect a Green Led, as a BT connected LED, to know if you are connected. (GPIO20)
3. Connect a Red Led, as a Recording LED, to know if you are recording. (GPIO12)
4. Change ENABLE_LEDS to True in ble_sync.py.
