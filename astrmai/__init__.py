"""astrmai — MaiBot's chat brain embedded inside AstrBot's platform layer."""

from .config import AstrmaiConfig
from .gate import Decision, decide
from .inbound import onebot_to_maim_dict, onebot_to_maim_dict_async
from .outbound import components_to_segments, resolve_target
from .tls import apply as apply_tls_cap

__all__ = [
    "AstrmaiConfig",
    "Decision",
    "apply_tls_cap",
    "components_to_segments",
    "decide",
    "onebot_to_maim_dict",
    "onebot_to_maim_dict_async",
    "resolve_target",
]
