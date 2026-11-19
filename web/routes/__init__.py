def register_routes(app):
    from web.routes.dashboard import dashboard_bp
    from web.routes.news import news_bp
    from web.routes.sources import sources_bp
    from web.routes.history import history_bp
    from web.routes.reports import reports_bp
    from web.routes.actions import actions_bp
    from web.routes.config_routes import config_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(news_bp, url_prefix="/news")
    app.register_blueprint(sources_bp, url_prefix="/sources")
    app.register_blueprint(history_bp, url_prefix="/history")
    app.register_blueprint(reports_bp, url_prefix="/reports")
    app.register_blueprint(actions_bp, url_prefix="/actions")
    app.register_blueprint(config_bp, url_prefix="/config")
