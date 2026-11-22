"""Common scheduling protocol and domain adapters.

Execution is opt-in while legacy resources are being migrated. A configured
coordinator failure is fatal to admission; there is no local fallback queue.
"""
