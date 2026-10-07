# ipmi-exporter: BMC memory (DIMM) temperature via in-band IPMI (KCS).
#
# Host prerequisites (one-time):
#   1. A reachable in-band BMC (KCS interface) so /dev/ipmi0 exists.
#      Load the kernel modules if the device is missing:
#        sudo modprobe ipmi_si && sudo modprobe ipmi_devintf
#      Persist across reboots:
#        echo "ipmi_si" | sudo tee /etc/modules-load.d/ipmi.conf
#        echo "ipmi_devintf" | sudo tee -a /etc/modules-load.d/ipmi.conf
#   2. ipmitool must be able to talk to the BMC as the user that runs the
#      container (root here; a non-root user needs an udev rule or the
#      `ipmi` group, plus BMC access).
#
# Verify on the host before enabling the `ipmi` compose profile:
#   ipmitool sensor list | grep -iE 'mem|dimm'
#
FROM python:3.12.14-slim

# ipmitool reads the BMC through /dev/ipmi0 (in-band KCS), which is mounted
# from the host in docker-compose.yml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ipmitool \
    && rm -rf /var/lib/apt/lists/*

COPY exporter.py /exporter.py
EXPOSE 9109
CMD ["python", "/exporter.py"]
