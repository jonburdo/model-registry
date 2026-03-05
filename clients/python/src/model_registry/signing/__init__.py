"""Signing utilities for model registry."""

import logging

# Shared parent logger for all signing components, independent of root logger
logger = logging.getLogger("model_registry.signing")
logger.propagate = False
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d - %(name)s:%(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

from model_registry.signing.config import SigningConfig
from model_registry.signing.exceptions import (
    BaseSigningError,
    InitializationError,
    SigningError,
    VerificationError,
)
from model_registry.signing.image_signer import CommandRunner, ImageSigner
from model_registry.signing.model_signer import ModelSigner
from model_registry.signing.signer import Signer
from model_registry.signing.token import decode_jwt_payload, extract_client_id
from model_registry.signing.trust_manager import TrustManager

__all__ = [
    "CommandRunner",
    "ImageSigner",
    "ModelSigner",
    "Signer",
    "SigningConfig",
    "TrustManager",
    "BaseSigningError",
    "InitializationError",
    "SigningError",
    "VerificationError",
    "decode_jwt_payload",
    "extract_client_id",
]
