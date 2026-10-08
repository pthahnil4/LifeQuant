# -*- coding: utf-8 -*-
"""
K 线训练模块（cryptoTrade 主应用内蓝图）
=======================================

来源：futureStockTrade 合并迁移 Phase 4（原 ``kline/`` 顶层包，1649 行蓝图 +
53 条路由 + 三品类 provider 数据层）。URL 前缀仍是 ``/kline``，站内旧链接、
前端 ``url_for('kline.static', ...)``、``/kline/api/*`` 全部原样可用。

目录结构
--------
- ``routes.py``        Flask 蓝图（页面 + API），蓝图名保持 ``'kline'``
- ``store.py``         SQLite 持久化（原名 ``db.py``，避开宿主的 ``database.py``）
- ``settings.py``      本模块专用配置：替代原根级 ``config.py`` 的那一半
                       （密钥只从环境变量 / data/ 外置文件来，无代码内默认值）
- ``auth_service.py``  邮箱验证码注册 / 多标识登录 / 改密 / 备注
- ``mailer.py``        验证码邮件（SMTP 配置复用宿主「任务系统 → 邮件配置」）
- ``registry.py``      三品类品种注册与校验
- ``training_service.py``  品类无关的训练开局 / 盲选 / 结算 / 对战判定
- ``chart_serialize.py``   指标计算 + 图表 JSON 序列化（复用 crypto/market_strategy）
- ``kline_adapter.py``     OKX 公开 API 加密币 K 线适配器
- ``providers/``           crypto / futures / stock 三品类取数 Provider
- ``td_cache/``            TDengine 历史 K 线缓存工具（闲置能力，默认关闭）
- ``templates/``           页面模板；``static/`` 前端脚本（kline.js / trade.js）

跨模块依赖（合并后统一走显式包路径，不再拼 sys.path）
----------------------------------------------------
- 指标：``crypto.market_strategy.indicators``（Phase 1 迁入的公共策略层）
- 期货取数：``crypto.futures.futures_adapter``（Phase 1/2）
- 股票取数：``crypto.stocks.stock_adapter``（Phase 3）

登录态口径（用户已拍板的 D2 决策）
--------------------------------
- 账号体系保留 K 线训练自己的 ``users`` 表（data/kline_training.db），
  不与宿主 MySQL 账号打通；
- Flask session 键由 ``uid`` 改为 ``kline_uid``，与宿主其它模块的会话键隔离；
- 与宿主 Web 访问闸门（crypto/web_auth.py）互不干扰：闸门管「谁能打开面板」，
  kline_uid 管「谁是这个训练账号」，两道关卡各管各的。

使用方式::

    from crypto.kline_training.routes import kline_bp
    app.register_blueprint(kline_bp, url_prefix='/kline')   # 必须在闸门之前
"""
