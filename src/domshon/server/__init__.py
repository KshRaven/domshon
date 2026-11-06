"""Domshon server package."""


def run():
    import uvicorn

    from ..config import Settings
    from .app import create_app

    s = Settings()
    app = create_app(s)
    uvicorn.run(app, host=s.get("host"), port=int(s.get("port")),
                proxy_headers=bool(s.get("trust_proxy")),
                forwarded_allow_ips="*" if s.get("trust_proxy") else "127.0.0.1",
                log_level="info")
