#!/bin/sh
# Telemetry reader TEMPLATE — wire your device's actual sources here.
# Invoked by awso-telemetry.timer (or cron); each line writes one row.
# Keys are free-form: whatever your deployment measures.
# ponytail: hardware is never ideal — keep calibration factors beside the reads.
HABITCTL="/opt/awso/habitctl"

# Shape of a real line (replace with actual reads — i2c, modbus, HTTP…):
#   value=$(read_my_sensor)
#   $HABITCTL log-reading <key> "$value" <unit>

exit 0
