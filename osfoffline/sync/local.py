import os
import asyncio
import logging

from watchdog.observers import Observer

from osfoffline import utils
from osfoffline import settings
from osfoffline.client import osf
from osfoffline.database import session
from osfoffline.database.models import File
from osfoffline.database.models import Node
from osfoffline.sync.ext.watchdog import ConsolidatedEventHandler
from osfoffline.tasks import operations
from osfoffline.utils import ensure_event_loop
from osfoffline.utils.authentication import get_current_user
from osfoffline.utils.path import ProperPath


logger = logging.getLogger(__name__)


class LocalSync(ConsolidatedEventHandler):

    def __init__(self, user, operation_queue):
        super().__init__()
        self.folder = user.folder

        self.observer = Observer()
        self.operation_queue = operation_queue
        self.observer.schedule(self, self.folder, recursive=True)

    def start(self):
        logger.info('Starting watchdog observer')
        self.observer.start()

    def stop(self):
        logger.debug('Stopping observer thread')
        # observer is actually a separate child thread and must be join()ed
        self.observer.stop()
        self.observer.join()

    def on_moved(self, event):
        logger.info('Moved {}: from {} to {}'.format((event.is_directory and 'directory') or 'file', event.src_path, event.dest_path))

    def on_created(self, event):
        logger.info('Created {}: {}'.format((event.is_directory and 'directory') or 'file', event.src_path))
        node = utils.extract_node(event.src_path)
        path = ProperPath(event.src_path, event.is_directory)
        if event.is_directory:
            return self.put_event(operations.RemoteCreateFolder(path, node))
        return self.put_event(operations.RemoteCreateFile(path, node))

    def on_deleted(self, event):
        logger.info('Deleted {}: {}'.format((event.is_directory and 'directory') or 'file', event.src_path))
        node = utils.extract_node(event.src_path)
        local = ProperPath(event.src_path, event.is_directory)
        db = utils.local_to_db(local, node)
        remote = utils.db_to_remote(db)

        if event.is_directory:
            return self.put_event(operations.RemoteDeleteFolder(remote, node))
        return self.put_event(operations.RemoteDeleteFile(remote, node))

    def on_modified(self, event):
        logger.info('Modified {}: {}'.format((event.is_directory and 'directory') or 'file', event.src_path))
        node = utils.extract_node(event.src_path)
        path = ProperPath(event.src_path, event.is_directory)
        if event.is_directory:
            # WHAT DO
            return self.put_event(operations.RemoteCreateFolder(path, node))
        return self.put_event(operations.RemoteUpdateFile(path, node))

    def put_event(self, event):
        self.operation_queue._loop.call_soon_threadsafe(asyncio.ensure_future, self.operation_queue.put(event))
