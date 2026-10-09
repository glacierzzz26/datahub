"""Bearer token 鉴权依赖。

- 校验 `Authorization: Bearer <DATAHUB_TOKEN>`；
- token 缺失/错误/服务端未配置 → 401（**绝不放行空口令**）；
- 常量时间比较（hmac.compare_digest）防时序侧信道；
- `/healthz` 不走本依赖（探针免鉴权）。
"""
import hmac

from fastapi import Header, HTTPException, status

from app import config


def require_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI 依赖：校验 Bearer token，不通过抛 401。"""
    if not config.token_configured():
        # 服务端未配置 token：一律拒绝（fail-closed），不静默放行
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="server token not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization[len("Bearer "):].strip()
    if not hmac.compare_digest(presented, config.TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
