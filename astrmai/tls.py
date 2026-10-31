"""Pin outbound TLS to <= 1.2.

The chosen OpenAI-compatible relay hangs on Python's OpenSSL 3.5 TLS 1.3 handshake on this
machine while Schannel (curl) negotiates it fine. Both frameworks reach models through
httpx/stdlib default contexts, so the cap is applied once at host boot instead of per call.
"""

import ssl

_TARGET = ssl.TLSVersion.TLSv1_2
_MARKER = "_astrmai_tls_capped"


def _cap(ctx: ssl.SSLContext) -> ssl.SSLContext:
    try:
        ctx.maximum_version = _TARGET
    except (AttributeError, ValueError):
        pass
    return ctx


def apply() -> bool:
    """Wrap the default context factories. Idempotent; returns True when the cap holds."""

    if not getattr(ssl, _MARKER, False):
        original = ssl.create_default_context

        def patched(*args, **kwargs) -> ssl.SSLContext:
            return _cap(original(*args, **kwargs))

        ssl.create_default_context = patched  # type: ignore[method-assign]

        try:
            import httpx

            original_httpx = httpx.create_ssl_context

            def patched_httpx(*args, **kwargs) -> ssl.SSLContext:
                return _cap(original_httpx(*args, **kwargs))

            httpx.create_ssl_context = patched_httpx  # type: ignore[attr-defined]
        except (ImportError, AttributeError):
            pass

        setattr(ssl, _MARKER, True)

    return _cap(ssl.create_default_context()).maximum_version == _TARGET
