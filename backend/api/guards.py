from fastapi import HTTPException
from path_policy import vault_root_is_usable
from runtime_activation import active_vault_is_usable
from runtime_context import (
    RuntimeNotLoadedError,
    VaultContext,
    WorkspaceContext,
    get_runtime_context,
)


def require_workspace_context() -> WorkspaceContext:
    try:
        return get_runtime_context()
    except RuntimeNotLoadedError as exc:
        raise HTTPException(status_code=503, detail="Workspace not loaded") from exc

def require_usable_vault_context() -> VaultContext:
    ctx = require_workspace_context()
    if not active_vault_is_usable(ctx):
        raise HTTPException(status_code=503, detail="Active vault is offline or missing")
    return ctx.active_vault

def require_usable_target_vault_context(vault_id: str) -> VaultContext:
    ctx = require_workspace_context()

    from vaults import _ctx_for_vault, _vault_entry
    try:
        clean_id, entry, root = _vault_entry(vault_id, ctx)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Vault not found: {vault_id}") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not root or not vault_root_is_usable(root, ctx.root):
        raise HTTPException(status_code=503, detail=f"Target vault is offline or missing: {vault_id}")

    target_workspace_ctx = _ctx_for_vault(vault_id, ctx)
    return target_workspace_ctx.active_vault
