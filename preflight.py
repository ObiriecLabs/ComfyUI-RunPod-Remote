"""
Preflight check: verify that all nodes and models required by a workflow
are present on the RunPod endpoint, before submitting the job.

Manifest format (manifest_cache.json):
{
    "custom_nodes": ["WanVideoWrapper", "KJNodes", ...],   # directory names
    "models": ["checkpoints/juggernaut.safetensors", ...], # relative paths
    "scanned_at": "2026-06-12T14:30:00"
}

Node → package mapping (partial, extendable):
Maps ComfyUI class_type → custom_node directory that provides it.
Only entries for KNOWN non-builtin nodes. If a class_type is not in the map,
it is assumed to be a ComfyUI builtin and always considered present.
"""

import json
import os
from pathlib import Path
from typing import Optional

MANIFEST_PATH = Path(__file__).parent / "manifest_cache.json"

# Model-like input suffixes
MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".bin", ".pth", ".gguf"}

# class_type → custom_node_folder (lowercase match)
# Extend this as needed. Missing entries are treated as builtins (always OK).
NODE_PACKAGE_MAP: dict[str, str] = {
    # WanVideoWrapper
    "WanVideoModelLoader": "WanVideoWrapper",
    "WanVideoTextEncode": "WanVideoWrapper",
    "WanVideoSampler": "WanVideoWrapper",
    "WanVideoVAEDecode": "WanVideoWrapper",
    "WanVideoVAEEncode": "WanVideoWrapper",
    # KJNodes
    "KJLoadVideo": "ComfyUI-KJNodes",
    "GetNode": "ComfyUI-KJNodes",
    "SetNode": "ComfyUI-KJNodes",
    "NormalizeImageBatch": "ComfyUI-KJNodes",
    "ResizeMask": "ComfyUI-KJNodes",
    "FloatConstant": "ComfyUI-KJNodes",
    "IntConstant": "ComfyUI-KJNodes",
    "StringConstant": "ComfyUI-KJNodes",
    "ConditioningMultiCombine": "ComfyUI-KJNodes",
    "SplitBatchIndex": "ComfyUI-KJNodes",
    "CreateFluidMask": "ComfyUI-KJNodes",
    "BatchCropFromMask": "ComfyUI-KJNodes",
    # VideoHelperSuite
    "VHS_VideoCombine": "ComfyUI-VideoHelperSuite",
    "VHS_LoadVideo": "ComfyUI-VideoHelperSuite",
    "VHS_LoadImages": "ComfyUI-VideoHelperSuite",
    "VHS_GetLatentCount": "ComfyUI-VideoHelperSuite",
    # AnimateDiff
    "ADE_AnimateDiffLoaderWithContext": "ComfyUI-AnimateDiff-Evolved",
    "ADE_StandardUniformContextOptions": "ComfyUI-AnimateDiff-Evolved",
    # Impact Pack
    "FaceDetailer": "ComfyUI-Impact-Pack",
    "SAMLoader": "ComfyUI-Impact-Pack",
    "UltralyticsDetectorProvider": "ComfyUI-Impact-Pack",
    # ControlNet aux
    "AnyLineArtPreprocessor": "comfyui_controlnet_aux",
    "DWPreprocessor": "comfyui_controlnet_aux",
    "MiDaSDepthMapPreprocessor": "comfyui_controlnet_aux",
    "CannyEdgePreprocessor": "comfyui_controlnet_aux",
    # GGUF
    "UnetLoaderGGUF": "ComfyUI-GGUF",
    "CLIPLoaderGGUF": "ComfyUI-GGUF",
    # LTXVideo
    "LTXVModelLoader": "ComfyUI-LTXVideo",
    "LTXVSampler": "ComfyUI-LTXVideo",
    # HunyuanVideo
    "HyVideoModelLoader": "ComfyUI-HunyuanVideoWrapper",
    "HyVideoSampler": "ComfyUI-HunyuanVideoWrapper",
}

# ComfyUI builtin class_types (never need a custom node)
# We do a prefix-based check — if unknown → assume builtin (safe default)
BUILTIN_PREFIXES = (
    "KSampler", "CheckpointLoader", "CLIPTextEncode", "VAE", "LoraLoader",
    "ControlNetLoader", "ControlNetApply", "ImageScale", "LatentUpscale",
    "EmptyLatent", "Save", "Preview", "Load", "Note", "PrimitiveNode",
    "Reroute", "UpscaleModel", "ImageUpscale", "Mask", "CR ", "easy ",
    "efficiency", "CLIPSetLastLayer", "CLIPVision", "unCLIP",
)


# ─── Manifest helpers ─────────────────────────────────────────────────────────

def load_manifest() -> Optional[dict]:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except Exception:
            return None
    return None


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


def manifest_age_hours() -> Optional[float]:
    """Returns hours since last scan, or None if no manifest."""
    m = load_manifest()
    if not m or "scanned_at" not in m:
        return None
    from datetime import datetime, timezone
    try:
        scanned = datetime.fromisoformat(m["scanned_at"]).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - scanned).total_seconds() / 3600
    except Exception:
        return None


# ─── Workflow analysis ────────────────────────────────────────────────────────

def extract_requirements(workflow_api: dict) -> dict:
    """
    Given a workflow in API format (dict of node_id → {class_type, inputs}),
    return:
      {
        "class_types": set of all class_type values,
        "models": set of model filenames / relative paths found in inputs,
        "unknown_nodes": set of class_types not in NODE_PACKAGE_MAP and not builtin,
        "required_packages": set of custom_node folders needed
      }
    """
    class_types: set[str] = set()
    models: set[str] = set()

    for node in workflow_api.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type", "")
        if ct:
            class_types.add(ct)
        for val in node.get("inputs", {}).values():
            if isinstance(val, str) and Path(val).suffix.lower() in MODEL_EXTENSIONS:
                models.add(val)

    required_packages: set[str] = set()
    unknown_nodes: set[str] = set()

    for ct in class_types:
        if ct in NODE_PACKAGE_MAP:
            required_packages.add(NODE_PACKAGE_MAP[ct])
        elif not any(ct.startswith(p) for p in BUILTIN_PREFIXES):
            unknown_nodes.add(ct)

    return {
        "class_types": class_types,
        "models": models,
        "unknown_nodes": unknown_nodes,
        "required_packages": required_packages,
    }


def check_against_manifest(reqs: dict, manifest: dict) -> dict:
    """
    Compare requirements against manifest. Returns:
      {
        "ok": bool,
        "missing_nodes": list,
        "missing_models": list,
        "unknown_nodes": list,   # nodes not in mapping — cannot verify
        "warnings": list
      }
    """
    installed_nodes = set(manifest.get("custom_nodes", []))
    installed_models = set(m.lower() for m in manifest.get("models", []))

    missing_nodes = []
    for pkg in reqs["required_packages"]:
        # Case-insensitive match on the package folder name
        if pkg.lower() not in {n.lower() for n in installed_nodes}:
            missing_nodes.append(pkg)

    missing_models = []
    for model in reqs["models"]:
        # Match by basename or full relative path
        basename = Path(model).name.lower()
        if (model.lower() not in installed_models and
                not any(Path(m).name.lower() == basename for m in installed_models)):
            missing_models.append(model)

    unknown_nodes = list(reqs["unknown_nodes"])

    warnings = []
    if unknown_nodes:
        warnings.append(
            f"{len(unknown_nodes)} node(s) could not be verified "
            f"(not in mapping): {', '.join(unknown_nodes)}"
        )

    ok = len(missing_nodes) == 0 and len(missing_models) == 0

    return {
        "ok": ok,
        "missing_nodes": missing_nodes,
        "missing_models": missing_models,
        "unknown_nodes": unknown_nodes,
        "warnings": warnings,
    }


def run_preflight(workflow_api: dict) -> dict:
    """
    Full preflight check. Returns a result dict suitable for JSON response.
    """
    reqs = extract_requirements(workflow_api)

    manifest = load_manifest()
    if not manifest:
        return {
            "ok": False,
            "manifest_missing": True,
            "missing_nodes": [],
            "missing_models": [],
            "unknown_nodes": list(reqs["unknown_nodes"]),
            "warnings": ["No manifest found. Run 'Scan RunPod' first."],
            "requirements": {
                "class_types": list(reqs["class_types"]),
                "models": list(reqs["models"]),
                "required_packages": list(reqs["required_packages"]),
            },
        }

    result = check_against_manifest(reqs, manifest)
    result["manifest_missing"] = False
    result["requirements"] = {
        "class_types": list(reqs["class_types"]),
        "models": list(reqs["models"]),
        "required_packages": list(reqs["required_packages"]),
    }
    age = manifest_age_hours()
    if age is not None and age > 24:
        result["warnings"].append(
            f"Manifest is {age:.0f}h old. Consider rescanning RunPod."
        )
    result["manifest_scanned_at"] = manifest.get("scanned_at", "unknown")
    return result
