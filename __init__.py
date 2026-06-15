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
    "endpoint_id": "a007azjm8d8r4k",
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
    """Proxy-check if ARIA2 Downloader is running at localhost:7891."""
    import urllib.request as ureq
    try:
        with ureq.urlopen("http://localhost:7891/api/runpod/status", timeout=3) as resp:
            data = json.loads(resp.read())
            return web.json_response({"available": True, "pod_status": data.get("status", "unknown")})
    except Exception:
        return web.json_response({"available": False})


@routes.post("/runpod/scan_ssh")
async def api_scan_ssh(request):
    """
    Auto-scan the running RunPod worker via SSH.
    1. Query RunPod GraphQL API for pods with SSH exposed
    2. SSH in with ~/.runpod/ssh/runpodctl-ssh-key
    3. List /workspace/custom_nodes and find model files
    4. Save manifest_cache.json
    """
    import subprocess
    import urllib.request as ureq

    cfg = _load_config()
    api_key = cfg.get("api_key", "")
    if not api_key:
        return web.json_response({"error": "API key non configurata"}, status=400)

    # Query RunPod GraphQL for running pods with SSH
    gql_query = json.dumps({
        "query": "{ myself { pods { id name desiredStatus runtime { ports { ip isIpPublic privatePort publicPort type } } } } }"
    }).encode()
    gql_req = ureq.Request(
        f"https://api.runpod.io/graphql?api_key={api_key}",
        data=gql_query,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with ureq.urlopen(gql_req, timeout=15) as resp:
            gql_data = json.loads(resp.read())
    except Exception as e:
        return web.json_response({"error": f"RunPod API error: {e}"}, status=500)

    pods = gql_data.get("data", {}).get("myself", {}).get("pods", [])
    ssh_info = None
    for pod in pods:
        if pod.get("desiredStatus") == "RUNNING" and pod.get("runtime"):
            for port_info in pod["runtime"].get("ports", []):
                if port_info.get("privatePort") == 22 and port_info.get("isIpPublic"):
                    ssh_info = {
                        "pod_id": pod["id"],
                        "ip": port_info["ip"],
                        "port": port_info["publicPort"],
                    }
                    break
        if ssh_info:
            break

    if not ssh_info:
        return web.json_response({
            "error": "Nessun pod attivo con SSH trovato.",
            "pods_found": len(pods),
            "hint": "Avvia un pod con volume /workspace prima di scansionare.",
        }, status=400)

    # SSH and scan
    ssh_key = str(Path.home() / ".runpod" / "ssh" / "runpodctl-ssh-key")
    ssh_base = [
        "ssh", "-i", ssh_key,
        "-p", str(ssh_info["port"]),
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"root@{ssh_info['ip']}",
    ]

    def ssh_run(cmd):
        result = subprocess.run(ssh_base + [cmd], capture_output=True, text=True, timeout=30)
        return result.stdout.strip()

    try:
        custom_nodes_raw = ssh_run(
            "ls /workspace/custom_nodes/ 2>/dev/null || ls /comfyui/custom_nodes/ 2>/dev/null"
        )
        models_raw = ssh_run(
            r"find /workspace/models /comfyui/models 2>/dev/null"
            r" \( -name '*.safetensors' -o -name '*.ckpt' -o -name '*.gguf' -o -name '*.pt' \)"
            r" | sed 's|.*/models/||' | sort 2>/dev/null"
        )
    except subprocess.TimeoutExpired:
        return web.json_response({"error": "Timeout SSH. Il pod risponde ma SSH è lento."}, status=500)
    except Exception as e:
        return web.json_response({"error": f"SSH error: {e}"}, status=500)

    custom_nodes = [n for n in custom_nodes_raw.splitlines() if n and not n.startswith(".")]
    models = [m for m in models_raw.splitlines() if m and not m.startswith("find:")]

    # Check available disk space on /workspace
    try:
        disk_avail_raw = ssh_run(
            "df -B1 /workspace 2>/dev/null | awk 'NR==2{print $4}' || echo 0"
        )
        disk_human_raw = ssh_run(
            "df -h /workspace 2>/dev/null | awk 'NR==2{print $4}' || echo N/A"
        )
        disk_avail_bytes = int(disk_avail_raw.strip() or "0")
        disk_human = disk_human_raw.strip() or "N/A"
    except Exception:
        disk_avail_bytes = 0
        disk_human = "N/A"

    manifest = {
        "custom_nodes": sorted(set(custom_nodes)),
        "models": sorted(set(models)),
        "scanned_at": _now(),
        "scan_method": "ssh",
        "pod_id": ssh_info["pod_id"],
        "pod_ip": ssh_info["ip"],
        "disk": {
            "available_bytes": disk_avail_bytes,
            "available_human": disk_human,
        },
    }
    save_manifest(manifest)

    return web.json_response({
        "ok": True,
        "custom_nodes_count": len(manifest["custom_nodes"]),
        "models_count": len(manifest["models"]),
        "scanned_at": manifest["scanned_at"],
        "pod_id": ssh_info["pod_id"],
        "disk_available_human": disk_human,
        "disk_available_bytes": disk_avail_bytes,
    })


@routes.post("/runpod/trigger_uploads")
async def api_trigger_uploads(request):
    """
    Queue missing models and nodes in ARIA2 Downloader (localhost:7891).
    Body: {"missing_models": [...], "missing_nodes": [...]}
    Calls POST /api/runpod/upload for models, POST /api/runpod/node for nodes.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    missing_models = data.get("missing_models", [])
    missing_nodes = data.get("missing_nodes", [])

    ARIA2_BASE = "http://localhost:7891"
    import urllib.request as ureq

    # Check ARIA2 is reachable
    try:
        ureq.urlopen(ARIA2_BASE + "/api/runpod/status", timeout=4).read()
    except Exception:
        return web.json_response(
            {"error": "ARIA2 Downloader non raggiungibile su porta 7891. Aprilo prima."},
            status=400
        )

    # Locate local ComfyUI models directory
    local_roots = [
        Path("/Volumes/ComfyUI_6TB/ComfyUI/models"),
        Path.home() / "ComfyUI" / "models",
        Path("/workspace/ComfyUI/models"),
    ]
    local_models_root = next((r for r in local_roots if r.exists()), None)

    # Known GitHub repos for custom nodes
    NODE_GITHUB: dict[str, str] = {
        "WanVideoWrapper":              "https://github.com/kijai/ComfyUI-WanVideoWrapper",
        "ComfyUI-KJNodes":              "https://github.com/kijai/ComfyUI-KJNodes",
        "ComfyUI-VideoHelperSuite":     "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite",
        "ComfyUI-AnimateDiff-Evolved":  "https://github.com/Kosinkadink/ComfyUI-AnimateDiff-Evolved",
        "ComfyUI-Impact-Pack":          "https://github.com/ltdrdata/ComfyUI-Impact-Pack",
        "comfyui_controlnet_aux":       "https://github.com/Fannovel16/comfyui_controlnet_aux",
        "ComfyUI-GGUF":                 "https://github.com/city96/ComfyUI-GGUF",
        "ComfyUI-LTXVideo":             "https://github.com/Lightricks/ComfyUI-LTXVideo",
        "ComfyUI-HunyuanVideoWrapper":  "https://github.com/kijai/ComfyUI-HunyuanVideoWrapper",
        "ComfyUI-LTXVideo-Extra":       "https://github.com/ShmuelRonen/ComfyUI-LTXVideo-Extra",
        "ComfyUI-WanVideoWrapper":      "https://github.com/kijai/ComfyUI-WanVideoWrapper",
    }

    def aria2_post(path, body):
        payload = json.dumps(body).encode()
        req = ureq.Request(
            ARIA2_BASE + path, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with ureq.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    results: dict = {
        "queued_models": [],
        "queued_nodes": [],
        "not_found_locally": [],
        "unknown_node_repo": [],
        "errors": [],
    }

    # Queue missing models
    for model_rel in missing_models:
        if not local_models_root:
            results["not_found_locally"].append(model_rel)
            continue
        local_path = local_models_root / model_rel
        if not local_path.exists():
            results["not_found_locally"].append(model_rel)
            continue
        dest_subdir = str(Path(model_rel).parent)
        if dest_subdir == ".":
            dest_subdir = ""
        try:
            resp = aria2_post("/api/runpod/upload", {"path": str(local_path), "dest": dest_subdir})
            results["queued_models"].append({"model": model_rel, "response": resp})
        except Exception as e:
            results["errors"].append({"item": model_rel, "error": str(e)})

    # Queue missing custom nodes
    for node in missing_nodes:
        repo = NODE_GITHUB.get(node)
        if not repo:
            results["unknown_node_repo"].append(node)
            continue
        try:
            resp = aria2_post("/api/runpod/node", {"repo": repo})
            results["queued_nodes"].append({"node": node, "repo": repo, "response": resp})
        except Exception as e:
            results["errors"].append({"item": node, "error": str(e)})

    # Add size info to response
    total_queued_bytes = sum(
        (local_models_root / m).stat().st_size
        for m in missing_models
        if local_models_root and (local_models_root / m).exists()
    )
    manifest_d = load_manifest()
    disk_avail = (manifest_d or {}).get("disk", {}).get("available_bytes", 0)
    space_warning = None
    if disk_avail > 0 and total_queued_bytes > disk_avail * 0.85:
        space_warning = (
            f"Attenzione: stai caricando ~{_fmt_bytes(total_queued_bytes)} "
            f"ma sul volume RunPod ci sono solo ~{_fmt_bytes(disk_avail)} disponibili. "
            "Espandi il Network Volume su runpod.io prima di procedere."
        )

    results["ok"] = len(results["errors"]) == 0
    results["total_queued"] = len(results["queued_models"]) + len(results["queued_nodes"])
    results["estimated_upload_bytes"] = total_queued_bytes
    results["estimated_upload_human"] = _fmt_bytes(total_queued_bytes) if total_queued_bytes else "0 B"
    results["disk_available_bytes"] = disk_avail
    results["space_warning"] = space_warning
    return web.json_response(results)


@routes.post("/runpod/upload_plan")
async def api_upload_plan(request):
    """
    Dry-run estimate: calculates local file sizes and checks RunPod disk space.
    Body: {"missing_models": [...], "missing_nodes": [...]}
    Returns size estimates and space warning WITHOUT queuing anything.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    missing_models = data.get("missing_models", [])
    missing_nodes = data.get("missing_nodes", [])

    local_roots = [
        Path("/Volumes/ComfyUI_6TB/ComfyUI/models"),
        Path.home() / "ComfyUI" / "models",
        Path("/workspace/ComfyUI/models"),
    ]
    local_models_root = next((r for r in local_roots if r.exists()), None)

    items = []
    total_bytes = 0

    for m in missing_models:
        item: dict = {"type": "model", "name": m, "size_bytes": None, "found_locally": False}
        if local_models_root:
            lp = local_models_root / m
            if lp.exists():
                item["found_locally"] = True
                item["size_bytes"] = lp.stat().st_size
                total_bytes += item["size_bytes"]
        items.append(item)

    # Nodes: estimate 80 MB each (rough average for a custom node + deps)
    _NODE_SIZE_ESTIMATE = 80 * 1024 * 1024
    for n in missing_nodes:
        items.append({
            "type": "node", "name": n,
            "size_bytes": _NODE_SIZE_ESTIMATE,
            "found_locally": None,
        })
        total_bytes += _NODE_SIZE_ESTIMATE

    manifest_d = load_manifest()
    disk = (manifest_d or {}).get("disk", {})
    disk_avail = disk.get("available_bytes", 0)
    disk_human = disk.get("available_human", "N/A")

    space_ok = (disk_avail == 0) or (total_bytes <= disk_avail * 0.85)
    space_warning = None
    if not space_ok:
        space_warning = (
            f"Spazio insufficiente: servono ~{_fmt_bytes(total_bytes)}, "
            f"disponibili ~{_fmt_bytes(disk_avail)} su RunPod. "
            "Espandi il Network Volume su runpod.io → My Pods → Edit Pod → Storage."
        )

    return web.json_response({
        "items": items,
        "total_bytes": total_bytes,
        "total_human": _fmt_bytes(total_bytes) if total_bytes else "0 B",
        "disk_available_bytes": disk_avail,
        "disk_available_human": disk_human,
        "space_ok": space_ok,
        "space_warning": space_warning,
        "models_count": len(missing_models),
        "nodes_count": len(missing_nodes),
        "manifest_age_warning": (manifest_d is None),
    })


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
