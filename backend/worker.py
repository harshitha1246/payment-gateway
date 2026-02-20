import os

from redis import Redis
from rq import Connection, Worker

from queue_jobs import QUEUE_NAME, REDIS_URL


def run_worker():
    redis_conn = Redis.from_url(REDIS_URL)
    with Connection(redis_conn):
        worker = Worker([QUEUE_NAME])
        worker.work(with_scheduler=True)


if __name__ == "__main__":
    run_worker()
