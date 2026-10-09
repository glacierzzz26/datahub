"""服务入口：装配 FastAPI（HTTP 出口）+ 挂载 FastMCP（MCP 出口）+ /healthz。

- 请求层超时补丁**只在 main() 入口安装**（import 期不装 —— 会污染测试集）；
- HTTP 与 MCP 共享同一内核 service.get_dataset，零逻辑重复；
- 错误一律渲染成 {code,message,data} 信封（与成功响应同构）。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app import __version__, config, http_api
from app.mcp_facade import build_mcp

logger = logging.getLogger(__name__)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def _http_exc(_request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.status_code, "message": exc.detail, "data": None},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request, exc: Exception):
        logger.exception("未处理异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"code": 500, "message": "internal error", "data": None},
        )


def build_app() -> FastAPI:
    """构建 ASGI app（不安装请求层补丁 —— 那在 main() 入口做）。"""
    mcp = build_mcp()
    mcp_asgi = mcp.streamable_http_app()
    session_manager = mcp.session_manager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # MCP streamable session manager 需随应用生命周期启动
        async with session_manager.run():
            yield

    app = FastAPI(title="datahub", version=__version__, lifespan=lifespan)
    app.include_router(http_api.router)
    app.mount("/mcp", mcp_asgi)
    _install_error_handlers(app)
    return app


def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not config.token_configured():
        logger.warning("DATAHUB_TOKEN 未配置 —— 鉴权层将一律拒绝（fail-closed）")

    from app.providers.net import install_http_timeouts
    install_http_timeouts()

    import uvicorn
    uvicorn.run(build_app(), host="0.0.0.0", port=config.PORT,
                log_level=config.LOG_LEVEL)


if __name__ == "__main__":
    main()
