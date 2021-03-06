import logging
from sqlalchemy import desc
from flexget.ui.webui import register_plugin, db_session
from flask import request, render_template, flash, Module
from flexget.plugin import DependencyError

try:
    from flexget.plugins.output.history import History
except ImportError:
    raise DependencyError(issued_by='ui.history', missing='history')

log = logging.getLogger('ui.history')
history = Module(__name__)


@history.route('/')
def index():
    context = {'items': db_session.query(History).order_by(desc(History.time)).limit(50).all()}
    return render_template('history/history.html', **context)

register_plugin(history, menu='History')
