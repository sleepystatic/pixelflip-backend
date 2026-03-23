from flask import Flask, jsonify
import threading
import time
import os

app = Flask(__name__)

scraper_status = {
    'running': False,
    'error': None,
    'users_scraped': 0,
    'total_listings': 0,
    'last_cycle': None
}


@app.route('/')
def health_check():
    return jsonify({
        'status': 'running',
        'service': 'PixelFlip Multi-User Scraper',
        'scraper_running': scraper_status['running'],
        'users_scraped': scraper_status['users_scraped'],
        'total_listings': scraper_status['total_listings'],
        'last_cycle': scraper_status['last_cycle']
    })


@app.route('/status')
def status():
    return jsonify(scraper_status)


def start_scraper():
    """Run multi-user scraper"""
    global scraper_status

    try:
        print("🚀 Starting multi-user scraper...", flush=True)

        from scraper_multi_user import main as run_scraper

        scraper_status['running'] = True
        run_scraper()

    except Exception as e:
        error_msg = f"Scraper error: {str(e)}"
        print(f"❌ {error_msg}", flush=True)
        scraper_status['running'] = False
        scraper_status['error'] = error_msg

        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    print("=" * 60)
    print("FLASK SERVER - MULTI-USER SCRAPER")
    print("=" * 60)

    # Start scraper thread
    scraper_thread = threading.Thread(target=start_scraper, daemon=True)
    scraper_thread.start()

    time.sleep(2)

    # Start Flask
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)