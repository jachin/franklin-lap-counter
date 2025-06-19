import asyncio
import multiprocessing
import threading


class AsyncMultiprocessingQueueBridge:
    """
    Bridges a blocking multiprocessing.Queue to an asyncio.Queue.

    Use by creating an instance with a multiprocessing.Queue and an optional
    asyncio event loop. The bridge spawns a background thread to read from the
    blocking multiprocessing.Queue and puts the messages into an asyncio.Queue
    which can be awaited asynchronously.

    Call `async_queue.get()` in async tasks to receive messages.
    Call `stop()` to shut down the background reader thread.
    """

    def __init__(self, mp_queue: multiprocessing.Queue, loop=None):
        self.mp_queue = mp_queue
        self.async_queue = asyncio.Queue()
        self.loop = loop or asyncio.get_event_loop()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._reader_thread, daemon=True)
        self._thread.start()

    def _reader_thread(self):
        while not self._stopped.is_set():
            try:
                # Blocking get with timeout to allow checking stop event
                msg = self.mp_queue.get(timeout=0.1)
            except Exception:
                # Timeout or other exception: just loop again if not stopped
                continue
            # Safely put the item into the asyncio queue in the event loop thread
            asyncio.run_coroutine_threadsafe(self.async_queue.put(msg), self.loop)

    async def get(self):
        """
        Async get from the asyncio queue, waits until a message is available.
        """
        return await self.async_queue.get()

    def stop(self):
        """
        Stops the background thread gracefully.
        """
        self._stopped.set()
        self._thread.join()
