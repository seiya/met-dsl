from __future__ import annotations

import json
import hashlib
from typing import Any, Mapping, Union

from .models import EmissionConfig


def _as_primitive(config: Union[EmissionConfig, Mapping[str, Any]]) -> Mapping[str, Any]:
    if isinstance(config, EmissionConfig):
        return json.loads(config.json())
    return config


def compute_config_hash(config: Union[EmissionConfig, Mapping[str, Any]]) -> str:
    """
    Compute a deterministic SHA256 fingerprint for a configuration payload.

    The input may be a pydantic model or a mapping. Keys are sorted to ensure stable hashes.
    """
    primitive = _as_primitive(config)
    payload = json.dumps(primitive, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["compute_config_hash"]
