#!/bin/bash
for i in $(seq 1 10); do
    if [ -w /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq ]; then
        echo 1800000 | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq > /dev/null 2>&1
        echo ondemand | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor > /dev/null 2>&1
        logger "cpu-cap: freq capped to 1.8GHz, governor=ondemand"
        exit 0
    fi
    sleep 1
done
logger "cpu-cap: WARNING could not set"
exit 1
