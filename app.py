import importlib
import os
import pkgutil

from flask import Flask

import routes

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'output'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size

# Ensure folders exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# Auto-register every blueprint in the routes package: any module there
# that defines a module-level `bp` is picked up — drop in a new module
# and restart the app.
for _mod_info in pkgutil.iter_modules(routes.__path__):
    _module = importlib.import_module(f'routes.{_mod_info.name}')
    _bp = getattr(_module, 'bp', None)
    if _bp is not None:
        app.register_blueprint(_bp)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5010)
