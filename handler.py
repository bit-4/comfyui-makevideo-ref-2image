"""RunPod Serverless handler for the two-reference MiniMax H3 workflow.

Install this file at the repository root of runpod-workers/worker-comfyui,
replacing its stock handler.py. The ComfyUI API-format workflow must be baked
into the image at WORKFLOW_PATH (default: /workflows/makevideo-api.json).

Simplified request:
{
  "input": {
    "person_image": "https://example.com/person.jpg",
    "style_image": "https://example.com/background.jpg",
    "prompt": "自拍，摆POSE，向镜头微笑。"
  }
}

The stock worker request shape (input.workflow + input.images) is also accepted.
"""

from __future__ import annotations

import base64
import copy
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import socket
import tempfile
import time
from typing import Any
from urllib.parse import urlparse
import uuid

try:
    import requests
except ModuleNotFoundError:  # Allows dependency-free unit tests of pure mapping logic.
    requests = None


def _require_requests():
    if requests is None:
        raise RuntimeError(
            "The 'requests' package is required at runtime. "
            "Use the official worker-comfyui requirements.txt."
        )
    return requests


COMFY_HOST = os.getenv("COMFY_HOST", "127.0.0.1:8188")
COMFY_BASE_URL = f"http://{COMFY_HOST}"
WORKFLOW_PATH = Path(os.getenv("WORKFLOW_PATH", "/workflows/makevideo-api.json"))

PERSON_NODE_TITLE = os.getenv("PERSON_NODE_TITLE", "RUNPOD_PERSON_IMAGE")
STYLE_NODE_TITLE = os.getenv("STYLE_NODE_TITLE", "RUNPOD_STYLE_IMAGE")
PROMPT_NODE_TITLE = os.getenv("PROMPT_NODE_TITLE", "RUNPOD_H3_REFERENCE")

DEFAULT_WIDTH = int(os.getenv("DEFAULT_WIDTH", "480"))
DEFAULT_HEIGHT = int(os.getenv("DEFAULT_HEIGHT", "848"))
DEFAULT_LENGTH = int(os.getenv("DEFAULT_LENGTH", "192"))

DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "60"))
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(15 * 1024 * 1024)))
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "2"))
COMFY_TIMEOUT_SECONDS = int(os.getenv("COMFY_TIMEOUT_SECONDS", "1800"))

OUTPUT_KEYS = ("images", "videos", "gifs", "audio", "files")
PROMPT_PLACEHOLDER = (
    "请在 RunPod 请求中替换此段，描述人物动作、表情、镜头运动和时间轴。"
)


def validate_simplified_input(job_input: Any) -> tuple[dict[str, str] | None, str | None]:
    """Validate the friendly three-field request."""
    if not isinstance(job_input, dict):
        return None, "input must be a JSON object"

    missing = [
        key
        for key in ("person_image", "style_image", "prompt")
        if not isinstance(job_input.get(key), str) or not job_input[key].strip()
    ]
    if missing:
        return None, f"Missing or empty required fields: {', '.join(missing)}"

    return {
        "person_image": job_input["person_image"].strip(),
        "style_image": job_input["style_image"].strip(),
        "prompt": job_input["prompt"].strip(),
    }, None


def _node_by_title(workflow: dict[str, Any], title: str) -> dict[str, Any]:
    matches = [
        node
        for node in workflow.values()
        if isinstance(node, dict)
        and isinstance(node.get("_meta"), dict)
        and node["_meta"].get("title") == title
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one API-workflow node titled {title!r}; found {len(matches)}. "
            "Import the UI workflow in ComfyUI and export it with Workflow > Export (API)."
        )
    return matches[0]


def _merge_prompt(template_prompt: Any, action_prompt: str) -> str:
    """Keep the baked workflow constraints when its placeholder is present."""
    if isinstance(template_prompt, str) and PROMPT_PLACEHOLDER in template_prompt:
        return template_prompt.replace(PROMPT_PLACEHOLDER, action_prompt)
    return action_prompt


def prepare_simplified_workflow(
    workflow_template: dict[str, Any],
    *,
    person_name: str,
    style_name: str,
    prompt: str,
) -> dict[str, Any]:
    """Clone and populate an API-format ComfyUI workflow."""
    workflow = copy.deepcopy(workflow_template)
    person_node = _node_by_title(workflow, PERSON_NODE_TITLE)
    style_node = _node_by_title(workflow, STYLE_NODE_TITLE)
    prompt_node = _node_by_title(workflow, PROMPT_NODE_TITLE)

    for title, node, image_name in (
        (PERSON_NODE_TITLE, person_node, person_name),
        (STYLE_NODE_TITLE, style_node, style_name),
    ):
        inputs = node.setdefault("inputs", {})
        if "image" not in inputs:
            raise ValueError(f"Node {title!r} has no 'image' input")
        inputs["image"] = image_name

    inputs = prompt_node.setdefault("inputs", {})
    if "prompt" not in inputs:
        raise ValueError(f"Node {PROMPT_NODE_TITLE!r} has no 'prompt' input")
    inputs["prompt"] = _merge_prompt(inputs.get("prompt"), prompt)

    if "width" in inputs:
        inputs["width"] = DEFAULT_WIDTH
    if "height" in inputs:
        inputs["height"] = DEFAULT_HEIGHT
    if "length" in inputs:
        inputs["length"] = DEFAULT_LENGTH
    return workflow


def load_workflow_template() -> dict[str, Any]:
    if not WORKFLOW_PATH.is_file():
        raise ValueError(
            f"API workflow not found: {WORKFLOW_PATH}. "
            "Export it from ComfyUI with Workflow > Export (API) and bake it into the image."
        )
    try:
        workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read API workflow {WORKFLOW_PATH}: {exc}") from exc
    if not isinstance(workflow, dict) or not workflow:
        raise ValueError("API workflow must be a non-empty JSON object")
    return workflow


def _validate_remote_url(url: str) -> None:
    parsed = urlparse(url)
    allowed_schemes = {"https"}
    if os.getenv("ALLOW_HTTP_IMAGE_URLS", "false").lower() == "true":
        allowed_schemes.add("http")
    if parsed.scheme.lower() not in allowed_schemes or not parsed.hostname:
        raise ValueError("Image URL must be an absolute HTTPS URL")

    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve image host {parsed.hostname!r}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError(f"Image URL resolves to a non-public address: {ip}")


def download_image(url: str) -> tuple[bytes, str, str]:
    """Download a public image with a size limit; return bytes, MIME type, suffix."""
    _validate_remote_url(url)
    http = _require_requests()
    with http.get(
        url,
        stream=True,
        timeout=(10, DOWNLOAD_TIMEOUT_SECONDS),
        allow_redirects=True,
    ) as response:
        response.raise_for_status()
        _validate_remote_url(response.url)
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if not mime_type.startswith("image/"):
            raise ValueError(f"URL did not return an image: content-type={mime_type!r}")
        declared_size = int(response.headers.get("content-length", "0") or 0)
        if declared_size > MAX_IMAGE_BYTES:
            raise ValueError(f"Image exceeds {MAX_IMAGE_BYTES} byte limit")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise ValueError(f"Image exceeds {MAX_IMAGE_BYTES} byte limit")
            chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise ValueError("Downloaded image is empty")
    suffix = mimetypes.guess_extension(mime_type) or ".png"
    if suffix == ".jpe":
        suffix = ".jpg"
    return data, mime_type, suffix


def decode_input_image(image: str) -> tuple[bytes, str]:
    """Decode the official worker's base64/data-URI image value."""
    if not isinstance(image, str) or not image:
        raise ValueError("Image payload must be a non-empty base64 string")
    mime_type = "application/octet-stream"
    payload = image
    if image.startswith("data:"):
        header, separator, payload = image.partition(",")
        if not separator or ";base64" not in header:
            raise ValueError("Only base64 data URIs are supported")
        mime_type = header[5:].split(";", 1)[0] or mime_type
    try:
        return base64.b64decode(payload, validate=True), mime_type
    except ValueError as exc:
        raise ValueError("Invalid base64 image payload") from exc


def wait_for_comfyui() -> None:
    http = _require_requests()
    deadline = time.monotonic() + min(COMFY_TIMEOUT_SECONDS, 300)
    last_error = "not started"
    while time.monotonic() < deadline:
        try:
            response = http.get(f"{COMFY_BASE_URL}/system_stats", timeout=5)
            if response.ok:
                return
            last_error = f"HTTP {response.status_code}"
        except http.RequestException as exc:
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(f"ComfyUI is not reachable at {COMFY_BASE_URL}: {last_error}")


def upload_image_to_comfyui(
    data: bytes, *, filename: str, mime_type: str
) -> str:
    http = _require_requests()
    response = http.post(
        f"{COMFY_BASE_URL}/upload/image",
        files={"image": (filename, data, mime_type)},
        data={"type": "input", "overwrite": "true"},
        timeout=120,
    )
    response.raise_for_status()
    result = response.json()
    name = result.get("name")
    if not name:
        raise RuntimeError(f"ComfyUI upload returned no filename: {result}")
    subfolder = result.get("subfolder", "")
    return f"{subfolder}/{name}" if subfolder else name


def queue_workflow(workflow: dict[str, Any], client_id: str) -> str:
    http = _require_requests()
    response = http.post(
        f"{COMFY_BASE_URL}/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=60,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"ComfyUI rejected workflow: HTTP {response.status_code}: {response.text[:4000]}"
        )
    prompt_id = response.json().get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI returned no prompt_id: {response.text[:1000]}")
    return prompt_id


def wait_for_history(prompt_id: str) -> dict[str, Any]:
    http = _require_requests()
    deadline = time.monotonic() + COMFY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = http.get(
            f"{COMFY_BASE_URL}/history/{prompt_id}", timeout=30
        )
        response.raise_for_status()
        history = response.json()
        if prompt_id in history:
            item = history[prompt_id]
            status = item.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI execution failed: {status}")
            if item.get("outputs"):
                return item
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"ComfyUI job {prompt_id} exceeded {COMFY_TIMEOUT_SECONDS}s")


def collect_output_files(prompt_history: dict[str, Any]) -> list[dict[str, str]]:
    """Collect image, video, gif, audio, or generic file descriptors."""
    files: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    outputs = prompt_history.get("outputs", {})
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        ordered_keys = list(OUTPUT_KEYS) + [
            key for key in node_output if key not in OUTPUT_KEYS
        ]
        for key in ordered_keys:
            values = node_output.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict) or not value.get("filename"):
                    continue
                item = {
                    "filename": value["filename"],
                    "subfolder": value.get("subfolder", ""),
                    "type": value.get("type", "output"),
                    "output_key": key,
                }
                identity = (item["filename"], item["subfolder"], item["type"])
                if identity not in seen and item["type"] != "temp":
                    seen.add(identity)
                    files.append(item)
    return files


def fetch_output_file(descriptor: dict[str, str]) -> bytes:
    http = _require_requests()
    response = http.get(
        f"{COMFY_BASE_URL}/view",
        params={
            "filename": descriptor["filename"],
            "subfolder": descriptor["subfolder"],
            "type": descriptor["type"],
        },
        timeout=300,
    )
    response.raise_for_status()
    return response.content


def publish_output(job_id: str, descriptor: dict[str, str]) -> dict[str, Any]:
    data = fetch_output_file(descriptor)
    if not data:
        raise RuntimeError(f"Output file is empty: {descriptor['filename']}")
    extension = Path(descriptor["filename"]).suffix.lower()
    media_type = mimetypes.guess_type(descriptor["filename"])[0] or "application/octet-stream"

    if os.getenv("BUCKET_ENDPOINT_URL"):
        from runpod.serverless.utils import rp_upload

        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as temp_file:
                temp_file.write(data)
                temporary_path = temp_file.name
            url = rp_upload.upload_image(job_id, temporary_path)
            return {
                "filename": descriptor["filename"],
                "type": "s3_url",
                "data": url,
                "media_type": media_type,
                "output_key": descriptor["output_key"],
            }
        finally:
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)

    return {
        "filename": descriptor["filename"],
        "type": "base64",
        "data": base64.b64encode(data).decode("ascii"),
        "media_type": media_type,
        "output_key": descriptor["output_key"],
    }


def _upload_official_images(images: Any) -> None:
    if images is None:
        return
    if not isinstance(images, list):
        raise ValueError("images must be an array")
    for image in images:
        if not isinstance(image, dict) or not image.get("name") or not image.get("image"):
            raise ValueError("Each images item requires name and image")
        data, mime_type = decode_input_image(image["image"])
        upload_image_to_comfyui(data, filename=image["name"], mime_type=mime_type)


def _prepare_job(job: dict[str, Any]) -> tuple[dict[str, Any], str]:
    job_input = job.get("input")
    if not isinstance(job_input, dict):
        raise ValueError("Missing input object")

    if "workflow" in job_input:
        workflow = job_input["workflow"]
        if not isinstance(workflow, dict) or not workflow:
            raise ValueError("workflow must be a non-empty API-format object")
        _upload_official_images(job_input.get("images"))
        return copy.deepcopy(workflow), "official"

    simplified, error = validate_simplified_input(job_input)
    if error:
        raise ValueError(error)
    assert simplified is not None

    job_id = str(job.get("id") or uuid.uuid4())
    person_bytes, person_mime, person_suffix = download_image(
        simplified["person_image"]
    )
    style_bytes, style_mime, style_suffix = download_image(
        simplified["style_image"]
    )
    person_name = upload_image_to_comfyui(
        person_bytes,
        filename=f"requests/{job_id}/person{person_suffix}",
        mime_type=person_mime,
    )
    style_name = upload_image_to_comfyui(
        style_bytes,
        filename=f"requests/{job_id}/style{style_suffix}",
        mime_type=style_mime,
    )
    workflow = prepare_simplified_workflow(
        load_workflow_template(),
        person_name=person_name,
        style_name=style_name,
        prompt=simplified["prompt"],
    )
    return workflow, "simplified"


def handler(job: dict[str, Any]) -> dict[str, Any]:
    """RunPod entry point."""
    try:
        wait_for_comfyui()
        workflow, request_mode = _prepare_job(job)
        client_id = str(uuid.uuid4())
        prompt_id = queue_workflow(workflow, client_id)
        history = wait_for_history(prompt_id)
        descriptors = collect_output_files(history)
        if not descriptors:
            return {
                "error": "Workflow completed but produced no supported output files",
                "prompt_id": prompt_id,
            }
        files = [publish_output(str(job.get("id", prompt_id)), item) for item in descriptors]
        videos = [
            item
            for item in files
            if item["media_type"].startswith("video/")
            or Path(item["filename"]).suffix.lower() in {".mp4", ".webm", ".mov", ".mkv"}
        ]
        result: dict[str, Any] = {
            "status": "success",
            "request_mode": request_mode,
            "prompt_id": prompt_id,
            "files": files,
        }
        if videos:
            result["video"] = videos[0]
            result["videos"] = videos
        return result
    except Exception as exc:
        return {"error": str(exc), "error_type": type(exc).__name__}


if __name__ == "__main__":
    import runpod

    print("worker-comfyui-custom - Starting handler")
    runpod.serverless.start({"handler": handler})
