import sys
import logging
import textwrap
import collections

from npyscreen import wgbutton
from npyscreen import wgmultiline


debug = 'debug' in sys.argv

logger = logging.getLogger(__name__)


class StdWrapper:

    MAXLEN = 1000

    def __init__(self, std):
        self.std = std
        self.text = collections.deque(maxlen=self.MAXLEN)

    def write(self, msg):
        self.text.append(msg)
        self.on_write(msg)

    def on_write(self, msg):
        pass

    def get_text(self):
        return ''.join(self.text)

    def fileno(self):
        return self.std.fileno()

if not debug:
    sys.stdout = stdout_wrapper = StdWrapper(sys.stdout)
    sys.stderr = stderr_wrapper = StdWrapper(sys.stderr)

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(threadName)s - %(name)s - %(levelname)s - %(message)s')


import asyncio
import threading

import npyscreen

from osfoffline.database_manager import models
from osfoffline.database_manager.db import session
from osfoffline.sync.local import LocalSync
from osfoffline.sync.remote import RemoteSync
from osfoffline.tasks.queue import OperationsQueue, InterventionQueue

try:
    asyncio.ensure_future
except AttributeError:
    asyncio.ensure_future = asyncio.async


class BackgroundWorker(threading.Thread):

    def __init__(self):
        super().__init__()
        self.loop = None

    def _ensure_event_loop(self):
        try:
            return asyncio.get_event_loop()
        except (AssertionError, RuntimeError):
            asyncio.set_event_loop(asyncio.new_event_loop())
        return asyncio.get_event_loop()

    def run(self):
        self.loop = self._ensure_event_loop()

        root_dir = '/Users/michael/Desktop/OSF'
        user = session.query(models.User).one()

        self.operation_queue = OperationsQueue()
        self.operation_queue_task = asyncio.ensure_future(self.operation_queue.start())
        self.operation_queue_task.add_done_callback(self._handle_exception)

        self.intervention_queue = InterventionQueue()

        self.remote_sync = RemoteSync(self.operation_queue, self.intervention_queue, user)
        self.loop.run_until_complete(self.remote_sync.initialize())

        self.local_sync = LocalSync(user, self.operation_queue, self.intervention_queue)
        self.local_sync.start()

        self.remote_sync_job = asyncio.ensure_future(self.remote_sync.start())
        self.remote_sync_job.add_done_callback(self._handle_exception)

        self.loop.run_forever()

    def _handle_exception(self, future):
        logger.info('In handle exception')
        if future.exception():
            logger.info('Sync.handle_exception')
            # self.database_task.cancel()
            # self.queue_task.cancel()
            # raise future.exception()
            raise future.exception()

    def stop(self):
        if self.loop:
            self.loop.stop()


class DecisionForm(npyscreen.ActionPopup):
    SHOW_ATT = 2
    SHOW_ATX = 10
    DEFAULT_LINES = 12
    DEFAULT_COLUMNS = 120

    def __init__(self, intervention, parentApp=None):
        self.intervention = intervention
        super().__init__(parentApp=parentApp)

    def create(self):
        self.center_on_display()
        # self.preserve_selected_widget = True
        mlw = self.add(wgmultiline.Pager,)
        text = [self.intervention.__class__.__name__ + ':']
        text.extend(textwrap.wrap(self.intervention.description, self.DEFAULT_COLUMNS - 10))
        mlw.values = text

    def generate_button(self, option):
        class OptionButton(wgbutton.MiniButtonPress):
            def whenPressed(self):
                self.parent.editing = False
                self.parent.value = option
        return OptionButton

    def create_control_buttons(self):
        offset = -2
        for i, option in enumerate(self.intervention.options):
            offset -= 3
            offset -= len(str(option))
            self._add_button(str(i), self.generate_button(option), str(option), -2, offset, None)


class MainForm(npyscreen.Form):

    def create(self):
        self.queue_status = self.add(npyscreen.TitleText, name="Queue Status", max_height=3, value='0', editable=False, max_width=25)

        self.sync_now = self.add(npyscreen.ButtonPress, name='Sync Now', relx=-15, rely=2)
        self.sync_now.whenPressed = self._sync_now

        self.queue = self.add(npyscreen.BoxTitle, name="Queue", rely=4, max_height=10)
        self.queue.entry_widget.scroll_exit = True

        self.logs = self.add(npyscreen.BufferPager, name="Logs")

        self.add_event_hander("STDWRITEEVENT", self.ev_std_write_event_handler)

    def _sync_now(self):
        self.parentApp.worker.loop.call_soon_threadsafe(asyncio.ensure_future, self.parentApp.worker.remote_sync.sync_now())

    def ev_std_write_event_handler(self, event):
        msg = event.payload
        self.logs.buffer([msg])
        self.logs.display()

    def while_waiting(self):
        self.queue_status.value = '{}/{}'.format(self.parentApp.worker.operation_queue.qsize(), self.parentApp.worker.operation_queue.MAX_SIZE)
        self.queue_status.display()
        self.queue.values = list(self.parentApp.worker.operation_queue._queue)
        self.queue.display()
        self.logs.display()


class App(npyscreen.StandardApp):

    def __init__(self, worker):
        super().__init__()
        self.worker = worker
        self.loop = asyncio.get_event_loop()
        stdout_wrapper.on_write = self.on_std_write
        stderr_wrapper.on_write = self.on_std_write

    def onStart(self):
        self.keypress_timeout_default = 1

        self.main = self.addForm('MAIN', MainForm)

        self.worker.start()

    def on_std_write(self, msg):
        if not msg == '\n':
            self.queue_event(npyscreen.Event("STDWRITEEVENT", msg))

    def while_waiting(self):
        try:
            intervention = self.worker.intervention_queue.get_nowait()
            df = DecisionForm(intervention, parentApp=self.main)
            df.edit()
            logger.info('Got decision {} for {}'.format(df.value, intervention))
            self.worker.loop.call_soon_threadsafe(asyncio.ensure_future, intervention.resolve(df.value))
            # self.worker.loop.call_soon_threadsafe(self.worker.intervention_queue.task_done)
            self.worker.intervention_queue.task_done()
        except asyncio.QueueEmpty:
            pass


if __name__ == '__main__':
    worker = BackgroundWorker()

    try:
        if not debug:
            try:
                app = App(worker)
                app.run()
            finally:
                sys.stdout = sys.__stdout__
                sys.stderr = sys.__stderr__
                sys.stdout.write(stdout_wrapper.get_text())
                sys.stderr.write(stderr_wrapper.get_text())
        else:
            worker.start()
            worker.join()
    except KeyboardInterrupt:
        worker.stop()
