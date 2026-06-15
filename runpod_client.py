"""
RunPod Serverless API client.
Handles submit, poll, cancel for a worker-comfyui endpoint.
"""

import json
import time
import urllib.request
import urllib.error
from typing import Optional

RUNPOD_API_BASE = "https://api.runpod.io/v2"

# --- low-level request helpers -----------------------------------------------

def _req(method: str, url: str, data: Optional[dict], api_key: str) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise RuntimeError(f"RunPod HTTP {e.code}: {body_text}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"RunPod connection error: {e.reason}") from e


# --- public API ---------------------------------------------------------------

def submit_job(api_key: str, endpoint_id: str, workflow: dict) -> str:
    """Submit a workflow to RunPod. Returns job_id."""
    url = f"{RUNPOD_API_BASE}/{endpoint_id}/run"
    resp = _req("POST", url, {"input": {"workflow": workflow}}, api_key)
    job_id = resp.get("id")
    if not job_id:
        raise RuntimeError(f"No job id in RunPod response: {resp}")
    return job_id


def get_status(api_key: str, endpoint_id: str, job_id: str) -> dict:
    """
    Returns a dict with at least:
      status: IN_QUEUE | IN_PROGRESS | COMPLETED | FAILED | CANCELLED | TIMED_OUT
      output: (only when COMPLETED)
      error:  (only when FAILED)
    """
    url = f"{RUNPOD_API_BASE}/{endpoint_id}/status/{job_id}"
    return _req("GET", url, None, api_key)


def cancel_job(api_key: str, endpoint_id: str, job_id: str) -> dict:
    url = f"{RUNPOD_API_BASE}/{endpoint_id}/cancel/{job_id}"
    return _req("POST", url, {}, api_key)


def poll_until_done(
    api_key: str,
    endpoint_id: str,
    job_id: str,
    interval: float = 2.0,
    timeout: float = 600.0,
    progress_cb=None,
) -> dict:
    """
    Blocks until job is done (or timeout). Returns the final status dict.
    progress_cb(status_str) called each poll tick.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = get_status(api_key, endpoint_id, job_id)
        state = status.get("status", "")
        if progress_cb:
            progress_cb(state)
        if state in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
            return status
        time.sleep(interval)
    raise TimeoutError(f"Job {job_id} timed out after {timeout}s")
