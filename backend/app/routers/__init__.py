"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import recon, resource

# recon 是固定前缀路由，必须先于 /api/{module} 动态路由注册。
ROUTERS = [recon, resource]
