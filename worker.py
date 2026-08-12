"""
Scraper worker — the background loop, with no Flask.

Run as a SEPARATE Render service from app.py:

    web    service:  python app.py     (ENABLE_SCRAPER_THREAD unset)
    worker service:  python worker.py

WHY SPLIT
An OOM during a scrape used to kill the API too, so a browser crash took the
dashboard offline. Split, the dashboard stays up no matter what the scraper
does, and the two can be sized independently — the API needs almost no memory,
the worker needs a lot.

This is only possible because console logs now live in the scrape_logs table
rather than a process dict: the API can serve lines this process wrote.

RUN EXACTLY ONE
Two workers means two loops scraping the same users, both stamping
last_scraped_at, and each user's interval burned twice — the "ghost scrape"
failure at infrastructure level. Keep the worker at 1 instance; scale
concurrency with SCRAPE_MAX_WORKERS below, not with more instances.
"""
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()


def main():
    # Imported after load_dotenv so the module-level interpreter check and the
    # pool both see the real configuration.
    from scraper_multi_user import run_scraper
    import scrape_logs

    workers = os.getenv('SCRAPE_MAX_WORKERS', '3')
    print(
        f"\n{'=' * 68}\n"
        f"  PixelFlip scraper worker\n"
        f"  interpreter      : {sys.executable}\n"
        f"  max concurrency  : {workers} users at once\n"
        f"  db pool max      : {os.getenv('DB_POOL_MAX', '10')}\n"
        f"{'=' * 68}\n",
        flush=True,
    )

    def log_bridge(user_id, message, log_type='info'):
        scrape_logs.add_log(user_id, message, log_type)

    while True:
        try:
            run_scraper(log_callback=log_bridge)
        except KeyboardInterrupt:
            print('worker: shutting down', flush=True)
            scrape_logs.flush(force=True)
            return 0
        except Exception as e:
            # Never let one bad cycle kill the worker: Render would restart it,
            # but the in-flight scrape state and the log buffer would be lost.
            print(f"worker: scrape loop crashed ({type(e).__name__}: {e}) — restarting in 30s", flush=True)
            try:
                scrape_logs.flush(force=True)
            except Exception:
                pass
            time.sleep(30)


if __name__ == '__main__':
    sys.exit(main() or 0)
