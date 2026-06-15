"""
ComfyUI-RunPod-Remote
=====================
Custom node that adds a "Run on RunPod" button to ComfyUI.

Features:
  - Preflight check: verify all required custom nodes and models are present
    on the RunPod endpoint before submitting the job
  - Submit workflow in API format (app.graphToPrompt().output)
  - Poll job status and show results inside ComfyUI
  - "Scan RunPod" to refresh the endpoint manifest (installed nodes + models)
  - "RunPodSystemInfo" ComfyUI node: runs on the worker to gather manifest data

Endpoint: worker-comfyui (worker-comfyui by fofr/blib-la)
Compatible with: ComfyUI 0.24.x
"""

import json
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from server import PromptServer

from .preflight import run_preflight, save_manifest, load_manifest
from .runpod_client import submit_job, get_status, cancel_job

# ─── Config helpers ───────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.json"
DEFAULT_CONFIG = {
    "api_key": "",
    "endpoint_id": "",
    "companion_url": "",
}


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def _proxy_companion(path: str, body: bytes = b"", method: str = "POST") -> tuple:
    """Forward request to companion app. URL set via companion_url in config.json."""
    import urllib.request as ureq
    base = _load_config().get("companion_url", "").rstrip("/")
    if not base:
        return {"error": "Companion app not configured. Set companion_url in config.json."}, 400
    try:
        req = ureq.Request(
            base + path,
            data=body or None,
            headers={"Content-Type": "application/json"} if body else {},
            method=method,
        )
        with ureq.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), 200
    except ureq.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {"error": f"HTTP {e.code}"}, e.code
    except Exception as e:
        return {"error": str(e)}, 503


# ─── In-memory job tracker ────────────────────────────────────────────────────
# Keyed by RunPod job_id. Cleaned up after 1h of inactivity.

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _track_job(job_id: str, initial: dict) -> None:
    with _jobs_lock:
        _jobs[job_id] = {**initial, "job_id": job_id, "updated_at": _now()}


def _update_job(job_id: str, **kwargs) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)
            _jobs[job_id]["updated_at"] = _now()


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ─── Background poll thread ───────────────────────────────────────────────────

def _poll_job_bg(api_key: str, endpoint_id: str, job_id: str) -> None:
    """Background thread that polls RunPod until the job completes."""
    import time
    deadline = time.time() + 3600  # max 1h
    interval = 2.0
    while time.time() < deadline:
        try:
            status = get_status(api_key, endpoint_id, job_id)
            state = status.get("status", "UNKNOWN")
            _update_job(job_id, status=state, raw=status)
            if state in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
                break
        except Exception as e:
            _update_job(job_id, status="ERROR", error=str(e))
            break
        time.sleep(interval)
        # Back-off after first 30s
        if time.time() > deadline - 3570:
            interval = min(interval * 1.5, 30.0)


# ─── HTTP routes ──────────────────────────────────────────────────────────────

routes = web.RouteTableDef()


@routes.get("/runpod/config")
async def api_get_config(request):
    cfg = _load_config()
    # Never send raw api_key — send masked version
    masked = {**cfg, "api_key": ("*" * 8 + cfg["api_key"][-4:]) if len(cfg.get("api_key", "")) > 4 else ""}
    return web.json_response(masked)


@routes.post("/runpod/config")
async def api_save_config(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    cfg = _load_config()
    if data.get("api_key") and not data["api_key"].startswith("****"):
        cfg["api_key"] = data["api_key"].strip()
    if data.get("endpoint_id"):
        cfg["endpoint_id"] = data["endpoint_id"].strip()
    _save_config(cfg)
    return web.json_response({"ok": True})


@routes.post("/runpod/preflight")
async def api_preflight(request):
    """
    Body: {"workflow": {<api_format>}}
    Returns preflight result JSON.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    workflow = data.get("workflow")
    if not workflow or not isinstance(workflow, dict):
        return web.json_response({"error": "Missing 'workflow' field"}, status=400)
    result = run_preflight(workflow)
    return web.json_response(result)


@routes.post("/runpod/submit")
async def api_submit(request):
    """
    Body: {"workflow": {<api_format>}}
    Submits to RunPod and returns {"job_id": "..."}
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    workflow = data.get("workflow")
    if not workflow or not isinstance(workflow, dict):
        return web.json_response({"error": "Missing 'workflow' field"}, status=400)
    cfg = _load_config()
    api_key = cfg.get("api_key", "")
    endpoint_id = cfg.get("endpoint_id", "")
    if not api_key:
        return web.json_response({"error": "RunPod API key not configured. Open RunPod settings."}, status=400)
    if not endpoint_id:
        return web.json_response({"error": "RunPod endpoint ID not configured."}, status=400)
    try:
        job_id = submit_job(api_key, endpoint_id, workflow)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    _track_job(job_id, {"status": "IN_QUEUE", "endpoint_id": endpoint_id})
    # Start background poll thread
    t = threading.Thread(target=_poll_job_bg, args=(api_key, endpoint_id, job_id), daemon=True)
    t.start()
    return web.json_response({"job_id": job_id, "status": "IN_QUEUE"})


@routes.get("/runpod/status/{job_id}")
async def api_status(request):
    """Returns cached job status (updated by background thread)."""
    job_id = request.match_info["job_id"]
    job = _get_job(job_id)
    if not job:
        # Fall back to live API call
        cfg = _load_config()
        try:
            raw = get_status(cfg["api_key"], cfg["endpoint_id"], job_id)
            return web.json_response(raw)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=404)
    return web.json_response(job)


@routes.post("/runpod/cancel/{job_id}")
async def api_cancel(request):
    job_id = request.match_info["job_id"]
    cfg = _load_config()
    try:
        result = cancel_job(cfg["api_key"], cfg["endpoint_id"], job_id)
        _update_job(job_id, status="CANCELLED")
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.post("/runpod/scan")
async def api_scan(request):
    """
    Scans the RunPod endpoint for installed custom nodes and models.
    Submits a probe job using the RunPodSystemInfo node (included in this package).
    Returns {"job_id": "..."} — client polls /runpod/scan_status/{job_id}
    """
    cfg = _load_config()
    api_key = cfg.get("api_key", "")
    endpoint_id = cfg.get("endpoint_id", "")
    if not api_key:
        return web.json_response({"error": "API key not configured"}, status=400)

    # Probe workflow: just the RunPodSystemInfo node
    probe_workflow = {
        "1": {
            "class_type": "RunPodSystemInfo",
            "inputs": {}
        }
    }
    try:
        job_id = submit_job(api_key, endpoint_id, probe_workflow)
    except Exception as e:
        return web.json_response({"error": f"Failed to submit scan job: {e}"}, status=500)

    _track_job(job_id, {"status": "IN_QUEUE", "endpoint_id": endpoint_id, "is_scan": True})
    t = threading.Thread(target=_poll_job_bg, args=(api_key, endpoint_id, job_id), daemon=True)
    t.start()
    return web.json_response({"job_id": job_id})


@routes.get("/runpod/scan_status/{job_id}")
async def api_scan_status(request):
    """
    Returns scan job status. When COMPLETED, extracts manifest from output
    and saves it to manifest_cache.json.
    """
    job_id = request.match_info["job_id"]
    job = _get_job(job_id)
    if not job:
        return web.json_response({"error": "Job not found"}, status=404)

    state = job.get("status", "")
    if state == "COMPLETED":
        raw = job.get("raw", {})
        output = raw.get("output", {})
        # worker-comfyui returns output as {"images": [...]}, we embedded manifest in a text node
        manifest_data = None
        # Try direct manifest key
        if "manifest" in output:
            manifest_data = output["manifest"]
        # Try extracting from images output (base64 text)
        elif "images" in output:
            for img in output["images"]:
                if isinstance(img, dict) and img.get("type") == "text":
                    try:
                        manifest_data = json.loads(img["data"])
                    except Exception:
                        pass
        # Try output as list of dicts with node results
        elif isinstance(output, list):
            for item in output:
                if isinstance(item, dict) and "manifest" in item:
                    manifest_data = item["manifest"]
                    break

        if manifest_data and isinstance(manifest_data, dict):
            manifest_data["scanned_at"] = _now()
            save_manifest(manifest_data)
            return web.json_response({
                "status": "COMPLETED",
                "manifest_saved": True,
                "custom_nodes_count": len(manifest_data.get("custom_nodes", [])),
                "models_count": len(manifest_data.get("models", [])),
            })
        else:
            # Scan job completed but no parseable manifest — save what we can
            return web.json_response({
                "status": "COMPLETED",
                "manifest_saved": False,
                "warning": "Could not parse manifest from job output. Check that RunPodSystemInfo node is installed on the worker.",
                "raw_output": output,
            })

    return web.json_response({"status": state, "job_id": job_id})


@routes.get("/runpod/manifest")
async def api_manifest(request):
    """Returns current manifest summary."""
    m = load_manifest()
    if not m:
        return web.json_response({"exists": False})
    return web.json_response({
        "exists": True,
        "scanned_at": m.get("scanned_at", "unknown"),
        "custom_nodes_count": len(m.get("custom_nodes", [])),
        "models_count": len(m.get("models", [])),
        "custom_nodes": m.get("custom_nodes", []),
        "models": m.get("models", []),
    })


@routes.post("/runpod/manifest/import")
async def api_manifest_import(request):
    """
    Manual manifest import. Body: {"custom_nodes": [...], "models": [...]}
    Useful when user manually knows what's installed.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    manifest = {
        "custom_nodes": data.get("custom_nodes", []),
        "models": data.get("models", []),
        "scanned_at": _now(),
    }
    save_manifest(manifest)
    return web.json_response({"ok": True, "scanned_at": manifest["scanned_at"]})


@routes.get("/runpod/aria2_check")
async def api_aria2_check(request):
    data, status = _proxy_companion("/api/runpod/aria2_check", method="GET")
    return web.json_response(data, status=status)


@routes.post("/runpod/scan_ssh")
async def api_scan_ssh(request):
    data, status = _proxy_companion("/api/runpod/scan_ssh", await request.read())
    return web.json_response(data, status=status)


@routes.post("/runpod/trigger_uploads")
async def api_trigger_uploads(request):
    data, status = _proxy_companion("/api/runpod/trigger_uploads", await request.read())
    return web.json_response(data, status=status)


@routes.post("/runpod/upload_plan")
async def api_upload_plan(request):
    data, status = _proxy_companion("/api/runpod/upload_plan", await request.read())
    return web.json_response(data, status=status)


# ─── Register routes ──────────────────────────────────────────────────────────

try:
    if hasattr(PromptServer, "instance"):
        # .router.frozen check matches ComfyUI-KJNodes pattern
        router_frozen = getattr(PromptServer.instance.app.router, "frozen", False)
        if not router_frozen:
            PromptServer.instance.app.add_routes(routes)
            print("[ComfyUI-RunPod-Remote] Routes registered ✓")
        else:
            print("[ComfyUI-RunPod-Remote] WARNING: router already frozen, routes not registered")
except Exception:
    traceback.print_exc()
    print("[ComfyUI-RunPod-Remote] WARNING: failed to register routes")


# ─── ComfyUI node: RunPodSystemInfo ─────────────────────────────────────────
# This node runs ON THE RUNPOD WORKER and returns manifest data.
# It scans installed custom_nodes dirs and model files.

class RunPodSystemInfo:
    """
    Scans the worker environment and returns a JSON manifest of:
    - installed custom_nodes (directory names)
    - available model files (relative paths under /comfyui/models or ./models)

    Run this node on RunPod via the "Scan RunPod" button to populate the
    manifest cache used by the preflight check.
    """

    CATEGORY = "RunPod/Utilities"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("manifest_json",)
    FUNCTION = "scan"
    OUTPUT_NODE = True

    def scan(self):
        import os
        from pathlib import Path as P

        # Possible ComfyUI roots on worker-comfyui images
        possible_roots = [
            P("/comfyui"),
            P("/workspace/ComfyUI"),
            P(os.environ.get("COMFYUI_PATH", "/comfyui")),
            P(__file__).parent.parent.parent,  # custom_nodes/../..
        ]

        comfy_root = None
        for root in possible_roots:
            if (root / "models").exists() and (root / "custom_nodes").exists():
                comfy_root = root
                break

        custom_nodes = []
        models = []

        if comfy_root:
            # Scan custom_nodes
            cn_dir = comfy_root / "custom_nodes"
            if cn_dir.exists():
                custom_nodes = [
                    d.name for d in cn_dir.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ]

            # Scan models recursively
            models_dir = comfy_root / "models"
            model_exts = {".safetensors", ".ckpt", ".pt", ".bin", ".pth", ".gguf"}
            if models_dir.exists():
                for f in models_dir.rglob("*"):
                    if f.is_file() and f.suffix.lower() in model_exts:
                        try:
                            models.append(str(f.relative_to(models_dir)))
                        except ValueError:
                            models.append(f.name)

        manifest = {
            "custom_nodes": sorted(custom_nodes),
            "models": sorted(models),
            "comfy_root": str(comfy_root) if comfy_root else "unknown",
        }

        manifest_json = json.dumps(manifest, indent=2)
        return {"ui": {"manifest": [manifest]}, "result": (manifest_json,)}


# ─── Node registration ────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "RunPodSystemInfo": RunPodSystemInfo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RunPodSystemInfo": "RunPod System Info (Scan)",
}

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
