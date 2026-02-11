"""
ACZM MCQ Generator - Desktop Application
Run this script to open the app in a native desktop window.
"""

import webview
import threading
import time
from app import app


def start_server():
    """Start Flask server in background thread"""
    app.run(host='127.0.0.1', port=5000, use_reloader=False, debug=False)


if __name__ == '__main__':
    # Start Flask server in background thread
    server = threading.Thread(target=start_server, daemon=True)
    server.start()

    # Give the server a moment to start
    time.sleep(1)

    # Create native desktop window
    webview.create_window(
        'ACZM MCQ Generator',
        'http://127.0.0.1:5000',
        width=1200,
        height=800,
        min_size=(800, 600)
    )
    webview.start()
