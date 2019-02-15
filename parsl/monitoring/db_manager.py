import logging
import threading
import queue
import os
import time
from enum import Enum

from parsl.providers.error import OptionalModuleMissing

try:
    import sqlalchemy as sa
    from sqlalchemy import Column, Text, PrimaryKeyConstraint
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.ext.declarative import declarative_base
except ImportError:
    _sqlalchemy_enabled = False
else:
    _sqlalchemy_enabled = True

try:
    from sqlalchemy_utils import get_mapper
except ImportError:
    _sqlalchemy_utils_enabled = False
else:
    _sqlalchemy_utils_enabled = True

WORKFLOW = 'workflow'    # Workflow table includes workflow metadata
TASK = 'task'            # Task table includes task metadata
STATUS = 'status'        # Status table includes task status
RESOURCE = 'resource'    # Resource table includes task resource utilization


class MessageType(Enum):

    # Reports any task related info such as launch, completion etc.
    TASK_INFO = 0

    # Reports of resource utilization on a per-task basis
    RESOURCE_INFO = 1

    # Top level workflow information
    WORKFLOW_INFO = 2


class Database(object):
    if not _sqlalchemy_enabled:
        raise OptionalModuleMissing(['sqlalchemy'],
                                    "Default database logging requires the sqlalchemy library.")
    if not _sqlalchemy_utils_enabled:
        raise OptionalModuleMissing(['sqlalchemy_utils'],
                                    "Default database logging requires the sqlalchemy_utils library.")
    Base = declarative_base()

    def __init__(self,
                 url='sqlite:///monitoring.db',
                 username=None,
                 password=None,
            ):

        self.eng = sa.create_engine(url)
        self.meta = self.Base.metadata

        self.meta.create_all(self.eng)
        self.meta.reflect(bind=self.eng)

        Session = sessionmaker(bind=self.eng)
        self.session = Session()

    def update(self, table=None, columns=None, messages=None):
        table = self.meta.tables[table]
        mappings = self._generate_mappings(table, columns=columns,
                                           messages=messages)
        mapper = get_mapper(table)
        self.session.bulk_update_mappings(mapper, mappings)
        self.session.commit()

    def insert(self, table=None, messages=None):
        table = self.meta.tables[table]
        mappings = self._generate_mappings(table, messages=messages)
        mapper = get_mapper(table)
        self.session.bulk_insert_mappings(mapper, mappings)
        self.session.commit()

    def _generate_mappings(self, table, columns=None, messages=[]):
        mappings = []
        for msg in messages:
            m = {}
            if columns is None:
                columns = table.c.keys()
            for column in columns:
                m[column] = msg[column]
            mappings.append(m)
        return mappings

    class Workflow(Base):
        __tablename__ = WORKFLOW
        run_id = Column(Text, nullable=False, primary_key=True)
        workflow_name = Column(Text, nullable=True)
        workflow_version = Column(Text, nullable=True)
        time_began = Column(Text, nullable=False)
        time_completed = Column(Text)
        host = Column(Text, nullable=False)
        user = Column(Text, nullable=False)
        rundir = Column(Text, nullable=False)
        tasks_failed_count = Column(Text, nullable=False)
        tasks_completed_count = Column(Text, nullable=False)

    # TODO: expand to full set of info
    class Status(Base):
        __tablename__ = STATUS
        task_id = Column(Text, sa.ForeignKey('task.task_id'), nullable=False)
        task_status_name = Column(Text, nullable=False)
        timestamp = Column(Text, nullable=False)
        run_id = Column(Text, sa.ForeignKey('workflow.run_id'), nullable=False)
        __table_args__ = (
                          PrimaryKeyConstraint('task_id', 'run_id', 'task_status_name', 'timestamp'),
                        )

    class Task(Base):
        __tablename__ = TASK
        task_id = Column('task_id', Text, nullable=False)
        run_id = Column('run_id', Text, nullable=False)
        task_executor = Column('task_executor', Text, nullable=False)
        task_func_name = Column('task_func_name', Text, nullable=False)
        task_time_submitted = Column('task_time_submitted', Text, nullable=False)
        task_time_returned = Column('task_time_returned', Text, nullable=True)
        task_memoize = Column('task_memoize', Text, nullable=False)
        task_inputs = Column('task_inputs', Text, nullable=True)
        task_outputs = Column('task_outputs', Text, nullable=True)
        task_stdin = Column('task_stdin', Text, nullable=True)
        task_stdout = Column('task_stdout', Text, nullable=True)
        __table_args__ = (
                          PrimaryKeyConstraint('task_id', 'run_id'),
                        )

    class Resource(Base):
        __tablename__ = RESOURCE
        task_id = Column('task_id', Text, sa.ForeignKey('task.task_id'), nullable=False)
        timestamp = Column('timestamp', Text, nullable=False)
        run_id = Column('run_id', Text, sa.ForeignKey('workflow.run_id'), nullable=False)
        psutil_process_pid = Column('psutil_process_pid', Text, nullable=True)
        psutil_process_cpu_percent = Column('psutil_process_cpu_percent', Text, nullable=True)
        psutil_process_memory_percent = Column('psutil_process_memory_percent', Text, nullable=True)
        psutil_process_children_count = Column('psutil_process_children_count', Text, nullable=True)
        psutil_process_time_user = Column('psutil_process_time_user', Text, nullable=True)
        psutil_process_time_system = Column('psutil_process_time_system', Text, nullable=True)
        psutil_process_memory_virtual = Column('psutil_process_memory_virtual', Text, nullable=True)
        psutil_process_memory_resident = Column('psutil_process_memory_resident', Text, nullable=True)
        psutil_process_disk_read = Column('psutil_process_disk_read', Text, nullable=True)
        psutil_process_disk_write = Column('psutil_process_disk_write', Text, nullable=True)
        psutil_process_status = Column('psutil_process_status', Text, nullable=True)
        __table_args__ = (
                          PrimaryKeyConstraint('task_id', 'run_id', 'timestamp'),
                        )

    def __del__(self):
        self.session.close()


class DatabaseManager(object):
    def __init__(self,
                 db_url='sqlite:///monitoring.db',
                 logdir='.',
                 logging_level=logging.INFO,
                 batching_interval=1,
                 batching_threshold=99999,
               ):

        self.logdir = logdir
        try:
            os.makedirs(self.logdir)
        except FileExistsError:
            pass

        self.logger = start_file_logger("{}/database_manager.log".format(self.logdir), level=logging_level)
        self.logger.debug("Initializing Database Manager process")

        self.db = Database(db_url)
        self.batching_interval = batching_interval
        self.batching_threshold = batching_threshold

        self.pending_priority_queue = queue.Queue()
        self.pending_resource_queue = queue.Queue()

    def start(self, priority_queue, resource_queue):

        self._kill_event = threading.Event()
        self._priority_queue_pull_thread = threading.Thread(target=self._migrate_logs_to_internal,
                                                            args=(priority_queue, 'priority', self._kill_event,)
                                              )
        self._priority_queue_pull_thread.start()

        self._resource_queue_pull_thread = threading.Thread(target=self._migrate_logs_to_internal,
                                                            args=(resource_queue, 'resource', self._kill_event,)
                                              )
        self._resource_queue_pull_thread.start()

        while (not self._kill_event.is_set() or
               self.pending_priority_queue.qsize() != 0 or self.pending_resource_queue.qsize() != 0 or
               priority_queue.qsize() != 0 or resource_queue.qsize() != 0):

            """
            WORKFLOW_INFO and TASK_INFO messages

            """
            self.logger.debug("""Checking STOP conditions: {}, {}, {}, {}, {}""".format(
                              self._kill_event.is_set(),
                              self.pending_priority_queue.qsize() != 0, self.pending_resource_queue.qsize() != 0,
                              priority_queue.qsize() != 0, resource_queue.qsize() != 0))

            messages = self._get_messages_in_batch(self.pending_priority_queue,
                                                   interval=self.batching_interval,
                                                   threshold=self.batching_threshold)

            if messages:
                self.logger.debug("Got {} messages from priority queue".format(len(messages)))
            update_messages, insert_messages, all_messages = [], [], []
            for msg_type, msg in messages:
                if msg_type.value == MessageType.WORKFLOW_INFO.value:
                    if "python_version" in msg:   # workflow start message
                        self.logger.debug("Inserting workflow start info to WORKFLOW table")
                        self._insert(table=WORKFLOW, messages=[msg])
                    else:                         # workflow end message
                        self.logger.debug("Updating workflow end info to WORKFLOW table")
                        self._update(table=WORKFLOW,
                                     columns=['run_id', 'tasks_failed_count', 'tasks_completed_count', 'time_completed'],
                                     messages=[msg])
                else:                             # TASK_INFO message
                    all_messages.append(msg)
                    if msg['task_time_returned'] is not None:
                        update_messages.append(msg)
                    else:
                        insert_messages.append(msg)

            self.logger.debug("Updating and inserting TASK_INFO to all tables")
            self._update(table=WORKFLOW,
                         columns=['run_id', 'tasks_failed_count', 'tasks_completed_count'],
                         messages=update_messages)

            if insert_messages:
                self._insert(table=TASK, messages=insert_messages)
            if update_messages:
                self._update(table=TASK,
                             columns=['task_time_returned', 'run_id', 'task_id'],
                             messages=update_messages)
            self._insert(table=STATUS, messages=all_messages)

            """
            RESOURCE_INFO messages

            """
            messages = self._get_messages_in_batch(self.pending_resource_queue,
                                                   interval=self.batching_interval,
                                                   threshold=self.batching_threshold)

            if messages:
                self.logger.debug("Got {} messages from resource queue".format(len(messages)))
            self._insert(table=RESOURCE, messages=messages)
            # self._insert(STATUS, msg)

    def _migrate_logs_to_internal(self, logs_queue, queue_tag, kill_event):
        self.logger.info("[{}_queue_PULL_THREAD] Starting".format(queue_tag))

        while not kill_event.is_set() or logs_queue.qsize() != 0:
            self.logger.debug("""Checking STOP conditions for {} threads: {}, {}"""
                              .format(queue_tag, kill_event.is_set(), logs_queue.qsize() != 0))
            try:
                x, addr = logs_queue.get(block=False)
            except queue.Empty:
                continue
            else:
                if queue_tag == 'priority':
                    if x == 'STOP':
                        self.close()
                    else:
                        self.pending_priority_queue.put(x)
                elif queue_tag == 'resource':
                    self.pending_resource_queue.put(x[-1])

    def _update(self, table, columns, messages):
        self.db.update(table=table, columns=columns, messages=messages)

    def _insert(self, table, messages):
        self.db.insert(table=table, messages=messages)

    def _get_messages_in_batch(self, msg_queue, interval=1, threshold=99999):
        messages = []
        start = time.time()
        while True:
            if time.time() - start >= interval or len(messages) >= threshold:
                break
            try:
                x = msg_queue.get(block=False)
                # self.logger.debug("Database manager receives a message {}".format(x))
            except queue.Empty:
                break
            else:
                messages.append(x)
        return messages

    def close(self):
        if self.logger:
            self.logger.info("Finishing all the logging and terminating Database Manager.")
        self.batching_interval, self.batching_threshold = float('inf'), float('inf')
        self._kill_event.set()


def start_file_logger(filename, name='database_manager', level=logging.DEBUG, format_string=None):
    """Add a stream log handler.
    Parameters
    ---------
    filename: string
        Name of the file to write logs to. Required.
    name: string
        Logger name. Default="parsl.executors.interchange"
    level: logging.LEVEL
        Set the logging level. Default=logging.DEBUG
        - format_string (string): Set the format string
    format_string: string
        Format string to use.
    Returns
    -------
        None.
    """
    if format_string is None:
        format_string = "%(asctime)s %(name)s:%(lineno)d [%(levelname)s]  %(message)s"

    global logger
    logger = logging.getLogger(name)
    logger.setLevel(level)
    handler = logging.FileHandler(filename)
    handler.setLevel(level)
    formatter = logging.Formatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def dbm_starter(priority_msgs, resource_msgs, *args, **kwargs):
    """Start the database manager process

    The DFK should start this function. The args, kwargs match that of the monitoring config

    """
    dbm = DatabaseManager(*args, **kwargs)
    dbm.start(priority_msgs, resource_msgs)
