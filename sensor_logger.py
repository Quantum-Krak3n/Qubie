
#!/usr/bin/env python3

"""
Raspberry Pi Zero 2 W - Offline Sensor Logger

Sensors:
    BME280       I2C 0x76
    LSM9DS1      I2C 0x6B / 0x1E

Outputs:
    GPIO 6       Temperature-controlled output
    GPIO 26      Camera LED

Features:
    - One CSV file per Linux boot
    - Resume CSV after service restart
    - Independent sensor reconnection
    - Shared I2C bus recovery
    - Atomic CSV backups
    - Periodic camera photography
    - Automatic GPIO control
"""

import os
import csv
import time
import shutil
import signal
import subprocess
import traceback

from datetime import datetime
from pathlib import Path

import board
import busio

import adafruit_lsm9ds1
from adafruit_bme280 import basic as adafruit_bme280

from gpiozero import OutputDevice


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path("/home/pi/sensor_logger")

DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = BASE_DIR / "backup"
PHOTO_DIR = BASE_DIR / "photos"

# Sampling
SAMPLE_INTERVAL = 1.0

# Backup
BACKUP_INTERVAL = 300.0       # 5 minutes

# Sensor reconnection
SENSOR_RETRY_INTERVAL = 30.0

# GPIO temperature control
TEMP_GPIO_PIN = 6
TEMP_THRESHOLD = 3.0 
TEMP_OUTPUT_DURATION = 60.0   # Minimum ON time once triggered

# What the output does when the temperature is unknown (BME280 failed).
# True  = keep the output ON (protects against cold while blind)
# False = leave it to time out and switch OFF
HEATER_ON_WHEN_TEMP_UNKNOWN = False

# Sensor reads: number of attempts before a sensor is marked failed
SENSOR_READ_ATTEMPTS = 2

# Camera
LED_GPIO_PIN = 26
CAMERA_INTERVAL = 600.0       # 10 minutes
LED_DURATION = 20.0
LED_PRE_DELAY = 1.0           # LED on this long before capture starts
CAMERA_HANG_TIMEOUT = 30.0    # Kill a capture that runs longer than this
CAMERA_LOG = BASE_DIR / "camera.log"
MIN_FREE_BYTES = 200 * 1024 * 1024   # Skip photos below this free space

# Camera executable:
CAMERA_COMMAND = "rpicam-still"

CAMERA_TIMEOUT = 2000         # Capture timeout in milliseconds

# I2C addresses
BME_ADDRESS = 0x76
LSM_XG_ADDRESS = 0x6B
LSM_MAG_ADDRESS = 0x1E


# ============================================================
# CSV CONFIGURATION
# ============================================================

CSV_COLUMNS = [
    "timestamp",
    "uptime_s",

    "bme_status",
    "bme_error",
    "bme_temperature_C",
    "bme_humidity_percent",
    "bme_pressure_hPa",

    "lsm_status",
    "lsm_error",
    "lsm_temperature_C",

    "accel_x_ms2",
    "accel_y_ms2",
    "accel_z_ms2",

    "gyro_x_rads",
    "gyro_y_rads",
    "gyro_z_rads",

    "mag_x_gauss",
    "mag_y_gauss",
    "mag_z_gauss"
]


# ============================================================
# GLOBAL SENSOR REFERENCES
# ============================================================

i2c = None
bme = None
lsm = None

next_bme_retry = 0.0
next_lsm_retry = 0.0
next_bus_recovery = 0.0

# Set by the signal handler (SIGTERM from systemd, or SIGINT).
stop_requested = False

# Monotonic time the current camera capture started.
camera_started_at = 0.0


# ============================================================
# FILE MANAGEMENT
# ============================================================

def create_directories():

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)


def get_boot_id():

    with open("/proc/sys/kernel/random/boot_id", "r") as file:
        return file.read().strip()


def get_csv_path():

    boot_id = get_boot_id()

    return DATA_DIR / f"sensor_log_{boot_id}.csv"


def open_csv_file():

    csv_path = get_csv_path()

    new_file = not csv_path.exists() or csv_path.stat().st_size == 0

    file = open(csv_path, "a", newline="", buffering=1)

    writer = csv.DictWriter(
        file,
        fieldnames=CSV_COLUMNS
    )

    if new_file:
        writer.writeheader()
        file.flush()
        os.fsync(file.fileno())

    print(f"Logging to: {csv_path}")

    return file, writer, csv_path


def backup_csv(csv_file, csv_path):

    try:
        # Ensure all Python-buffered data reaches the OS.
        csv_file.flush()

        temporary_path = BACKUP_DIR / (csv_path.name + ".tmp")
        backup_path = BACKUP_DIR / csv_path.name

        # Copy to a temporary file first.
        shutil.copyfile(csv_path, temporary_path)

        # Ensure the temporary copy reaches disk.
        with open(temporary_path, "rb") as temp_file:
            os.fsync(temp_file.fileno())

        # Atomic replacement: the old backup remains intact
        # until the new copy is complete.
        os.replace(temporary_path, backup_path)

        print(f"Backup updated: {backup_path}")

    except Exception as error:
        print(f"Backup error: {error}")


# ============================================================
# I2C AND SENSOR INITIALISATION
# ============================================================

def create_i2c_bus():

    global i2c

    i2c = busio.I2C(board.SCL, board.SDA)

    print("I2C bus created.")


def connect_bme():

    global bme

    try:
        bme = adafruit_bme280.Adafruit_BME280_I2C(
            i2c,
            address=BME_ADDRESS
        )

        print("BME280 connected.")

        return True

    except Exception as error:

        bme = None

        print(f"BME280 connection failed: {error}")

        return False


def connect_lsm():

    global lsm

    try:
        lsm = adafruit_lsm9ds1.LSM9DS1_I2C(
            i2c,
            mag_address=LSM_MAG_ADDRESS,
            xg_address=LSM_XG_ADDRESS
        )

        print("LSM9DS1 connected.")

        return True

    except Exception as error:

        lsm = None

        print(f"LSM9DS1 connection failed: {error}")

        return False


def initialise_sensors():

    try:
        create_i2c_bus()

    except Exception as error:
        # Not fatal: the bus is recreated by recover_i2c_bus() later.
        print(f"I2C bus creation failed: {error}")

    connect_bme()
    connect_lsm()


def recover_i2c_bus():

    global i2c, bme, lsm

    print("Both sensors unavailable. Recreating I2C bus.")

    bme = None
    lsm = None

    if i2c is not None:

        try:
            i2c.deinit()

        except Exception as error:
            print(f"I2C shutdown error: {error}")

    time.sleep(1)

    try:

        create_i2c_bus()

        connect_bme()
        connect_lsm()

    except Exception as error:

        print(f"I2C recovery failed: {error}")


# ============================================================
# SENSOR READING
# ============================================================

def empty_bme_reading():

    return {
        "bme_status": "ERROR",
        "bme_error": "",
        "bme_temperature_C": None,
        "bme_humidity_percent": None,
        "bme_pressure_hPa": None
    }


def empty_lsm_reading():

    return {
        "lsm_status": "ERROR",
        "lsm_error": "",
        "lsm_temperature_C": None,

        "accel_x_ms2": None,
        "accel_y_ms2": None,
        "accel_z_ms2": None,

        "gyro_x_rads": None,
        "gyro_y_rads": None,
        "gyro_z_rads": None,

        "mag_x_gauss": None,
        "mag_y_gauss": None,
        "mag_z_gauss": None
    }


def read_bme():

    global bme, next_bme_retry

    result = empty_bme_reading()

    if bme is None:
        return result

    error = None

    for attempt in range(SENSOR_READ_ATTEMPTS):

        try:

            result.update({
                "bme_status": "OK",
                "bme_temperature_C": bme.temperature,
                "bme_humidity_percent": bme.relative_humidity,
                "bme_pressure_hPa": bme.pressure
            })

            return result

        except Exception as read_error:

            error = read_error
            time.sleep(0.05)

    print(f"BME280 reading failed: {error}")

    result["bme_error"] = str(error)

    bme = None
    next_bme_retry = time.monotonic() + SENSOR_RETRY_INTERVAL

    return result


def read_lsm():

    global lsm, next_lsm_retry

    result = empty_lsm_reading()

    if lsm is None:
        return result

    error = None

    for attempt in range(SENSOR_READ_ATTEMPTS):

        try:

            accel_x, accel_y, accel_z = lsm.acceleration
            gyro_x, gyro_y, gyro_z = lsm.gyro
            mag_x, mag_y, mag_z = lsm.magnetic

            result.update({

                "lsm_status": "OK",
                "lsm_temperature_C": lsm.temperature,

                "accel_x_ms2": accel_x,
                "accel_y_ms2": accel_y,
                "accel_z_ms2": accel_z,

                "gyro_x_rads": gyro_x,
                "gyro_y_rads": gyro_y,
                "gyro_z_rads": gyro_z,

                "mag_x_gauss": mag_x,
                "mag_y_gauss": mag_y,
                "mag_z_gauss": mag_z
            })

            return result

        except Exception as read_error:

            error = read_error
            time.sleep(0.05)

    print(f"LSM9DS1 reading failed: {error}")

    result["lsm_error"] = str(error)

    lsm = None
    next_lsm_retry = time.monotonic() + SENSOR_RETRY_INTERVAL

    return result


# ============================================================
# SENSOR RECONNECTION
# ============================================================

def reconnect_sensors():

    global next_bme_retry
    global next_lsm_retry
    global next_bus_recovery

    now = time.monotonic()

    # If both sensors are unavailable, recreate the shared bus.
    if bme is None and lsm is None:

        if now >= next_bus_recovery:

            recover_i2c_bus()

            next_bme_retry = now + SENSOR_RETRY_INTERVAL
            next_lsm_retry = now + SENSOR_RETRY_INTERVAL
            next_bus_recovery = now + SENSOR_RETRY_INTERVAL

        return

    # Reconnect BME independently.
    if bme is None and now >= next_bme_retry:

        connect_bme()

        next_bme_retry = now + SENSOR_RETRY_INTERVAL

    # Reconnect LSM independently.
    if lsm is None and now >= next_lsm_retry:

        connect_lsm()

        next_lsm_retry = now + SENSOR_RETRY_INTERVAL


# ============================================================
# TEMPERATURE-CONTROLLED GPIO
# ============================================================

def update_temperature_output(
    temperature,
    output,
    active_until
):

    now = time.monotonic()

    if temperature is None:
        needs_heat = HEATER_ON_WHEN_TEMP_UNKNOWN
    else:
        needs_heat = temperature < TEMP_THRESHOLD

    if needs_heat:

        if not output.value:

            output.on()

            if temperature is None:
                print("Temperature unknown. GPIO output ON (fail-safe).")
            else:
                print(
                    f"Temperature {temperature:.2f} C is below "
                    f"{TEMP_THRESHOLD:.2f} C. GPIO output ON."
                )

        # Keep extending the ON period while heat is still needed,
        # so the output never switches off and straight back on.
        active_until = now + TEMP_OUTPUT_DURATION

    elif output.value and now >= active_until:

        output.off()

        print("Temperature GPIO output OFF.")

    return active_until


# ============================================================
# CAMERA
# ============================================================

def take_photo():

    timestamp = datetime.now().astimezone().strftime(
        "%Y%m%d_%H%M%S"
    )

    # Boot id prefix + uptime let each photo be matched to a CSV row
    # even if the wall clock (timestamp) is wrong.
    boot_short = get_boot_id().replace("-", "")[:8]
    uptime = int(get_uptime())

    photo_path = PHOTO_DIR / f"photo_{boot_short}_up{uptime:06d}_{timestamp}.jpg"

    command = [
        CAMERA_COMMAND,
        "-n",
        "-o",
        str(photo_path)
    ]

    global camera_started_at

    try:

        # Camera output goes to a log so failures can be diagnosed.
        with open(CAMERA_LOG, "a") as log_file:

            log_file.write(f"\n=== {get_timestamp()} {photo_path.name} ===\n")
            log_file.flush()

            process = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT
            )

        camera_started_at = time.monotonic()

        print(f"Camera started: {photo_path}")

        return process

    except Exception as error:

        print(f"Camera failed to start: {error}")

        return None


def update_camera(
    led,
    camera_process,
    led_off_time
):

    now = time.monotonic()

    # Turn off LED after its LED_DURATION period.
    if led.value and now >= led_off_time:

        led.off()

        print("Camera LED OFF.")

    # Do not start another capture while one is running.
    if camera_process is not None:

        if camera_process.poll() is not None:

            if camera_process.returncode != 0:
                print(
                    f"Camera exited with code "
                    f"{camera_process.returncode}"
                )

            camera_process = None

        elif now - camera_started_at > CAMERA_HANG_TIMEOUT:

            # Capture is hung: kill it so photography can continue.
            print("Camera capture hung. Killing process.")

            camera_process.kill()

            try:
                camera_process.wait(timeout=5)
                camera_process = None

            except Exception:
                # Could not reap yet; try again on the next loop.
                pass

    return camera_process, led_off_time


# ============================================================
# TIMESTAMP
# ============================================================

def get_timestamp():

    # NOTE: the Pi Zero 2 W has no RTC. Offline, this wall-clock time
    # is only a best guess and may jump. Use uptime_s for ordering.
    return datetime.now().astimezone().isoformat(
        timespec="milliseconds"
    )


def get_uptime():

    # Seconds since boot. Monotonic, so it is correct even when the
    # wall clock is wrong. Matches the boot-specific CSV file.
    return time.monotonic()


# ============================================================
# SIGNAL HANDLING
# ============================================================

def handle_stop_signal(signum, frame):

    # Only set a flag. The main loop exits at a safe point, so a
    # CSV write or backup is never interrupted half-way.
    global stop_requested

    stop_requested = True

    print(f"Signal {signum} received. Stopping.")


# ============================================================
# MAIN PROGRAM
# ============================================================

def main():

    global next_bme_retry
    global next_lsm_retry
    global next_bus_recovery

    create_directories()

    # Startup checks (visible in the journal).
    if shutil.which(CAMERA_COMMAND) is None:
        print(f"WARNING: camera command '{CAMERA_COMMAND}' not found.")

    free_mb = shutil.disk_usage(BASE_DIR).free / (1024 * 1024)
    print(f"Free disk space: {free_mb:.0f} MB")

    # Handle systemd stop/shutdown (SIGTERM) and Ctrl+C (SIGINT).
    signal.signal(signal.SIGTERM, handle_stop_signal)
    signal.signal(signal.SIGINT, handle_stop_signal)

    # Initialise GPIO.
    temperature_output = OutputDevice(
        TEMP_GPIO_PIN,
        active_high=True,
        initial_value=False
    )

    camera_led = OutputDevice(
        LED_GPIO_PIN,
        active_high=True,
        initial_value=False
    )

    csv_file = None

    camera_process = None

    active_until = 0.0
    led_off_time = 0.0

    # Latest BME280 temperature (None until the first successful read).
    temperature = None

    # Initialise sensors.
    initialise_sensors()

    # Retry timers.
    now = time.monotonic()

    next_bme_retry = now + SENSOR_RETRY_INTERVAL
    next_lsm_retry = now + SENSOR_RETRY_INTERVAL
    next_bus_recovery = now + SENSOR_RETRY_INTERVAL

    # Open boot-specific CSV.
    csv_file, csv_writer, csv_path = open_csv_file()

    now = time.monotonic()

    next_sample = now
    next_backup = now + BACKUP_INTERVAL
    next_photo = now + CAMERA_INTERVAL

    print("Sensor logger started.")

    try:

        while not stop_requested:

            try:

                now = time.monotonic()

                # ------------------------------------------------
                # Sensor reconnection
                # ------------------------------------------------

                reconnect_sensors()

                # ------------------------------------------------
                # Sample: read sensors and write CSV (once per interval)
                # ------------------------------------------------

                if now >= next_sample:

                    bme_data = read_bme()
                    lsm_data = read_lsm()

                    temperature = bme_data["bme_temperature_C"]

                    row = {
                        "timestamp": get_timestamp(),
                        "uptime_s": f"{get_uptime():.3f}"
                    }

                    row.update(bme_data)
                    row.update(lsm_data)

                    csv_writer.writerow(row)

                    # Flush each sampling cycle.
                    csv_file.flush()

                    # Periodically force data to storage.
                    if int(now / SAMPLE_INTERVAL) % 5 == 0:
                        os.fsync(csv_file.fileno())

                    next_sample += SAMPLE_INTERVAL

                    # Avoid trying to catch up after a long interruption.
                    if now - next_sample > SAMPLE_INTERVAL:
                        next_sample = now + SAMPLE_INTERVAL

                # ------------------------------------------------
                # Temperature GPIO (uses the latest sampled temperature;
                # still runs every loop so the output timer is accurate)
                # ------------------------------------------------

                active_until = update_temperature_output(
                    temperature,
                    temperature_output,
                    active_until
                )

                # ------------------------------------------------
                # Camera scheduling
                # ------------------------------------------------

                if now >= next_photo and camera_process is None:

                    next_photo = now + CAMERA_INTERVAL

                    free_bytes = shutil.disk_usage(PHOTO_DIR).free

                    if free_bytes < MIN_FREE_BYTES:

                        # Protect the CSV log: never fill the disk with photos.
                        print(
                            f"Low disk space ({free_bytes // (1024 * 1024)} MB)."
                            f" Photo skipped."
                        )

                    else:

                        camera_led.on()

                        led_off_time = now + LED_DURATION

                        # Let the LED light the scene before capture starts.
                        time.sleep(LED_PRE_DELAY)

                        camera_process = take_photo()

                camera_process, led_off_time = update_camera(
                    camera_led,
                    camera_process,
                    led_off_time
                )

                # ------------------------------------------------
                # CSV backup
                # ------------------------------------------------

                if now >= next_backup:

                    backup_csv(csv_file, csv_path)

                    next_backup = now + BACKUP_INTERVAL

                # ------------------------------------------------
                # Main loop delay
                # ------------------------------------------------

                time.sleep(0.1)

            except Exception:

                # One failure (e.g. disk error) must not stop the heater,
                # camera and logging. Report it and keep going.
                print("Main loop error:")
                traceback.print_exc()

                time.sleep(1)

    except KeyboardInterrupt:

        print("Logger stopped by keyboard interrupt.")

    finally:

        print("Shutting down.")

        if csv_file is not None:

            backup_csv(csv_file, csv_path)

            csv_file.close()

        if camera_process is not None:

            if camera_process.poll() is None:

                camera_process.terminate()

                try:
                    camera_process.wait(timeout=3)
                except Exception:
                    camera_process.kill()

        temperature_output.off()
        camera_led.off()

        temperature_output.close()
        camera_led.close()

        if i2c is not None:

            try:
                i2c.deinit()

            except Exception:
                pass

        print("Shutdown complete.")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
