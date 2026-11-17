import unittest

from diskrescue.smart_report import build_smart_report


class NVMeSmartReportTests(unittest.TestCase):
    def sample_payload(self):
        return {
            "model_name": "KIOXIA-EXCERIA G2 SSD 1.0 TB",
            "serial_number": "72CFC0EZFM25",
            "firmware_version": "ECFA17.1",
            "user_capacity": {"bytes": 1_000_204_886_016},
            "device": {"protocol": "NVMe"},
            "nvme_version": [1, 3],
            "smart_status": {"passed": True},
            "temperature": {"current": 45},
            "nvme_smart_health_information_log": {
                "critical_warning": 0,
                "temperature": 45,
                "available_spare": 100,
                "available_spare_threshold": 5,
                "percentage_used": 6,
                "data_units_read": 29194206,
                "data_units_written": 27666983,
                "host_reads": 171995361,
                "host_writes": 197160021,
                "controller_busy_time": 554,
                "power_cycles": 1604,
                "power_on_hours": 10514,
                "unsafe_shutdowns": 130,
                "media_errors": 0,
                "num_err_log_entries": 3256,
                "warning_temp_time": 0,
                "critical_comp_time": 0,
                "thm_temp1_trans_count": 38,
                "thm_temp2_trans_count": 0,
                "thm_temp1_total_time": 7837,
                "thm_temp2_total_time": 0,
            },
        }

    def test_kioxia_style_summary_and_table(self):
        report = build_smart_report(self.sample_payload())
        self.assertEqual(report["model"], "KIOXIA-EXCERIA G2 SSD 1.0 TB")
        self.assertEqual(report["serial"], "72CFC0EZFM25")
        self.assertEqual(report["firmware"], "ECFA17.1")
        self.assertEqual(report["health"], "良好")
        self.assertEqual(report["temperature"], "45 °C")
        metrics = dict(report["metrics"])
        self.assertEqual(metrics["预计剩余寿命"], "94%")
        self.assertEqual(metrics["通电时间"], "10,514 小时")
        self.assertEqual(metrics["通电次数"], "1,604")
        self.assertEqual(metrics["标准"], "NVM Express 1.3")

        attrs = {item["id"]: item for item in report["attributes"]}
        self.assertEqual(attrs["01"]["raw_hex"], "000000000000")
        self.assertEqual(attrs["02"]["raw_decimal"], "318")
        self.assertEqual(attrs["06"]["raw_decimal"], "29,194,206")
        self.assertEqual(attrs["0A"]["raw_decimal"], "554")
        self.assertEqual(attrs["1C"]["raw_decimal"], "7,837")
        self.assertEqual(attrs["01"]["status"], "良好")
        self.assertEqual(attrs["0E"]["status"], "良好")
        self.assertEqual(len(report["attributes"]), 21)

    def test_critical_warning_changes_health_status(self):
        payload = self.sample_payload()
        payload["nvme_smart_health_information_log"]["critical_warning"] = 4
        report = build_smart_report(payload)
        self.assertEqual(report["health"], "警告")
        self.assertIn("0x04", report["health_detail"])
        self.assertEqual(
            next(x for x in report["attributes"] if x["id"] == "01")["status"],
            "警告",
        )

    def test_missing_values_are_not_replaced_with_zero(self):
        report = build_smart_report({"device": {"protocol": "NVMe"}, "nvme_smart_health_information_log": {}})
        self.assertEqual(report["temperature"], "设备未提供")
        self.assertEqual(dict(report["metrics"])["通电次数"], "设备未提供")
        self.assertEqual(report["attributes"], [])

    def test_ata_threshold_and_failure_status(self):
        payload = {
            "model_name": "SATA SSD",
            "device": {"protocol": "ATA"},
            "smart_status": {"passed": True},
            "ata_smart_attributes": {
                "table": [
                    {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "thresh": 10,
                     "when_failed": "", "raw": {"value": 0}},
                    {"id": 197, "name": "Current_Pending_Sector", "value": 1, "thresh": 10,
                     "when_failed": "now", "raw": {"value": 4}},
                ]
            }
        }
        report = build_smart_report(payload)
        attrs = {item["id"]: item for item in report["attributes"]}
        self.assertEqual(attrs["05"]["status"], "良好")
        self.assertEqual(attrs["C5"]["status"], "失败")
        self.assertEqual(attrs["C5"]["raw_decimal"], "4")


if __name__ == "__main__":
    unittest.main()
