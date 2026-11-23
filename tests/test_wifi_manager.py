import os
import tempfile
import unittest
from unittest.mock import patch, call


if os.name != "nt":
    from app import wifi_manager


@unittest.skipIf(os.name == "nt", "NetworkManager integration uses Linux")
class WifiManagerTests(unittest.TestCase):
    def test_nmcli_escaped_ssid_fields_are_parsed(self):
        fields = wifi_manager._split_nmcli_row(
            r"uuid:802-11-wireless:Office\:Lab:wpa-psk"
        )
        self.assertEqual(
            fields, ["uuid", "802-11-wireless", "Office:Lab", "wpa-psk"]
        )

    def test_known_network_list_includes_networkmanager_profiles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "missing-wpa_supplicant.conf")
            with patch.object(wifi_manager, "WPA_CONF", config_path), \
                    patch.object(wifi_manager, "_nm_wifi_profiles", return_value=[
                        {"uuid": "profile-id", "ssid": "Home WiFi",
                         "key_mgmt": "wpa-psk"},
                    ]):
                networks = wifi_manager.get_known_networks()

        self.assertEqual(networks, [{
            "ssid": "Home WiFi",
            "has_password": True,
            "key_mgmt": "WPA-PSK",
        }])

    def test_forget_deletes_networkmanager_and_legacy_profiles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "wpa_supplicant.conf")
            with open(config_path, "w", encoding="utf-8") as config:
                config.write(
                    'update_config=1\n\nnetwork={\n'
                    '    ssid="Home WiFi"\n    psk="password123"\n}\n'
                )
            result = wifi_manager.subprocess.CompletedProcess([], 0, "", "")
            with patch.object(wifi_manager, "WPA_CONF", config_path), \
                    patch.object(wifi_manager, "_nm_wifi_profiles", return_value=[
                        {"uuid": "profile-id", "ssid": "Home WiFi",
                         "key_mgmt": "wpa-psk"},
                    ]), \
                    patch.object(wifi_manager, "_nmcli", return_value=result) as nmcli:
                ok, message = wifi_manager.remove_network("Home WiFi")

            self.assertTrue(ok, message)
            nmcli.assert_called_once_with(
                ["connection", "delete", "uuid", "profile-id"]
            )
            with open(config_path, encoding="utf-8") as config:
                self.assertNotIn('ssid="Home WiFi"', config.read())

    def test_reconnect_updates_existing_profile_instead_of_reusing_old_password(self):
        profiles = [
            {"uuid": "primary", "ssid": "Home WiFi", "key_mgmt": "wpa-psk"},
            {"uuid": "duplicate", "ssid": "Home WiFi", "key_mgmt": "wpa-psk"},
        ]
        result = wifi_manager.subprocess.CompletedProcess([], 0, "", "")
        with patch.object(wifi_manager, "is_ap_active", return_value=False), \
                patch.object(wifi_manager, "_nm_wifi_profiles", return_value=profiles), \
                patch.object(wifi_manager, "_nmcli", return_value=result) as nmcli, \
                patch.object(wifi_manager, "_remove_wpa_network", return_value=False):
            ok, message = wifi_manager.connect_to_network(
                "Home WiFi", "newpassword"
            )

        self.assertTrue(ok, message)
        calls = [entry.args[0] for entry in nmcli.call_args_list]
        self.assertEqual(calls[0][:4], [
            "connection", "modify", "uuid", "primary",
        ])
        self.assertIn("newpassword", calls[0])
        self.assertEqual(calls[1], [
            "connection", "up", "uuid", "primary", "ifname", wifi_manager.IFACE,
        ])
        self.assertEqual(calls[2], [
            "connection", "delete", "uuid", "duplicate",
        ])

    def test_retry_activates_only_a_visible_known_profile(self):
        profiles = [
            {"uuid": "home", "ssid": "Home WiFi", "key_mgmt": "wpa-psk"},
            {"uuid": "away", "ssid": "Away WiFi", "key_mgmt": "wpa-psk"},
        ]
        result = wifi_manager.subprocess.CompletedProcess([], 0, "", "")
        with patch.object(wifi_manager, "_nm_wifi_profiles", return_value=profiles), \
                patch.object(wifi_manager, "scan_networks", return_value=[
                    {"ssid": "Home WiFi", "signal": 80, "security": "WPA"},
                ]), \
                patch.object(wifi_manager, "_nmcli", return_value=result) as nmcli, \
                patch.object(wifi_manager, "_is_nm_connected",
                             side_effect=[False, True]):
            connected = wifi_manager._retry_known_networks()

        self.assertTrue(connected)
        nmcli.assert_called_once_with([
            "connection", "up", "uuid", "home", "ifname", wifi_manager.IFACE,
        ], timeout=30)


if __name__ == "__main__":
    unittest.main()
