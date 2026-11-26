"""Submit and retrieve the production Responses requests as one replay batch."""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import review
from inputs import LOGGER, read_json, write_json
from spend import CAP, MAX_GRADE_BYTES, reserve, token_bound, upper_cost

API = "https://api.openai.com/v1"
TERMINAL = {"completed", "failed", "expired", "cancelled"}
RESPONSE_PATH = "/v1/responses"
EXPIRY = {"anchor": "created_at", "seconds": 172_800}
MAX_RESPONSE_BYTES = 64_000_000
KEY_PLACEHOLDER = "fnox-placeholder-openai-review"


def api_key():
    key = os.environ.get("OPENAI_REVIEW_API_KEY")
    if key != KEY_PLACEHOLDER or not os.environ.get("HTTPS_PROXY"):
        raise RuntimeError(
            "Start paid replay under the checkout's api-review fnox proxy."
        )
    return key


def api(path, key, *, data=None, content_type="application/json"):
    request = urllib.request.Request(
        API + path,
        data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
    )
    try:
        with urllib.request.build_opener(review.NoRedirect()).open(
            request, timeout=90
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"OpenAI replay request got HTTP {exc.code}{review.error_type(exc)}."
        ) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("OpenAI replay response exceeded the download limit.")
    return raw


def prepare(rows, schema):
    requests = []
    for row in rows:
        if "data" not in row:
            continue

        def capture(_url, _key, *, body, row_id=row["id"]):
            requests.append(
                {
                    "custom_id": row_id,
                    "method": "POST",
                    "url": RESPONSE_PATH,
                    "body": body,
                }
            )
            return {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"findings":[]}'}],
                    }
                ],
            }

        review.model_review("openai", "", row["data"], capture, schema)
    return requests


def estimate(requests, schema, triage_max_bytes):
    if review.MODEL != "gpt-6.2-sol":
        raise RuntimeError("Review model changed. Verify prices before running replay.")
    review_maximum = sum(
        upper_cost(
            token_bound(row["body"]), row["body"]["max_output_tokens"], batch=True
        )
        for row in requests
    )
    candidate_limit = schema["properties"]["findings"]["maxItems"]
    # All candidates could require grading, even if production rejects them.
    decisions_maximum = len(requests) * (
        upper_cost(triage_max_bytes + 2_048)
        + candidate_limit * upper_cost(MAX_GRADE_BYTES + 2_048)
    )
    total = review_maximum + decisions_maximum
    if total > CAP:
        raise RuntimeError(
            f"Pre-submit replay estimate ${total / 1e6:.4f} exceeds the $15 cap."
        )
    return {"batch": review_maximum, "decisions": decisions_maximum, "total": total}


def upload(key, requests):
    boundary = uuid.uuid4().hex
    prefix = []
    fields = {
        "purpose": "batch",
        **{f"expires_after[{k}]": v for k, v in EXPIRY.items()},
    }
    for name, value in fields.items():
        prefix.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        )
    prefix.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="replay.jsonl"\r\n'
        "Content-Type: application/jsonl\r\n\r\n"
    )
    data = (
        "".join(prefix)
        + "".join(json.dumps(row) + "\n" for row in requests)
        + f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")
    return json.loads(
        api(
            "/files",
            key,
            data=data,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
    )["id"]


def checked_id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(
        prefix + r"[A-Za-z0-9_-]{1,100}", value
    ):
        raise RuntimeError("OpenAI returned an invalid replay identifier.")
    return value


def request_digest(requests):
    ordered = sorted(requests, key=lambda row: row["custom_id"])
    return hashlib.sha256(
        json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def existing_batch(cache, requests):
    receipt = read_json(cache / "batch.json")
    if receipt["request_digest"] != request_digest(requests):
        raise RuntimeError(
            "The prepared inputs differ from the submitted Batch requests."
        )
    return checked_id(receipt["id"], "batch_")


def submit(cache: Path, key, requests, estimate_row):
    receipt = cache / "batch.json"
    if receipt.exists():
        return existing_batch(cache, requests)
    if not requests:
        raise RuntimeError("No PR passed the production patch checks.")
    reserve(cache, "batch", estimate_row["batch"])
    file_id = checked_id(upload(key, requests), "file-")
    write_json(cache / "batch-upload.json", {"id": file_id})
    payload = {
        "input_file_id": file_id,
        "endpoint": RESPONSE_PATH,
        "completion_window": "24h",
        "output_expires_after": EXPIRY,
    }
    response = json.loads(
        api("/batches", key, data=json.dumps(payload).encode("utf-8"))
    )
    identifier = checked_id(response["id"], "batch_")
    write_json(receipt, {"id": identifier, "request_digest": request_digest(requests)})
    LOGGER.info("batch_submitted", requests=len(requests), batch_id=identifier)
    return identifier


def fetch(cache, key, identifier):
    path = cache / "batch-results.json"
    if path.exists():
        return read_json(path)
    deadline = time.monotonic() + 86_700
    while True:
        batch = json.loads(api(f"/batches/{identifier}", key))
        status = batch["status"]
        LOGGER.info("batch_status", status=status, counts=batch.get("request_counts"))
        if status in TERMINAL:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Batch polling timed out. Resume using the same replay cache."
            )
        time.sleep(30)
    results = {}
    for kind in ("output_file_id", "error_file_id"):
        if raw_id := batch.get(kind):
            file_id = checked_id(raw_id, "file-")
            output = api(f"/files/{file_id}/content", key)
            for line in output.splitlines():
                entry = json.loads(line)
                row_id = entry["custom_id"]
                if row_id in results:
                    raise RuntimeError("OpenAI returned a duplicate Batch row.")
                results[row_id] = entry
    write_json(path, {"status": status, "results": results})
    return read_json(path)
