import json
import subprocess
import unittest
from unittest.mock import patch

from diskrescue.smartctl import Device, read_smart, summarize


class SmartctlImportAndSummaryTests(unittest.TestCase):
    def test_required_public_functions_are_importable(self):
        self.assertTrue(callable(read_smart))
        self.assertTrue(callable(summarize))
        self.assertTrue(Device)

    def test_read_smart_accepts_json_even_when_smartctl_status_is_nonzero(self):
        payload = {
            "model_name": "Example NVMe",
            "serial_number": "TEST-001",
            "smart_status": {"passed": True},
        }
        completed = subprocess.CompletedProcess(
            args=["smartctl"],
            returncode=2,
            stdout=json.dumps(payload),
            stderr="SMART warning",
        )
        device = Device(name="PhysicalDrive0", device_type="nvme")
        with patch("diskrescue.smartctl.smartctl_path", return_value="smartctl.exe"), \
             patch("diskrescue.smartctl._run_json", return_value=(payload.copy(), completed)):
            result = read_smart(device)

        self.assertEqual(result["model_name"], "Example NVMe")
        self.assertEqual(result["_diskrescue_diagnostics"]["return_code"], 2)
        self.assertEqual(result["_diskrescue_diagnostics"]["stderr"], "SMART warning")

    def test_summarize_nvme_health_fields(self):
        payload = {
            "model_name": "Example NVMe",
            "serial_number": "TEST-002",
            "user_capacity": {"bytes": 1_000_000_000},
            "smart_status": {"passed": True},
            "nvme_smart_health_information_log": {
                "temperature": 39,
                "power_on_hours": 1234,
                "percentage_used": 4,
                "available_spare": 100,
                "unsafe_shutdowns": 2,
                "media_errors": 0,
                "data_units_read": 10,
                "data_units_written": 20,
            },
        }
        rows = summarize(payload)
        values = dict(rows)
        self.assertEqual(values["型号"], "Example NVMe")
        self.assertEqual(values["寿命已使用"], "4%")
        self.assertEqual(values["可用备用空间"], "100%")
        self.assertEqual(values["温度"], "39 °C")
        self.assertEqual(values["通电时间"], "1,234 小时")
        self.assertEqual(values["介质与数据完整性错误"], "0 次")
        self.assertIn("数据读取量", values)

    def test_unmatched_smart_alias_is_not_a_second_physical_disk(self):
        windows_rows = [
            {"Number": 0, "FriendlyName": "SSD A", "SerialNumber": "A1", "Size": 1000, "BusType": "NVMe"},
            {"Number": 1, "FriendlyName": "SSD B", "SerialNumber": "B1", "Size": 2000, "BusType": "NVMe"},
            {"Number": 2, "FriendlyName": "Kingston DataTraveler 3.0", "SerialNumber": "", "Size": 3000, "BusType": "USB"},
        ]
        scan_payload = {"devices": [{"name": "/dev/sdc", "type": "scsi", "protocol": "SCSI"}]}
        completed = subprocess.CompletedProcess(
            args=["smartctl"], returncode=0, stdout="", stderr=""
        )
        with patch("diskrescue.smartctl.list_windows_disks", return_value=windows_rows), \\
             patch("diskrescue.smartctl.smartctl_path", return_value="smartctl.exe"), \\
             patch("diskrescue.smartctl._run_json", return_value=(scan_payload, completed)), \\
             patch("diskrescue.smartctl.read_smart", return_value={
                 "model_name": "", "serial_number": "", "user_capacity": {"bytes": 0}
             }):
            from diskrescue.smartctl import discover_devices
            devices = discover_devices()

        self.assertEqual(len(devices), 3)
        self.assertEqual([item.windows_disk_number for item in devices], [0, 1, 2])
        self.assertTrue(all(item.name != "/dev/sdc" for item in devices))


if __name__ == "__main__":
    unittest.main()
