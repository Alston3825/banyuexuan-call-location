import os

import uvicorn

from .app import create_app

if __name__ == '__main__':
    uvicorn.run(create_app(os.environ.get('BYCALL_API_KEY'), models_dir=os.environ.get('MODELS_DIR', '/models'),
                           service=os.environ.get('HOST_SERVICE', 'host'),
                           health_url=os.environ.get('HOST_HEALTH_URL', 'http://host:8000/health')),
                host='0.0.0.0', port=int(os.environ.get('UPDATER_PORT', '8200')))
